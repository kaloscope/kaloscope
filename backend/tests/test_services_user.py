"""Tests for user preferences and history retention."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sanic import Sanic
from sanic.response import BaseHTTPResponse
from tortoise import Tortoise, timezone

from app.core.exceptions import error_handler
from app.core.middleware import SessionHolder, on_request, on_response
from app.models.user import (
    HistoryEntry,
    HistoryType,
    User,
    UserHistory,
    UserInfo,
    UserPreference,
    UserRole,
)
from app.routes.auth import auth
from app.routes.user import user as user_blueprint
from app.services.user import UserHistoryService, UserService
from app.utils.crypto import encrypt
from app.utils.json import dumps


@asynccontextmanager
async def _database() -> AsyncGenerator[None]:
    """Provide an isolated user database.

    Yields:
        Control while the in-memory database is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@asynccontextmanager
async def _client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[httpx.AsyncClient, Sanic]]:
    """Exercise user routes without production startup listeners.

    Args:
        monkeypatch: The fixture binding services to this test application.

    Yields:
        The HTTP client and isolated application with in-memory sessions.
    """
    original_dumps = BaseHTTPResponse._dumps
    app = Sanic(f"user_{uuid4().hex}", configure_logging=False, dumps=dumps)
    app.config.AUTO_EXTEND = False
    app.config.TOUCHUP = False
    app.config.AUTH_EXCLUDE_PATHS = ["/auth/login"]
    app.config.TOKEN_EXPIRATION_HOURS = 1
    app.shared_ctx.sessions = {}
    monkeypatch.setattr(UserService, "app_config", classmethod(lambda cls: app.config))
    monkeypatch.setattr(
        SessionHolder, "get_sessions", staticmethod(lambda: app.shared_ctx.sessions)
    )
    app.blueprint(auth, url_prefix="/_api/auth")
    app.blueprint(user_blueprint, url_prefix="/_api/user")
    app.register_middleware(on_request, "request")
    app.register_middleware(on_response, "response")
    app.error_handler.add(Exception, error_handler)
    app.asgi = True
    try:
        await app._startup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, app
    finally:
        Sanic.unregister_app(app)
        BaseHTTPResponse._dumps = original_dumps


@pytest.mark.parametrize("key", ["search_records", "watch_records", "read_records"])
@pytest.mark.parametrize("value", [-2, True, False, "3", "-1", 1.0, 1.5, None, []])
def test_retention_invalid(key, value):
    with pytest.raises(ValidationError):
        UserPreference.model_validate({"key": key, "value": value})


@pytest.mark.parametrize("key", ["search_records", "watch_records", "read_records"])
@pytest.mark.parametrize("value", [-1, 0, 1, 3, 42, 365])
def test_retention_valid(key, value):
    assert UserPreference(key=key, value=value).value == value


@pytest.mark.parametrize(
    "preferences",
    [None, {}, {"watch_records": 0}, {"watch_records": 3}, {"watch_records": 42}],
)
def test_retention_defaults(preferences):
    async def run():
        async with _database():
            user = await User.create(
                username="Reader",
                password="unused",
                role=UserRole.USER,
                preferences=preferences,
            )
            expected = {
                HistoryType.SEARCH: 3,
                HistoryType.VIDEO: (preferences or {}).get("watch_records", -1),
                HistoryType.TEXT: -1,
                HistoryType.IMAGE: -1,
            }
            for rel_type, days in expected.items():
                assert (
                    await UserHistoryService.retention_days(user.id, rel_type) == days
                )
                assert await UserHistoryService.retention_days(999, rel_type) == 0
            await user.refresh_from_db()
            assert user.preferences == preferences

    asyncio.run(run())


@pytest.mark.parametrize("value", [True, False, "3", -2, None])
def test_retention_stored_invalid(value):
    async def run():
        async with _database():
            user = await User.create(
                username="Reader",
                password="unused",
                role=UserRole.USER,
                preferences=dict.fromkeys(
                    ["search_records", "watch_records", "read_records"], value
                ),
            )
            for rel_type in HistoryType:
                assert await UserHistoryService.retention_days(user.id, rel_type) == 0

    asyncio.run(run())


@pytest.mark.parametrize("rel_type", list(HistoryType))
@pytest.mark.parametrize("days", [-1, 0, 3, 10**10])
def test_history_cleanup(monkeypatch, rel_type, days):
    now = datetime.now(UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)

    async def run():
        async with _database():
            user = await User.create(
                username="Reader",
                password="unused",
                role=UserRole.USER,
                preferences=dict.fromkeys(
                    ["search_records", "watch_records", "read_records"], days
                ),
            )
            other = await User.create(
                username="Other",
                password="unused",
                role=UserRole.USER,
            )
            expected = set()
            for owner in (user, other):
                for kind in HistoryType:
                    for age in (0, 3, 4, 1000):
                        history = await UserHistory.create(
                            user=owner, rel_type=kind, rel_id=age
                        )
                        await UserHistory.filter(id=history.id).update(
                            updated_at=now - timedelta(days=age)
                        )
                        if (
                            owner.id != user.id
                            or kind != rel_type
                            or (days != 0 and (days == -1 or age <= days))
                        ):
                            expected.add(history.id)

            await UserHistoryService.clean_expired(user.id, rel_type)
            assert set(await UserHistory.all().values_list("id", flat=True)) == expected

    asyncio.run(run())


@pytest.mark.parametrize("rel_type", [HistoryType.SEARCH, HistoryType.VIDEO])
@pytest.mark.parametrize("days", [-1, 0, 7])
def test_history_record(monkeypatch, rel_type, days):
    now = datetime.now(UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)

    async def run():
        async with _database():
            user = await User.create(
                username="Reader",
                password="unused",
                role=UserRole.USER,
                preferences={"search_records": days, "watch_records": days},
            )
            session = UserInfo(
                id=user.id,
                login_id="test",
                username=user.username,
                avatar=None,
                role=user.role,
                preferences=user.preferences or {},
                client_ip="127.0.0.1",
                login_at=now,
                expire_at=now + timedelta(hours=1),
                last_activity=now,
            )
            entry = HistoryEntry(
                rel_type=rel_type, rel_id=1, keyword="Query", position=10, percentage=20
            )
            first = await UserHistoryService.record(session, entry)
            if days == 0:
                assert first is None
                assert await UserHistory.all().count() == 0
                return
            assert first is not None
            await UserHistory.filter(id=first.id).update(
                updated_at=now - timedelta(days=30)
            )
            entry.position = 25
            entry.percentage = 50
            updated = await UserHistoryService.record(session, entry)
            assert updated is not None and updated.id == first.id
            assert updated.repetitions == 1
            assert updated.updated_at == now
            assert updated.locator is None
            if rel_type == HistoryType.VIDEO:
                assert (updated.position, updated.percentage) == (25, 50)
            else:
                assert updated.keyword == "Query"
            await UserHistoryService.clean_expired(user.id, rel_type)
            assert await UserHistory.all().count() == 1
            await User.filter(id=user.id).update(
                preferences={"search_records": 0, "watch_records": 0}
            )
            assert await UserHistoryService.record(session, entry) is None
            assert await UserHistory.all().count() == 1
            await UserHistoryService.clean_expired(user.id, rel_type)
            assert await UserHistory.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "preferences", [None, {"watch_records": 3}, {"watch_records": 42}]
)
def test_preferences_http(monkeypatch, preferences):
    async def run():
        async with _database(), _client(monkeypatch) as (client, app):
            user = await User.create(
                username="Reader",
                password=encrypt("test-password"),
                role=UserRole.USER,
                preferences=preferences,
            )
            other = await User.create(
                username="Other",
                password="unused",
                role=UserRole.USER,
                preferences={"watch_records": 7},
            )
            # old sessions may contain an unsaved watch default and no read setting
            now = timezone.now()
            session = UserInfo(
                id=user.id,
                login_id="legacy",
                username=user.username,
                avatar=None,
                role=user.role,
                preferences={"watch_records": 3} | (preferences or {}),
                client_ip="127.0.0.1",
                login_at=now,
                expire_at=now + timedelta(hours=1),
                last_activity=now,
            )
            app.shared_ctx.sessions["legacy"] = session
            app.shared_ctx.sessions["other"] = session.model_copy(
                update={"id": other.id, "preferences": other.preferences}
            )
            expected = UserService.DEFAULT_PREFERENCES | (preferences or {})
            client.headers["Authorization"] = "Bearer legacy"
            response = await client.get("/_api/auth/current")
            assert response.status_code == 200, response.text
            assert response.json()["data"]["preferences"] == expected
            assert session.preferences == ({"watch_records": 3} | (preferences or {}))

            response = await client.post(
                "/_api/auth/login",
                data={"username": user.username, "password": "test-password"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["user"]["preferences"] == expected
            await user.refresh_from_db()
            assert user.preferences == preferences

            for key in ("search_records", "watch_records", "read_records"):
                for value in (True, "3", -2, 1.0):
                    response = await client.post(
                        "/_api/user/update_pref", json={"key": key, "value": value}
                    )
                    assert response.status_code == 400, response.text
            await user.refresh_from_db()
            assert user.preferences == preferences

            for key, value in (
                ("read_records", 90),
                ("watch_records", 0),
                ("search_records", -1),
                ("read_records", -1),
                ("watch_records", 42),
                ("vibration", True),
            ):
                response = await client.post(
                    "/_api/user/update_pref", json={"key": key, "value": value}
                )
                assert response.status_code == 200, response.text
                assert response.json()["data"] == {"key": key, "value": value}
                expected[key] = value
                await user.refresh_from_db()
                assert user.preferences == expected
                for active in app.shared_ctx.sessions.values():
                    if active.id == user.id:
                        assert active.preferences == expected
                    else:
                        assert active.preferences == {"watch_records": 7}
            await other.refresh_from_db()
            assert other.preferences == {"watch_records": 7}

            # permanent records still support explicit, user-scoped deletion
            history = await UserHistory.create(
                user=user, rel_type=HistoryType.SEARCH, rel_id=0, keyword="Old query"
            )
            await UserHistory.filter(id=history.id).update(
                updated_at=now - timedelta(days=1000)
            )
            other_history = await UserHistory.create(
                user=other, rel_type=HistoryType.SEARCH, rel_id=0, keyword="Other query"
            )
            response = await client.get(
                "/_api/user/history/list", params={"rel_type": "search"}
            )
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["data"]["items"]] == [
                history.id
            ]
            response = await client.post(
                "/_api/user/history/delete",
                json={"ids": [history.id, other_history.id]},
            )
            assert response.status_code == 204, response.text
            assert not await UserHistory.filter(id=history.id).exists()
            assert await UserHistory.filter(id=other_history.id).exists()

            # logging in again reads the saved values rather than the defaults
            response = await client.post(
                "/_api/auth/login",
                data={"username": user.username, "password": "test-password"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["data"]["user"]["preferences"] == expected
            assert UserService.DEFAULT_PREFERENCES["watch_records"] == -1
            assert UserService.DEFAULT_PREFERENCES["search_records"] == 3

    asyncio.run(run())
