"""Unit tests for AI route authorization and validated requests."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sanic import Request, Sanic
from sanic.response.types import BaseHTTPResponse
from sanic_ext.exceptions import ValidationError
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.core.middleware import on_response
from app.models.ai import AIProviderConnection, AIProviderUpsert
from app.models.user import UserRole
from app.routes import ai as routes
from app.utils.json import dumps


@pytest.fixture(autouse=True)
def application():
    original_dumps = BaseHTTPResponse._dumps
    app = Sanic("ai-route-test", configure_logging=False, dumps=dumps)
    try:
        yield app
    finally:
        Sanic.unregister_app(app)
        BaseHTTPResponse._dumps = original_dumps


def request(body=None, role=UserRole.ADMIN):
    """Create a real Sanic request without starting application services.

    Args:
        body: Optional JSON payload.
        role: Session role, or `None` for an unauthenticated request.

    Returns:
        Request suitable for the authorization and validation decorators.
    """
    result = Request(
        url_bytes=b"/_api/ai/provider/upsert",
        headers={"content-type": "application/json"},
        version="1.1",
        method="POST",
        transport=None,
        app=Sanic.get_app("ai-route-test"),
    )
    result.body = json.dumps(body).encode()
    result.ctx.user = SimpleNamespace(role=role) if role else None
    return result


@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (routes.provider_presets, {}),
        (routes.list_providers, {}),
        (routes.get_provider, {"id": 1}),
        (routes.upsert_provider, {}),
        (routes.discover_models, {}),
        (routes.delete_provider, {"id": 1}),
    ],
)
@pytest.mark.parametrize(
    ("role", "error"),
    [(None, UnauthorizedException), (UserRole.USER, ForbiddenException)],
)
def test_authorization(handler, arguments, role, error):
    with pytest.raises(error):
        asyncio.run(handler(request(role=role), **arguments))


def test_presets():
    response = asyncio.run(routes.provider_presets(request()))
    result = json.loads(response.body)

    assert len(result) == 8
    assert result[0]["id"] == "openai"
    deepseek = next(preset for preset in result if preset["id"] == "deepseek")
    assert deepseek["base_url"] == "https://api.deepseek.com"
    assert all("api_key" not in preset for preset in result)


def test_upsert(monkeypatch):
    provider = SimpleNamespace(id=1)
    saved = {"id": 1, "name": "Custom", "has_api_key": True}
    upsert = AsyncMock(return_value=provider)
    dump = AsyncMock(return_value=saved)
    monkeypatch.setattr(routes.AIProviderService, "upsert", upsert)
    monkeypatch.setattr(routes.AIProviderService, "dump", dump)

    response = asyncio.run(
        routes.upsert_provider(
            request(
                {
                    "name": "Custom",
                    "base_url": "https://example.com/v1",
                    "api_key": "key",
                }
            )
        )
    )

    assert json.loads(response.body) == saved
    body = upsert.await_args.args[0]
    assert isinstance(body, AIProviderUpsert)
    assert body.api_key.get_secret_value() == "key"
    dump.assert_awaited_once_with(provider)


def test_invalid_payload(monkeypatch):
    upsert = AsyncMock()
    monkeypatch.setattr(routes.AIProviderService, "upsert", upsert)

    with pytest.raises(ValidationError) as error:
        asyncio.run(
            routes.upsert_provider(
                request(
                    {
                        "name": "Custom",
                        "base_url": "file:///tmp/invalid",
                        "api_key": "private-secret\nheader",
                    }
                )
            )
        )

    upsert.assert_not_awaited()
    assert "private-secret" not in str(error.value)
    assert "private-secret" not in str(error.value.extra)


def test_delete(monkeypatch):
    delete = AsyncMock()
    monkeypatch.setattr(routes.AIProviderService, "delete", delete)

    response = asyncio.run(routes.delete_provider(request(), id=2))

    assert response.status == 204
    delete.assert_awaited_once_with(2)


def test_discover(monkeypatch):
    discover = AsyncMock(return_value=["model-one"])
    monkeypatch.setattr(routes.AIProviderService, "discover_models", discover)

    response = asyncio.run(
        routes.discover_models(request({"base_url": "https://example.com/v1"}))
    )

    assert json.loads(response.body) == ["model-one"]
    assert response.headers["Cache-Control"] == "no-store"
    assert isinstance(discover.await_args.args[0], AIProviderConnection)


def test_invalid_discovery(monkeypatch):
    discover = AsyncMock()
    monkeypatch.setattr(routes.AIProviderService, "discover_models", discover)

    with pytest.raises(ValidationError) as error:
        asyncio.run(
            routes.discover_models(
                request(
                    {
                        "base_url": "https://example.com",
                        "api_key": "private-key\nheader",
                    }
                )
            )
        )

    discover.assert_not_awaited()
    assert "private-key" not in str(error.value)


@pytest.mark.parametrize("preset", [None, "deepseek"])
def test_http_round_trip(application, monkeypatch, tmp_path, preset):
    monkeypatch.setattr(
        KaloscopeConfig,
        "_config",
        SimpleNamespace(secret_key_path=tmp_path / "secret.key"),
    )
    application.config.AUTO_EXTEND = False
    application.config.TOUCHUP = False
    application.asgi = True
    application.blueprint(routes.ai, url_prefix="/_api/ai")
    application.register_middleware(on_response, "response")

    @application.on_request
    async def login(request):
        request.ctx.user = SimpleNamespace(role=UserRole.ADMIN)

    async def run():
        await Tortoise.init(
            db_url=f"sqlite://{tmp_path / 'providers.db'}",
            modules={"models": ["app.models"]},
        )
        try:
            await Tortoise.generate_schemas()
            await application._startup()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/_api/ai/provider/upsert",
                    json={
                        "name": "Custom",
                        "preset": preset,
                        "base_url": "https://example.com/v1",
                        "model_ids": ["test-model"],
                        "api_key": "private-secret",
                    },
                )

                assert response.status_code == 200
                created = response.json()
                assert created["status"] == 200
                assert created["request_id"]
                assert created["data"]["created_at"]
                assert created["data"]["preset"] == preset
                assert created["data"]["has_api_key"] is True
                assert "api_key" not in created["data"]
                assert "private-secret" not in response.text
                id = created["data"]["id"]

                read = await client.get(f"/_api/ai/provider/{id}")
                listed = await client.get("/_api/ai/provider/list")
                presets = await client.get("/_api/ai/provider/presets")

                assert read.json()["data"] == created["data"]
                assert listed.json()["data"] == [created["data"]]
                assert len(presets.json()["data"]) == 8

                deleted = await client.post(f"/_api/ai/provider/{id}/delete")
                empty_list = await client.get("/_api/ai/provider/list")

                assert deleted.status_code == 204
                assert empty_list.json()["data"] == []
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
