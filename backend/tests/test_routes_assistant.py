"""Unit tests for authenticated assistant SSE transport and cleanup."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sanic import Request, Sanic
from sanic.response.types import BaseHTTPResponse
from sanic_ext.exceptions import ValidationError
from tortoise import Tortoise, timezone

from app.core.exceptions import UnauthorizedException
from app.core.middleware import on_request, on_response
from app.models.ai import AIProvider
from app.models.assistant import AssistantEvent
from app.models.user import UserRole
from app.routes import assistant as routes
from app.utils.json import dumps

BODY = {
    "model": {"provider_id": 1, "model_id": "test"},
    "messages": [{"role": "user", "content": "hello"}],
}


@pytest.fixture
def application():
    original_dumps = BaseHTTPResponse._dumps
    app = Sanic("assistant-route-test", configure_logging=False, dumps=dumps)
    try:
        yield app
    finally:
        Sanic.unregister_app(app)
        BaseHTTPResponse._dumps = original_dumps


def request(app, *, body=BODY, authenticated=True):
    result = Request(
        url_bytes=b"/_api/assistant/chat",
        headers={"content-type": "application/json"},
        version="1.1",
        method="POST",
        transport=None,
        app=app,
    )
    result.body = json.dumps(body).encode()
    result.ctx.user = SimpleNamespace(role=UserRole.ADMIN) if authenticated else None
    return result


@pytest.mark.parametrize("handler", [routes.chat, routes.list_models])
def test_login_required(application, handler):
    with pytest.raises(UnauthorizedException):
        asyncio.run(handler(request(application, authenticated=False)))


def test_invalid_request(application, monkeypatch):
    respond = AsyncMock()
    monkeypatch.setattr(Request, "respond", respond)

    with pytest.raises(ValidationError):
        asyncio.run(routes.chat(request(application, body={**BODY, "user_id": 2})))

    respond.assert_not_awaited()


def test_heartbeat_and_disconnect(application, monkeypatch):
    frames = []
    stopped = []
    monkeypatch.setattr(routes, "HEARTBEAT_INTERVAL", 0.01)

    async def stream(*_):
        try:
            yield AssistantEvent(event="start", data={"turn_id": "test"})
            await asyncio.Future()
        finally:
            stopped.append(True)

    async def send(frame):
        frames.append(frame)
        if frame.startswith(":"):
            raise BrokenPipeError

    response = SimpleNamespace(send=send)
    monkeypatch.setattr(Request, "respond", AsyncMock(return_value=response))
    monkeypatch.setattr(routes.AssistantService, "stream", stream)

    with pytest.raises(BrokenPipeError):
        asyncio.run(routes.chat(request(application)))

    assert frames[0].startswith("event: start\n")
    assert frames[-1] == ": keep-alive\n\n"
    assert stopped == [True]


def test_cancel_backpressure(application, monkeypatch):
    async def run():
        sending = asyncio.Event()
        stopped = asyncio.Event()
        produced = []

        async def stream(*_):
            try:
                while True:
                    produced.append(True)
                    yield AssistantEvent(event="text_delta", data={"text": "x"})
            finally:
                stopped.set()

        async def send(_):
            sending.set()
            await asyncio.Future()

        monkeypatch.setattr(
            Request, "respond", AsyncMock(return_value=SimpleNamespace(send=send))
        )
        monkeypatch.setattr(routes.AssistantService, "stream", stream)
        task = asyncio.create_task(routes.chat(request(application)))
        await asyncio.wait_for(sending.wait(), 5)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert stopped.is_set()
        assert len(produced) <= 18

    asyncio.run(run())


def test_http_contract(application, monkeypatch):
    app = application
    app.config.AUTO_EXTEND = False
    app.config.TOUCHUP = False
    app.config.AUTH_EXCLUDE_PATHS = []
    app.config.TOKEN_EXPIRATION_HOURS = 24
    app.asgi = True
    app.blueprint(routes.assistant, url_prefix="/_api/assistant")
    app.register_middleware(on_request, "request")
    app.register_middleware(on_response, "response")
    user = SimpleNamespace(
        id=1, role=UserRole.USER, expire_at=timezone.now() + timedelta(hours=1)
    )
    app.shared_ctx.sessions = {"browser-session": user}
    users = []

    async def stream(body, user):
        users.append(user)
        assert body.messages[0].content == "hello"
        yield AssistantEvent(event="start", data={"turn_id": "one"})
        yield AssistantEvent(event="text_delta", data={"text": "你好\n世界"})
        yield AssistantEvent(event="done", data={"usage": None, "tool_calls": 0})

    monkeypatch.setattr(routes.AssistantService, "stream", stream)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        try:
            await Tortoise.generate_schemas()
            provider = await AIProvider.create(
                name="Enabled",
                base_url="https://private.example",
                model_ids=["test"],
                api_key="encrypted-secret",
            )
            await AIProvider.create(
                name="Disabled",
                base_url="https://example.test",
                model_ids=["hidden"],
                enabled=False,
            )
            await AIProvider.create(
                name="Unconfigured", base_url="https://example.test"
            )
            await app._startup()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                for headers in ({}, {"authorization": "Bearer ks_mcp_not_a_session"}):
                    denied = await client.post(
                        "/_api/assistant/chat", json=BODY, headers=headers
                    )
                    assert denied.status_code == 401
                assert users == []
                headers = {"authorization": "Bearer browser-session"}
                models = await client.get("/_api/assistant/models", headers=headers)
                assert models.json()["data"] == [
                    {"id": provider.id, "name": "Enabled", "model_ids": ["test"]}
                ]
                assert "private.example" not in models.text
                assert "encrypted-secret" not in models.text
                result = await client.post(
                    "/_api/assistant/chat", json=BODY, headers=headers
                )

                assert result.status_code == 200
                assert result.headers["content-type"].startswith("text/event-stream")
                assert result.headers["cache-control"] == "no-store"
                assert result.headers["x-accel-buffering"] == "no"
                assert (
                    'event: text_delta\ndata: {"text":"你好\\n世界"}\n\n' in result.text
                )
                assert result.text.endswith(
                    'event: done\ndata: {"usage":null,"tool_calls":0}\n\n'
                )
                assert users[0].id == 1
                assert users[0].perms.indexer_ids == []
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
