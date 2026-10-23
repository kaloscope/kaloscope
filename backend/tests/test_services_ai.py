"""Unit tests for AI provider persistence and credential updates."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.models.ai import AIProvider, AIProviderConnection, AIProviderUpsert
from app.models.user import UserInfo, UserRole
from app.services import ai as ai_service
from app.services.ai import AIProviderService
from app.utils.crypto import fernet_decrypt


@asynccontextmanager
async def database():
    """Open an isolated database for provider service tests.

    Yields:
        Control while the temporary database is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture
def key_path(monkeypatch, tmp_path):
    monkeypatch.setattr(
        KaloscopeConfig,
        "_config",
        SimpleNamespace(secret_key_path=tmp_path / "database" / "secret.key"),
    )
    return tmp_path / "database" / "secret.key"


def settings(**overrides):
    """Build a validated provider request with local test defaults.

    Args:
        **overrides: Values replacing the default settings.

    Returns:
        Provider request with any requested credential operation.
    """
    return AIProviderUpsert.model_validate(
        {
            "name": "Test provider",
            "base_url": "https://example.com/v1",
            "model_ids": ["test-model"],
            **overrides,
        }
    )


def test_list_forbidden():
    user = cast(UserInfo, SimpleNamespace(role=UserRole.USER))

    with pytest.raises(ForbiddenException) as exc_info:
        asyncio.run(AIProviderService.list_providers(user))

    assert exc_info.value.status_code == 403
    assert exc_info.value.message == ErrorCode.FORBIDDEN


def test_save_and_read(key_path):
    async def run():
        async with database():
            provider = await AIProviderService.upsert(settings(api_key="test-secret"))
            saved = await AIProviderService.get(provider.id)
            public = await AIProviderService.dump(saved)
            listed = await AIProviderService.dump_list(AIProvider.all())

            assert fernet_decrypt(saved.api_key, key_path) == "test-secret"
            assert "test-secret" not in saved.api_key
            assert public["has_api_key"] is True
            assert public["model_ids"] == ["test-model"]
            assert public["preset"] is None
            assert listed == [public]
            assert "api_key" not in public
            assert "test-secret" not in str(public)
            assert "test-secret" not in str(listed)
            assert len(key_path.read_bytes()) == 32
            assert list(key_path.parent.glob("*.key")) == [key_path]

            await AIProviderService.delete(provider.id)
            await AIProviderService.delete(provider.id)

            assert await AIProvider.all().count() == 0
            with pytest.raises(NotFoundException):
                await AIProviderService.get(provider.id)

    asyncio.run(run())


def test_existing_secret_key(key_path):
    key_path.parent.mkdir(parents=True)
    existing_key = bytes(range(32))
    key_path.write_bytes(existing_key)

    async def run():
        async with database():
            provider = await AIProviderService.upsert(settings(api_key="new-secret"))

            assert key_path.read_bytes() == existing_key
            assert fernet_decrypt(provider.api_key, key_path) == "new-secret"
            assert list(key_path.parent.glob("*.key")) == [key_path]

    asyncio.run(run())


@pytest.mark.parametrize("update", [{}, {"api_key": None}])
def test_retain_key(key_path, update):
    async def run():
        async with database():
            provider = await AIProviderService.upsert(settings(api_key="test-secret"))
            original = provider.api_key

            saved = await AIProviderService.upsert(
                settings(
                    id=provider.id, model_ids=["other-model"], enabled=False, **update
                )
            )

            assert saved.api_key == original
            assert saved.model_ids == ["other-model"]
            assert saved.enabled is False
            assert saved.id == provider.id
            assert await AIProvider.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("replacement", ["new-secret", "", "   "])
def test_replace_key(key_path, replacement):
    async def run():
        async with database():
            provider = await AIProviderService.upsert(settings(api_key="old-secret"))

            saved = await AIProviderService.upsert(
                settings(id=provider.id, api_key=replacement)
            )
            public = await AIProviderService.dump(saved)

            assert public["has_api_key"] is bool(replacement.strip())
            if replacement.strip():
                assert fernet_decrypt(saved.api_key, key_path) == replacement
            else:
                assert saved.api_key is None

    asyncio.run(run())


def test_keyless_provider(key_path):
    async def run():
        async with database():
            provider = await AIProviderService.upsert(
                settings(preset="ollama", base_url="http://localhost:11434/v1")
            )

            assert provider.api_key is None
            assert (await AIProviderService.dump(provider))["has_api_key"] is False
            assert not key_path.exists()

    asyncio.run(run())


def test_endpoint_change(key_path):
    async def run():
        async with database():
            provider = await AIProviderService.upsert(settings(api_key="old-secret"))

            with pytest.raises(BadRequestException):
                await AIProviderService.upsert(
                    settings(id=provider.id, base_url="https://other.example/v1")
                )

            await provider.refresh_from_db()
            assert provider.base_url == "https://example.com/v1"
            assert fernet_decrypt(provider.api_key, key_path) == "old-secret"

            changed = await AIProviderService.upsert(
                settings(
                    id=provider.id,
                    base_url="https://other.example/v1",
                    api_key="new-key",
                )
            )

            assert changed.base_url == "https://other.example/v1"
            assert fernet_decrypt(changed.api_key, key_path) == "new-key"

            cleared = await AIProviderService.upsert(
                settings(
                    id=provider.id, base_url="http://localhost:1234/v1", api_key=""
                )
            )
            assert cleared.api_key is None

    asyncio.run(run())


def test_duplicate_name(key_path):
    async def run():
        async with database():
            first = await AIProviderService.upsert(settings(api_key="first-key"))
            other = await AIProviderService.upsert(settings(name="Other"))

            for id in (None, other.id):
                with pytest.raises(KaloscopeException) as error:
                    await AIProviderService.upsert(settings(id=id, api_key="new-key"))
                assert error.value.message == "name_already_exists"
                assert error.value.status_code == 409

            await first.refresh_from_db()
            await other.refresh_from_db()
            assert fernet_decrypt(first.api_key, key_path) == "first-key"
            assert other.name == "Other"
            assert other.api_key is None
            assert await AIProvider.all().count() == 2

    asyncio.run(run())


def test_missing_provider(key_path):
    async def run():
        async with database():
            with pytest.raises(NotFoundException):
                await AIProviderService.upsert(settings(id=99, api_key="new-key"))

            assert await AIProvider.all().count() == 0
            assert not key_path.exists()

    asyncio.run(run())


def test_lost_key(key_path):
    async def run():
        async with database():
            provider = await AIProviderService.upsert(settings(api_key="original"))
            key_path.unlink()

            with pytest.raises(RuntimeError):
                await AIProviderService.upsert(
                    settings(name="Other", api_key="new-key")
                )

            assert not key_path.exists()
            assert await AIProvider.all().count() == 1
            assert (await AIProviderService.dump(provider))["has_api_key"] is True

    asyncio.run(run())


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "saved-key"),
        ({"api_key": None}, "saved-key"),
        ({"api_key": "draft-key"}, "draft-key"),
        ({"api_key": ""}, ""),
        ({"base_url": "https://other.example/v1", "api_key": "draft-key"}, "draft-key"),
        ({"base_url": "http://localhost:1234/v1", "api_key": ""}, ""),
    ],
)
def test_discovery_credentials(key_path, monkeypatch, changes, expected):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "discovered-model"}]})

    monkeypatch.setattr(
        ai_service, "NetworkTransport", lambda **_: httpx.MockTransport(respond)
    )

    async def run():
        async with database():
            provider = await AIProviderService.upsert(settings(api_key="saved-key"))
            original = provider.api_key
            draft = AIProviderConnection.model_validate(
                {"id": provider.id, "base_url": provider.base_url, **changes}
            )

            assert await AIProviderService.discover_models(draft) == [
                "discovered-model"
            ]

            assert len(requests) == 1
            assert str(requests[0].url) == draft.base_url + "/models"
            assert requests[0].headers.get("Authorization") == (
                f"Bearer {expected}" if expected else None
            )
            await provider.refresh_from_db()
            assert provider.api_key == original
            assert provider.base_url == "https://example.com/v1"
            assert provider.model_ids == ["test-model"]

    asyncio.run(run())


def test_draft_discovery(key_path, monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        assert "Authorization" not in request.headers
        assert "Cookie" not in request.headers
        return httpx.Response(
            200,
            json={"data": [{"id": f"model-{len(requests)}"}]},
            headers={
                "Cache-Control": "public, max-age=3600",
                "Set-Cookie": "upstream=value",
            },
        )

    monkeypatch.setattr(
        ai_service, "NetworkTransport", lambda **_: httpx.MockTransport(respond)
    )

    async def run():
        async with database():
            draft = AIProviderConnection(base_url="http://localhost:11434/v1")

            assert await AIProviderService.discover_models(draft) == ["model-1"]
            assert await AIProviderService.discover_models(draft) == ["model-2"]

            assert len(requests) == 2
            assert await AIProvider.all().count() == 0
            assert not key_path.exists()

    asyncio.run(run())


def test_unsafe_discovery(key_path, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr(ai_service, "fetch_models", fetch)

    async def run():
        async with database():
            provider = await AIProviderService.upsert(settings(api_key="saved-key"))

            with pytest.raises(BadRequestException):
                await AIProviderService.discover_models(
                    AIProviderConnection(
                        id=provider.id, base_url="https://other.example/v1"
                    )
                )
            with pytest.raises(NotFoundException):
                await AIProviderService.discover_models(
                    AIProviderConnection(id=999, base_url=provider.base_url)
                )
            key_path.unlink()
            with pytest.raises(RuntimeError):
                await AIProviderService.discover_models(
                    AIProviderConnection(id=provider.id, base_url=provider.base_url)
                )

            fetch.assert_not_awaited()
            assert not key_path.exists()

    asyncio.run(run())
