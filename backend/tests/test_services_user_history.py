"""Tests for reading positions in the shared user history API."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.models.media import (
    EpubContent,
    ImageContent,
    IndexState,
    LibType,
    MediaContentQuery,
    MediaFormat,
    MediaItem,
    MediaLib,
    TextContent,
)
from app.models.user import (
    HistoryEntry,
    HistoryType,
    PermType,
    User,
    UserHistory,
    UserPermission,
    UserRole,
)
from app.services import media as media_service
from app.services.media import MediaItemService
from app.services.user import UserHistoryService
from app.utils.crypto import encrypt
from tests.test_services_media_reading import (
    _database,
    _indexed_collection,
    _indexed_comic,
    _indexed_epub,
    _indexed_text,
    _user,
)
from tests.test_services_media_reading import workspace as workspace
from tests.test_services_user import _client


async def _login(client: httpx.AsyncClient, lib_id: int, name: str = "Reader") -> User:
    """Log in an ordinary user with one library grant.

    Args:
        client: The isolated HTTP client.
        lib_id: The library to grant to this user.
        name: The account name, defaulting to Reader.

    Returns:
        The stored user owning the client's session.
    """
    user = await User.create(
        username=name, password=encrypt("test-password"), role=UserRole.USER
    )
    await UserPermission.create(
        user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=lib_id
    )
    response = await client.post(
        "/_api/auth/login", data={"username": name, "password": "test-password"}
    )
    assert response.status_code == 200
    return user


async def _entry(item: MediaItem) -> dict[str, Any]:
    """Build a position from the same published content consumed by the reader.

    Args:
        item: The indexed novel, standalone comic or comic child.

    Returns:
        The history request with an actual paragraph, block or page identity.
    """
    content = await MediaItemService.get_content(item.id, _user(), MediaContentQuery())
    locator: dict[str, Any] = {"version": content.version, "offset": 0.25}
    if isinstance(content, ImageContent):
        locator["page_id"] = content.images[0].split("/assets/")[1].split("?")[0]
        locator["chapter_item_id"] = item.id if item.parent_id is not None else None
    else:
        locator["chapter_id"] = content.chapter_id
        if isinstance(content, TextContent):
            locator["paragraph"] = 0
        else:
            assert isinstance(content, EpubContent)
            locator["block_id"] = content.blocks[0].id
    return {
        "rel_type": "image" if isinstance(content, ImageContent) else "text",
        "rel_id": item.parent_id or item.id,
        "percentage": 25,
        "locator": locator,
    }


@pytest.mark.parametrize(
    "locator",
    [
        {"chapter_id": "a" * 32, "paragraph": 0},
        {"chapter_id": "a" * 32, "block_id": "b" * 32},
        {"page_id": "c" * 32},
        {"page_id": "c" * 32, "chapter_item_id": 2},
    ],
)
@pytest.mark.parametrize("offset", [0, 0.5, 1])
def test_locator_valid(locator, offset):
    rel_type = HistoryType.IMAGE if "page_id" in locator else HistoryType.TEXT
    entry = HistoryEntry.model_validate(
        {
            "rel_type": rel_type,
            "rel_id": 1,
            "locator": {**locator, "version": "d" * 64, "offset": offset},
        }
    )
    assert entry.locator is not None and entry.locator.offset == offset


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "../cache"},
        {"version": "a" * 63},
        {"chapter_id": "../../book.txt"},
        {"chapter_id": "item:1"},
        {"paragraph": -1},
        {"paragraph": True},
        {"paragraph": "0"},
        {"paragraph": None},
        {"block_id": "b" * 32},
        {"offset": -0.1},
        {"offset": 1.1},
        {"offset": True},
        {"offset": "0.5"},
        {"offset": float("nan")},
        {"schema_version": 1},
        {"path": "/book.txt"},
    ],
)
def test_locator_invalid(changes):
    with pytest.raises(ValidationError):
        HistoryEntry.model_validate(
            {
                "rel_type": "text",
                "rel_id": 1,
                "locator": {
                    "version": "a" * 64,
                    "chapter_id": "b" * 32,
                    "paragraph": 0,
                    **changes,
                },
            }
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"rel_type": "video"},
        {"rel_type": "search"},
        {"rel_type": "image"},
        {"rel_id": 0},
        {"position": 0},
        {"keyword": "Book"},
        {"percentage": 101},
        {"percentage": -1},
    ],
)
def test_entry_invalid(changes):
    with pytest.raises(ValidationError):
        HistoryEntry.model_validate(
            {
                "rel_type": "text",
                "rel_id": 1,
                "locator": {
                    "version": "a" * 64,
                    "chapter_id": "b" * 32,
                    "paragraph": 0,
                },
                **changes,
            }
        )


@pytest.mark.parametrize("format", list(MediaFormat))
def test_reading_history_http(tmp_path, monkeypatch, format):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = (
                await _indexed_text(tmp_path)
                if format == MediaFormat.TXT
                else await _indexed_epub(tmp_path)
                if format == MediaFormat.EPUB
                else await _indexed_comic(tmp_path, format)
            )
            owner = await _login(client, item.lib_id)
            entry = await _entry(item)
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == 204
            saved = await UserHistory.get(user_id=owner.id)
            assert saved.position is None and saved.keyword is None
            assert saved.rel_id == item.id and saved.percentage == 25
            assert saved.repetitions == 0

            entry["locator"]["offset"] = 0.75
            entry["percentage"] = 75
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == 204
            updated = await UserHistory.get(user_id=owner.id)
            assert updated.id == saved.id and updated.repetitions == 1
            assert updated.updated_at is not None and saved.updated_at is not None
            assert updated.updated_at >= saved.updated_at
            assert updated.locator is not None and updated.locator["offset"] == 0.75
            assert updated.percentage == 75

            response = await client.get(
                "/_api/user/history/list",
                params={"rel_type": entry["rel_type"], "rel_id": item.id},
            )
            assert response.status_code == 200
            page = response.json()["data"]
            assert page["total"] == 1
            assert page["items"][0]["locator"] == updated.locator
            assert page["items"][0]["media"]["id"] == item.id

            # another login sees the same server-side position
            response = await client.post(
                "/_api/auth/login",
                data={"username": owner.username, "password": "test-password"},
            )
            assert response.status_code == 200
            response = await client.get(
                "/_api/user/history/list", params={"rel_type": entry["rel_type"]}
            )
            assert response.json()["data"]["items"][0]["id"] == saved.id

            # account isolation applies to queries, writes and deletion
            other = await _login(client, item.lib_id, "Other")
            response = await client.get(
                "/_api/user/history/list", params={"rel_type": entry["rel_type"]}
            )
            assert response.json()["data"]["items"] == []
            response = await client.post(
                "/_api/user/history/delete", json={"ids": [saved.id]}
            )
            assert response.status_code == 204
            assert await UserHistory.filter(id=saved.id).exists()
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == 204
            theirs = await UserHistory.get(user_id=other.id)
            assert theirs.id != saved.id
            response = await client.post(
                "/_api/user/history/delete", json={"ids": [theirs.id]}
            )
            assert response.status_code == 204
            assert await UserHistory.all().count() == 1

    asyncio.run(run())


def test_comic_chapter_history(tmp_path, monkeypatch):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            work, chapters = await _indexed_collection(tmp_path)
            owner = await _login(client, work.lib_id)
            history_id = None
            for repeats, chapter in enumerate(chapters.values()):
                entry = await _entry(chapter)
                response = await client.post("/_api/user/history/record", json=entry)
                assert response.status_code == 204
                assert await UserHistory.all().count() == 1
                saved = await UserHistory.get(user_id=owner.id)
                if history_id is None:
                    history_id = saved.id
                assert saved.id == history_id and saved.rel_id == work.id
                assert saved.repetitions == repeats
                assert saved.locator is not None
                assert saved.locator["chapter_item_id"] == chapter.id
                response = await client.get(
                    "/_api/user/history/list",
                    params={"rel_type": "image", "rel_id": work.id},
                )
                assert response.json()["data"]["items"][0]["locator"] == saved.locator
            response = await client.get(
                "/_api/user/history/list",
                params={"rel_type": "image", "rel_id": chapters[1].id},
            )
            assert response.json()["data"]["items"] == []

    asyncio.run(run())


@pytest.mark.parametrize("case", ["chapter", "anchor", "version", "format"])
@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.EPUB, MediaFormat.DIR])
def test_history_bad_anchor(tmp_path, monkeypatch, case, format):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = (
                await _indexed_text(tmp_path)
                if format == MediaFormat.TXT
                else await _indexed_epub(tmp_path)
                if format == MediaFormat.EPUB
                else await _indexed_comic(tmp_path, format)
            )
            await _login(client, item.lib_id)
            entry = await _entry(item)
            locator = entry["locator"]
            status = 404
            if case == "version":
                locator["version"] = "0" * 64
                status = 409
            elif case == "format":
                if format == MediaFormat.DIR:
                    entry["rel_type"] = "text"
                    locator.pop("page_id")
                    locator.pop("chapter_item_id")
                    locator.update(chapter_id="a" * 32, paragraph=0)
                    status = 400
                elif format == MediaFormat.TXT:
                    locator.pop("paragraph")
                    locator["block_id"] = "a" * 32
                else:
                    locator.pop("block_id")
                    locator["paragraph"] = 0
            elif case == "chapter":
                if format == MediaFormat.DIR:
                    locator["chapter_item_id"] = item.id
                    status = 400
                else:
                    locator["chapter_id"] = "0" * 32
            elif format == MediaFormat.TXT:
                locator["paragraph"] = 10000
            else:
                locator["block_id" if format == MediaFormat.EPUB else "page_id"] = (
                    "0" * 32
                )
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == status, response.text
            assert await UserHistory.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "case", ["child_id", "foreign_child", "missing_child", "no_child"]
)
def test_history_chapter_scope(tmp_path, monkeypatch, case):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            work, chapters = await _indexed_collection(tmp_path / "one")
            await MediaLib.filter(id=work.lib_id).update(priority=2, name="First")
            other, theirs = await _indexed_collection(tmp_path / "two")
            owner = await _login(client, work.lib_id)
            await UserPermission.create(
                user_id=owner.id, rel_type=PermType.MEDIA_LIB, rel_id=other.lib_id
            )
            entry = await _entry(chapters[1])
            status = 400
            if case == "child_id":
                entry["rel_id"] = chapters[1].id
            elif case == "foreign_child":
                entry["locator"] = (await _entry(theirs[1]))["locator"]
            elif case == "missing_child":
                entry["locator"]["chapter_item_id"] = 999
                status = 404
            else:
                entry["locator"].pop("chapter_item_id")
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == status, response.text
            assert await UserHistory.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("case", ["hidden", "denied", "missing", "pending", "changed"])
def test_history_access(tmp_path, monkeypatch, case):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = await _indexed_text(tmp_path)
            owner = await _login(client, item.lib_id)
            entry = await _entry(item)
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == 204
            saved = await UserHistory.get(user_id=owner.id)
            status = 409
            if case == "hidden":
                await MediaItem.filter(id=item.id).update(visible=False)
                status = 404
            elif case == "denied":
                await UserPermission.filter(user_id=owner.id).delete()
                status = 403
            elif case == "missing":
                Path(item.path).unlink()
                status = 503
            elif case == "pending":
                await MediaItem.filter(id=item.id).update(
                    index_state=IndexState.PENDING
                )
            else:
                Path(item.path).write_text("Chapter 1\n\nReplacement body")
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == status, response.text
            response = await client.get(
                "/_api/user/history/list",
                params={"rel_type": "text", "rel_id": item.id},
            )
            assert response.status_code == 200
            page = response.json()["data"]
            if case in ("hidden", "denied"):
                assert page["total"] == 0 and page["items"] == []
            else:
                assert page["total"] == 1
                assert page["items"][0]["locator"] is None
                assert page["items"][0]["percentage"] is None
            await saved.refresh_from_db()
            assert saved.locator is not None and saved.repetitions == 0

    asyncio.run(run())


def test_history_pagination(tmp_path, monkeypatch):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            lib = await MediaLib.create(
                name="Books",
                dir=str(tmp_path / "Books"),
                lib_type=LibType.NOVEL,
                priority=1,
            )
            private = await MediaLib.create(
                name="Private",
                dir=str(tmp_path / "Private"),
                lib_type=LibType.COMIC,
                priority=2,
            )
            owner = await _login(client, lib.id)
            ids = []
            for number in range(6):
                item = await MediaItem.create(
                    lib=private if number == 0 else lib,
                    parent_id=ids[0] if number == 1 else None,
                    path=f"{lib.dir}/Book{number}/book.txt",
                    dir=f"{lib.dir}/Book{number}",
                    name=str(number),
                    visible=number != 2,
                    format=MediaFormat.TXT,
                )
                ids.append(item.id)
                await UserHistory.create(
                    user_id=owner.id, rel_type=HistoryType.TEXT, rel_id=item.id
                )
            for page_num, expected in [(1, [ids[5], ids[4]]), (2, [ids[3]]), (3, [])]:
                response = await client.get(
                    "/_api/user/history/list",
                    params={"rel_type": "text", "page_num": page_num, "page_size": 2},
                )
                assert response.status_code == 200
                page = response.json()["data"]
                assert page["total"] == 3
                assert [item["rel_id"] for item in page["items"]] == expected
            response = await client.get(
                "/_api/user/history/list", params={"rel_type": "text", "rel_id": ids[0]}
            )
            assert response.json()["data"]["items"] == []

    asyncio.run(run())


@pytest.mark.parametrize("case", ["hidden", "moved", "foreign_library", "corrupt"])
def test_history_stale_chapter(tmp_path, monkeypatch, case):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            work, chapters = await _indexed_collection(tmp_path)
            owner = await _login(client, work.lib_id)
            entry = await _entry(chapters[1])
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == 204
            if case == "hidden":
                await MediaItem.filter(id=chapters[1].id).update(visible=False)
            elif case == "moved":
                await MediaItem.filter(id=chapters[1].id).update(parent_id=None)
            elif case == "foreign_library":
                other = await MediaLib.create(
                    name="Other",
                    dir=str(tmp_path / "Other"),
                    lib_type=LibType.COMIC,
                    priority=2,
                )
                await MediaItem.filter(id=chapters[1].id).update(lib=other)
            else:
                await UserHistory.filter(user_id=owner.id).update(
                    locator={"chapter_item_id": True}
                )
            before = (await UserHistory.get(user_id=owner.id)).locator
            response = await client.get(
                "/_api/user/history/list",
                params={"rel_type": "image", "rel_id": work.id},
            )
            assert response.status_code == 200
            row = response.json()["data"]["items"][0]
            assert row["media"]["id"] == work.id
            assert row["locator"] is None and row["percentage"] is None
            assert (await UserHistory.get(user_id=owner.id)).locator == before

    asyncio.run(run())


def test_history_concurrent(tmp_path):
    async def run():
        async with _database():
            work, chapters = await _indexed_collection(tmp_path)
            session = _user([work.lib_id])
            await User.create(
                id=session.id, username="Reader", password="unused", role=UserRole.USER
            )
            await UserPermission.create(
                user_id=session.id, rel_type=PermType.MEDIA_LIB, rel_id=work.lib_id
            )
            entries = [
                HistoryEntry.model_validate(await _entry(chapter))
                for chapter in chapters.values()
            ]
            results = await asyncio.gather(
                *(UserHistoryService.record(session, entry) for entry in entries)
            )
            assert all(result is not None for result in results)
            saved = await UserHistory.get(user_id=session.id)
            assert saved.repetitions == len(entries) - 1
            assert saved.locator in [
                entry.locator.model_dump(exclude_none=True)
                for entry in entries
                if entry.locator
            ]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["permission", "version", "hidden"])
def test_history_save_race(tmp_path, monkeypatch, change):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = await _indexed_text(tmp_path)
            owner = await _login(client, item.lib_id)
            entry = await _entry(item)
            original = media_service.to_thread

            async def changed(func, *args, **kwargs):
                result = await original(func, *args, **kwargs)
                if func is media_service._check_locator:
                    if change == "permission":
                        await UserPermission.filter(user_id=owner.id).delete()
                    elif change == "version":
                        await MediaItem.filter(id=item.id).update(
                            index_version="0" * 64
                        )
                    else:
                        await MediaItem.filter(id=item.id).update(visible=False)
                return result

            monkeypatch.setattr(media_service, "to_thread", changed)
            response = await client.post("/_api/user/history/record", json=entry)
            assert (
                response.status_code
                == {"permission": 403, "version": 409, "hidden": 404}[change]
            )
            assert await UserHistory.all().count() == 0

    asyncio.run(run())


def test_history_retention_http(tmp_path, monkeypatch):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = await _indexed_text(tmp_path)
            owner = await _login(client, item.lib_id)
            entry = await _entry(item)
            await User.filter(id=owner.id).update(preferences={"read_records": 0})
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == 204
            assert await UserHistory.all().count() == 0
            await User.filter(id=owner.id).update(preferences={"read_records": -1})
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == 204
            assert await UserHistory.all().count() == 1
            await User.filter(id=owner.id).update(preferences={"read_records": 0})
            response = await client.get(
                "/_api/user/history/list", params={"rel_type": "text"}
            )
            assert response.json()["data"]["items"] == []
            assert await UserHistory.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("change", ["permission", "hidden"])
def test_history_list_race(tmp_path, monkeypatch, change):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = await _indexed_text(tmp_path)
            owner = await _login(client, item.lib_id)
            response = await client.post(
                "/_api/user/history/record", json=await _entry(item)
            )
            assert response.status_code == 204
            original = media_service.to_thread

            async def changed(func, *args, **kwargs):
                result = await original(func, *args, **kwargs)
                if func is media_service._check_locator:
                    if change == "permission":
                        await UserPermission.filter(user_id=owner.id).delete()
                    else:
                        await MediaItem.filter(id=item.id).update(visible=False)
                return result

            monkeypatch.setattr(media_service, "to_thread", changed)
            response = await client.get(
                "/_api/user/history/list", params={"rel_type": "text"}
            )
            assert response.status_code == 200
            page = response.json()["data"]
            assert page["items"] == [] and page["total"] == 0
            assert await UserHistory.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("rel_type", ["video", "search"])
def test_history_legacy_http(tmp_path, monkeypatch, rel_type):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = await _indexed_text(tmp_path)
            owner = await _login(client, item.lib_id)
            entry = {
                "rel_type": rel_type,
                "rel_id": item.id if rel_type == "video" else 0,
            }
            if rel_type == "video":
                entry.update(position=35, percentage=50)
            else:
                entry["keyword"] = "  Book  "
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == 204
            response = await client.get(
                "/_api/user/history/list",
                params={"rel_type": rel_type, "rel_id": entry["rel_id"]},
            )
            assert response.status_code == 200
            page = response.json()["data"]
            assert page["total"] == 1
            row = page["items"][0]
            assert row["locator"] is None
            if rel_type == "video":
                assert row["media"]["id"] == item.id
                assert row["position"] == 35 and row["percentage"] == 50
            else:
                assert row["keyword"] == "Book" and "graph" not in row
            response = await client.post(
                "/_api/user/history/delete", json={"ids": [row["id"]]}
            )
            assert response.status_code == 204
            assert not await UserHistory.filter(user_id=owner.id).exists()

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.EPUB, MediaFormat.DIR])
@pytest.mark.parametrize("change", ["version", "anchor"])
def test_history_recover(tmp_path, monkeypatch, format, change):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = (
                await _indexed_text(tmp_path)
                if format == MediaFormat.TXT
                else await _indexed_epub(tmp_path)
                if format == MediaFormat.EPUB
                else await _indexed_comic(tmp_path, format)
            )
            owner = await _login(client, item.lib_id)
            entry = await _entry(item)
            original = entry["locator"].copy()
            if change == "version":
                entry["locator"]["version"] = "0" * 64
            else:
                field = (
                    "paragraph"
                    if format == MediaFormat.TXT
                    else "block_id"
                    if format == MediaFormat.EPUB
                    else "page_id"
                )
                entry["locator"][field] = 99999 if field == "paragraph" else "0" * 32
            saved = await UserHistory.create(user_id=owner.id, **entry)
            response = await client.get(
                "/_api/user/history/list",
                params={"rel_type": entry["rel_type"], "rel_id": item.id},
            )
            assert response.status_code == 200
            row = response.json()["data"]["items"][0]
            expected = {
                key: value for key, value in original.items() if value is not None
            }
            expected["offset"] = 0
            assert row["locator"] == expected
            assert row["percentage"] is None
            await saved.refresh_from_db()
            assert saved.locator == entry["locator"] and saved.percentage == 25
            # restoring does not relax the write contract
            response = await client.post("/_api/user/history/record", json=entry)
            assert response.status_code == (409 if change == "version" else 404)

    asyncio.run(run())


def test_history_recover_chapter(tmp_path, monkeypatch):
    async def run():
        async with _database(), _client(monkeypatch) as (client, _):
            item = await _indexed_text(tmp_path)
            owner = await _login(client, item.lib_id)
            content = await MediaItemService.get_content(
                item.id, _user(), MediaContentQuery()
            )
            entry = await _entry(item)
            entry["locator"]["chapter_id"] = content.chapters[1].id
            saved = await UserHistory.create(user_id=owner.id, **entry)
            body = Path(item.path)
            body.write_text(body.read_text() + "\n\nAppended paragraph")
            current = await MediaItemService.index_content(item.id)
            response = await client.get(
                "/_api/user/history/list",
                params={"rel_type": "text", "rel_id": item.id},
            )
            row = response.json()["data"]["items"][0]
            assert row["locator"] == {
                "chapter_id": content.chapters[1].id,
                "version": current.index_version,
                "paragraph": 0,
                "offset": 0,
            }
            assert row["percentage"] is None
            body.write_text("Chapter 1\n\nOnly chapter")
            await MediaItemService.index_content(item.id)
            response = await client.get(
                "/_api/user/history/list",
                params={"rel_type": "text", "rel_id": item.id},
            )
            row = response.json()["data"]["items"][0]
            assert row["locator"] is None and row["percentage"] is None
            await saved.refresh_from_db()
            assert saved.locator == entry["locator"]

    asyncio.run(run())
