"""Integration tests for independent MCP authentication and token management."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sanic import Sanic
from sanic.response.types import BaseHTTPResponse
from tortoise import Tortoise, timezone

from app.core.constants import MCP_PATH, SESSION_ID
from app.core.exceptions import error_handler
from app.core.middleware import SessionHolder, on_request, on_response
from app.mcp import close_mcp, start_mcp
from app.models.mcp import MCPAccessToken
from app.models.media import MediaLib
from app.models.user import User, UserInfo, UserPermission, UserRole
from app.routes.auth import auth
from app.routes.mcp import mcp
from app.utils.json import dumps


@asynccontextmanager
async def client(monkeypatch):
    """Exercise the actual HTTP routes and middleware against an isolated database.

    Args:
        monkeypatch: Fixture binding session helpers to this test application.

    Yields:
        HTTP client and application containing reader and administrator sessions.
    """
    original_dumps = BaseHTTPResponse._dumps
    app = Sanic(f"mcp_tokens_{uuid4().hex}", configure_logging=False, dumps=dumps)
    app.config.update(
        AUTO_EXTEND=False,
        TOUCHUP=False,
        AUTH_EXCLUDE_PATHS=[],
        TOKEN_EXPIRATION_HOURS=120,
        FALLBACK_ERROR_FORMAT="json",
    )
    app.shared_ctx.sessions = {}
    app.ctx.lib_watcher = SimpleNamespace(is_scanning=lambda _: False)
    monkeypatch.setattr(
        SessionHolder, "get_sessions", staticmethod(lambda: app.shared_ctx.sessions)
    )
    app.blueprint(auth, url_prefix="/_api/auth")
    app.blueprint(mcp, url_prefix=MCP_PATH)
    app.register_middleware(on_request, "request")
    app.register_middleware(on_response, "response")
    app.error_handler.add(Exception, error_handler)
    app.asgi = True
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        for name, role in (("reader", UserRole.USER), ("admin", UserRole.ADMIN)):
            user = await User.create(username=name, password="unused", role=role)
            now = timezone.now()
            app.shared_ctx.sessions[name] = UserInfo(
                id=user.id,
                login_id=name,
                username=name,
                role=role,
                avatar=None,
                preferences={},
                client_ip="127.0.0.1",
                login_at=now,
                last_activity=now,
                expire_at=now + timedelta(hours=1),
            )
        await app._startup()
        await start_mcp(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http, app
    finally:
        await close_mcp(app)
        await Tortoise.close_connections()
        Sanic.unregister_app(app)
        BaseHTTPResponse._dumps = original_dumps


async def issue(http, owner="reader", **body):
    """Create a dedicated token using a browser session.

    Args:
        http: HTTP client bound to the test application.
        owner: Browser session used to authorize creation.
        **body: Optional fields overriding the default token name.

    Returns:
        Created token metadata and its one-time credential.
    """
    response = await http.post(
        f"{MCP_PATH}/token/create",
        headers={"Authorization": f"Bearer {owner}"},
        json={"name": "Codex", **body},
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["data"]


async def rpc(http, token, method="tools/list", params=None):
    """Send an MCP request with an independent bearer credential.

    Args:
        http: HTTP client bound to the test application.
        token: Credential to send, or `None` to omit the header.
        method: JSON-RPC method to invoke.
        params: Optional method parameters; omitted values become an empty object.

    Returns:
        Unwrapped MCP HTTP response.
    """
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await http.post(
        MCP_PATH,
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


@pytest.mark.parametrize("expired", [False, True])
def test_management(monkeypatch, expired):
    async def run():
        async with client(monkeypatch) as (http, app):
            issued = await issue(http)
            other = await issue(http, "admin")
            if expired:
                await MCPAccessToken.filter(id=issued["id"]).update(
                    expires_at=timezone.now() - timedelta(seconds=1)
                )
            listed = await http.get(
                f"{MCP_PATH}/token/list", headers={"Authorization": "Bearer reader"}
            )
            records = listed.json()["data"]
            assert listed.headers["cache-control"] == "no-store"
            assert len(records) == 1 and records[0]["id"] == issued["id"]
            assert issued["token"] not in listed.text
            assert "token_hash" not in listed.text
            assert "user_id" not in records[0]
            assert records[0]["scope"] == "read_only"
            assert records[0]["status"] == ("expired" if expired else "active")

            for owner, id in (("admin", issued["id"]), ("reader", other["id"])):
                denied = await http.post(
                    f"{MCP_PATH}/token/{id}/delete",
                    headers={"Authorization": f"Bearer {owner}"},
                )
                assert denied.status_code == 404
            assert (await rpc(http, issued["token"])).status_code == (
                401 if expired else 200
            )

            deleted = await http.post(
                f"{MCP_PATH}/token/{issued['id']}/delete",
                headers={"Authorization": "Bearer reader"},
            )

            assert deleted.status_code == 204
            assert not await MCPAccessToken.filter(id=issued["id"]).exists()
            listed = await http.get(
                f"{MCP_PATH}/token/list", headers={"Authorization": "Bearer reader"}
            )
            assert listed.json()["data"] == []
            missing = await http.post(
                f"{MCP_PATH}/token/{issued['id']}/delete",
                headers={"Authorization": "Bearer reader"},
            )
            assert missing.status_code == 404
            assert (await rpc(http, issued["token"])).status_code == 401
            assert (await rpc(http, other["token"])).status_code == 200
            assert len(app.shared_ctx.sessions) == 2

    asyncio.run(run())


def test_session_independence(monkeypatch):
    async def run():
        async with client(monkeypatch) as (http, app):
            issued = await issue(http)
            session_expiry = app.shared_ctx.sessions["reader"].expire_at
            response = await rpc(http, issued["token"])
            assert response.status_code == 200
            assert len(response.json()["result"]["tools"]) == 30
            assert "set-cookie" not in response.headers
            assert app.shared_ctx.sessions["reader"].expire_at == session_expiry
            assert issued["token"] not in app.shared_ctx.sessions

            logged_out = await http.post(
                "/_api/auth/logout", headers={"Authorization": "Bearer reader"}
            )
            assert logged_out.status_code == 204
            assert "reader" not in app.shared_ctx.sessions
            assert (await rpc(http, issued["token"])).status_code == 200
            stored = await MCPAccessToken.get(id=issued["id"])
            assert stored.expires_at.isoformat() == issued["expires_at"]
            assert stored.last_used_at is not None

    asyncio.run(run())


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/_api/auth/current"),
        ("GET", f"{MCP_PATH}/token/list"),
        ("POST", f"{MCP_PATH}/token/create"),
        ("POST", f"{MCP_PATH}/token/1/delete"),
        ("POST", f"{MCP_PATH}/"),
    ],
)
def test_token_boundary(monkeypatch, method, path):
    async def run():
        async with client(monkeypatch) as (http, _):
            issued = await issue(http)
            response = await http.request(
                method,
                path,
                headers={"Authorization": f"Bearer {issued['token']}"},
                json={"name": "unauthorized"},
            )
            assert response.status_code == 401
            assert await MCPAccessToken.all().count() == 1
            assert (await MCPAccessToken.get(id=issued["id"])).status() == "active"

    asyncio.run(run())


@pytest.mark.parametrize("credential", [None, "reader", "missing", "Basic reader"])
def test_invalid_auth(monkeypatch, credential):
    async def run():
        async with client(monkeypatch) as (http, app):
            await issue(http)
            app.config.AUTH_EXCLUDE_PATHS = ["/*"]
            response = await rpc(http, credential)
            assert response.status_code == 401
            assert response.headers["www-authenticate"] == "Bearer"
            assert "set-cookie" not in response.headers

    asyncio.run(run())


def test_cookie_denied(monkeypatch):
    async def run():
        async with client(monkeypatch) as (http, _):
            issued = await issue(http)
            for credential in ("reader", issued["token"]):
                http.cookies.clear()
                http.cookies.set(SESSION_ID, credential)
                response = await rpc(http, None)
                assert response.status_code == 401

    asyncio.run(run())


def test_current_permissions(monkeypatch):
    async def run():
        async with client(monkeypatch) as (http, app):
            issued = await issue(http)
            user_id = app.shared_ctx.sessions["reader"].id
            library = await MediaLib.create(
                name="Visible", lib_type="movie", dir="/media", priority=1
            )
            request = {"name": "list_media_libraries", "arguments": {}}
            response = await rpc(http, issued["token"], "tools/call", request)
            assert response.json()["result"]["structuredContent"]["items"] == []

            permission = await UserPermission.create(
                user_id=user_id, rel_type="media_lib", rel_id=library.id
            )
            response = await rpc(http, issued["token"], "tools/call", request)
            items = response.json()["result"]["structuredContent"]["items"]
            assert [item["id"] for item in items] == [library.id]
            await permission.delete()
            response = await rpc(http, issued["token"], "tools/call", request)
            assert response.json()["result"]["structuredContent"]["items"] == []

            request = {"name": "list_ai_providers", "arguments": {}}
            for role, denied in (("user", True), ("admin", False), ("user", True)):
                await User.filter(id=user_id).update(role=role)
                response = await rpc(http, issued["token"], "tools/call", request)
                assert response.json()["result"].get("isError", False) is denied

    asyncio.run(run())


@pytest.mark.parametrize("body", [{"name": " "}, {"name": "Codex", "scope": "write"}])
def test_invalid_create(monkeypatch, body):
    async def run():
        async with client(monkeypatch) as (http, _):
            response = await http.post(
                f"{MCP_PATH}/token/create",
                headers={"Authorization": "Bearer reader"},
                json=body,
            )
            assert response.status_code == 400
            assert not await MCPAccessToken.all().exists()

    asyncio.run(run())
