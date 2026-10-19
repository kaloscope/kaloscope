"""Unit tests for MCP access token persistence and fixed lifetimes."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from pydantic import ValidationError
from tortoise import Tortoise, timezone

from app.core.exceptions import NotFoundException, UnauthorizedException
from app.models.mcp import MCPAccessToken, MCPAccessTokenCreate, MCPTokenScope
from app.models.user import User
from app.services.mcp import MCPAccessTokenService


@asynccontextmanager
async def database():
    """Open an isolated database for access token tests.

    Yields:
        The account owning test tokens.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        yield await User.create(username="reader", password="unused", role="user")
    finally:
        await Tortoise.close_connections()


@pytest.mark.parametrize("days", [30, 90, 365])
def test_create(days):
    async def run():
        async with database() as user:
            before = timezone.now()
            issued = await MCPAccessTokenService.create(
                user.id, MCPAccessTokenCreate(name="  Codex  ", expires_in_days=days)
            )
            after = timezone.now()
            stored = await MCPAccessToken.get(id=issued["id"])
            listed = await MCPAccessTokenService.list_tokens(user.id)

            assert issued["name"] == "Codex"
            assert issued["scope"] == MCPTokenScope.READ_ONLY
            assert issued["status"] == "active"
            assert issued["last_used_at"] is None
            token = issued.pop("token")
            assert token.startswith("ks_mcp_") and len(token) == 50
            assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest()
            assert stored.prefix == token[:17] == issued["prefix"]
            assert before + timedelta(days=days) <= stored.expires_at
            assert stored.expires_at <= after + timedelta(days=days)
            assert listed == [issued]
            assert not {"token", "token_hash", "user", "user_id"} & listed[0].keys()
            assert token not in str(await MCPAccessToken.all().values())

    asyncio.run(run())


def test_defaults():
    assert MCPAccessTokenCreate(name="Codex").expires_in_days == 90
    assert MCPAccessTokenCreate(name="Codex").scope == MCPTokenScope.READ_ONLY


def test_read_write_grant():
    async def run():
        async with database() as user:
            body = MCPAccessTokenCreate(name="Writer", scope=MCPTokenScope.READ_WRITE)
            issued = await MCPAccessTokenService.create(user.id, body)
            identity = await MCPAccessTokenService.authenticate(
                issued["token"], client_ip="127.0.0.1", user_agent=None
            )
            assert identity.mcp_scope == MCPTokenScope.READ_WRITE
            assert identity.role == "user"

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": " \t "},
        {"name": "x" * 65},
        {"name": "Codex", "expires_in_days": 0},
        {"name": "Codex", "expires_in_days": 31},
        {"name": "Codex", "expires_in_days": None},
        {"name": "Codex", "expires_in_days": True},
        {"name": "Codex", "expires_in_days": "90"},
        {"name": "Codex", "scope": "write"},
        {"name": "Codex", "user_id": 42},
    ],
)
def test_invalid_create(body):
    with pytest.raises(ValidationError):
        MCPAccessTokenCreate.model_validate(body)


def test_authenticate():
    async def run():
        async with database() as user:
            issued = await MCPAccessTokenService.create(
                user.id, MCPAccessTokenCreate(name="Codex")
            )
            await User.filter(id=user.id).update(role="admin", username="updated")
            identity = await MCPAccessTokenService.authenticate(
                issued["token"], client_ip="127.0.0.1", user_agent="test-client"
            )
            stored = await MCPAccessToken.get(id=issued["id"])

            assert identity.id == user.id
            assert identity.role == "admin"
            assert identity.username == "updated"
            assert identity.client_ip == "127.0.0.1"
            assert identity.user_agent == "test-client"
            assert identity.perms is None
            assert identity.login_id == f"mcp:{stored.id}"
            assert stored.last_used_at == identity.last_activity
            assert stored.expires_at == identity.expire_at == issued["expires_at"]

    asyncio.run(run())


@pytest.mark.parametrize("state", ["expired", "deleted", "owner_deleted", "changed"])
def test_unusable(state):
    async def run():
        async with database() as user:
            issued = await MCPAccessTokenService.create(
                user.id, MCPAccessTokenCreate(name="Codex")
            )
            token = issued["token"]
            if state == "expired":
                await MCPAccessToken.filter(id=issued["id"]).update(
                    expires_at=timezone.now() - timedelta(seconds=1)
                )
            elif state == "deleted":
                await MCPAccessTokenService.delete(issued["id"], user.id)
            elif state == "owner_deleted":
                await user.delete()
                assert not await MCPAccessToken.all().exists()
            else:
                token = token[:-1] + ("A" if token[-1] != "A" else "B")

            with pytest.raises(UnauthorizedException) as error:
                await MCPAccessTokenService.authenticate(
                    token, client_ip="127.0.0.1", user_agent=None
                )
            assert error.value.status_code == 401
            assert token not in str(error.value)
            stored = await MCPAccessToken.get_or_none(id=issued["id"])
            if stored is not None:
                assert stored.last_used_at is None
                if state != "changed":
                    assert stored.status() == state

    asyncio.run(run())


def test_expiry_boundary(monkeypatch):
    async def run():
        async with database() as user:
            issued = await MCPAccessTokenService.create(
                user.id, MCPAccessTokenCreate(name="Codex")
            )
            monkeypatch.setattr(timezone, "now", lambda: issued["expires_at"])

            with pytest.raises(UnauthorizedException):
                await MCPAccessTokenService.authenticate(
                    issued["token"], client_ip="127.0.0.1", user_agent=None
                )
            assert (await MCPAccessTokenService.list_tokens(user.id))[0]["status"] == (
                "expired"
            )

    asyncio.run(run())


@pytest.mark.parametrize("expired", [False, True])
def test_owner_and_delete(expired):
    async def run():
        async with database() as user:
            other = await User.create(username="other", password="unused", role="admin")
            issued = await MCPAccessTokenService.create(
                user.id, MCPAccessTokenCreate(name="Codex")
            )
            second = await MCPAccessTokenService.create(
                user.id, MCPAccessTokenCreate(name="Codex")
            )
            if expired:
                await MCPAccessToken.filter(id=issued["id"]).update(
                    expires_at=timezone.now() - timedelta(seconds=1)
                )

            assert second["token"] != issued["token"]
            assert await MCPAccessTokenService.list_tokens(other.id) == []
            with pytest.raises(NotFoundException):
                await MCPAccessTokenService.delete(issued["id"], other.id)
            with pytest.raises(NotFoundException):
                await MCPAccessTokenService.delete(second["id"] + 1, user.id)
            assert await MCPAccessToken.filter(id=issued["id"]).exists()

            await MCPAccessTokenService.delete(issued["id"], user.id)
            remaining = await MCPAccessTokenService.list_tokens(user.id)

            assert not await MCPAccessToken.filter(id=issued["id"]).exists()
            assert [token["id"] for token in remaining] == [second["id"]]
            with pytest.raises(NotFoundException):
                await MCPAccessTokenService.delete(issued["id"], user.id)
            assert (await MCPAccessToken.get(id=second["id"])).status() == "active"

    asyncio.run(run())
