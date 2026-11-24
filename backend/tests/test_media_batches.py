"""Unit tests for reviewed batches of library files and items."""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sanic import Sanic
from tortoise.exceptions import OperationalError

from app.core.ai import tools
from app.core.config import KaloscopeConfig
from app.core.dl.driver import DownloadState
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.media import organizer
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.handlers.reading import ReadingSource
from app.core.media.organizer import recover_organizing
from app.models.ai import AIProviderPreset
from app.models.assistant import ConversationCreate
from app.models.base import Pageable
from app.models.download import Downloader, DownloadTask
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import (
    LibType,
    MediaBatch,
    MediaBatchEntry,
    MediaChangeApply,
    MediaChangeReceipt,
    MediaEvent,
    MediaFormat,
    MediaItem,
    MediaLib,
    NFOType,
)
from app.models.user import User, UserHistory, UserInfo, UserRole
from app.services import media_batches as service
from app.services.mcp import MCPAccessTokenService
from app.services.media import MediaItemService
from app.services.media_batches import MediaBatchService
from app.services.media_changes import MediaChangeService
from app.utils.json import dumps
from tests.helpers import memory_db
from tests.test_core_media_reading_organizer import _source as reading_source
from tests.test_core_media_reading_organizer import _work as reading_work
from tests.test_download_plan_changes import latest_request
from tests.test_library_config import MOVIE_TEMPLATE, TV_TEMPLATE
from tests.test_mcp import application, call
from tests.test_media_changes import (
    MODEL,
    Sessions,
    media,
    monitor_for,
    store,
    write_nfo,
)
from tests.test_services_assistant import configure
from tests.test_services_media_reading import _client, _user
from tests.test_services_user_history import _entry
from tests.test_workflow_runs import converse, names, scripted

ADMIN = cast(UserInfo, SimpleNamespace(id=1, role=UserRole.ADMIN, perms=None))
USER = cast(UserInfo, SimpleNamespace(id=2, role=UserRole.USER, perms=None))


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Keep locks and deletion inside the test directory.

    Args:
        tmp_path: Isolated filesystem root.
        monkeypatch: Fixture restoring application configuration.
    """
    cache = tmp_path / "workspace"
    cache.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(cache))
    monkeypatch.setattr(
        KaloscopeConfig, "get", lambda: SimpleNamespace(filesystem_trash_mode=False)
    )


def write(root: Path, *names: str):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)


async def show(tmp_path: Path) -> tuple[MediaLib, MediaItem, list[MediaItem]]:
    """Create a TV library with one indexed show, two episodes and leftovers.

    Args:
        tmp_path: Isolated filesystem root.

    Returns:
        The library, the show's directory item and its episodes.
    """
    root = tmp_path / "tv"
    folder = root / "Show"
    write(
        root,
        "Show/Show S01E01.mkv",
        "Show/Show S01E01.nfo",
        "Show/Show S01E02.mkv",
        "Show/Show.nfo",
        "Show/info.url",
        "Show/Sample/sample.url",
        "Show/Sample/readme.txt",
        "stray.url",
    )
    lib = await MediaLib.create(
        lib_type=LibType.TV_SHOW, dir=str(root), name="TV", priority=1
    )
    parent = await MediaItem.create(
        lib=lib,
        dir=str(folder),
        path=str(folder),
        name="Show",
        nfo_path=str(folder / "Show.nfo"),
    )
    episodes = [
        await MediaItem.create(
            lib=lib,
            parent=parent,
            dir=str(folder),
            path=str(folder / f"Show S01E0{number}.mkv"),
            name=f"Show S01E0{number}",
            size=10,
        )
        for number in (1, 2)
    ]
    return lib, parent, episodes


def deletion(lib: MediaLib, **fields) -> MediaBatch:
    return MediaBatch(library_id=lib.id, operation="delete", **fields)


async def confirm(review: dict, change: MediaBatch, user: UserInfo = ADMIN) -> dict:
    return await MediaChangeService.apply_change(
        MediaChangeApply(
            request_id=review["request_id"],
            change=change,
            revision=review["revision"],
            confirmed=True,
        ),
        user,
    )


def test_listing(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)

            result = await MediaBatchService.review(
                MediaBatch(library_id=lib.id, extensions=["url", "nfo", "mkv"]), ADMIN
            )

            assert result["matched"] == {"files": 7, "bytes": 70}
            assert result["by_owner"] == {"loose": 3, "companion": 2, "indexed": 2}
            assert result["by_extension"] == {".url": 3, ".nfo": 2, ".mkv": 2}
            assert "request_id" not in result
            assert result["sample"][0] == {
                "path": "Show/Sample/sample.url",
                "size": 10,
                "owner": "loose",
            }
            assert not await MediaChangeReceipt.all().count()

    asyncio.run(run())


def test_delete_loose(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            root = Path(lib.dir)
            change = MediaBatch(
                library_id=lib.id, operation="delete", extensions=["url", "txt", "nfo"]
            )

            review = await MediaBatchService.review(change, ADMIN)

            assert review["valid"]
            assert review["planned"]["units"] == 4
            assert review["blocked"] == {"units": 2, "reasons": {"owner_excluded": 2}}
            assert review["deletion"] == "permanent"
            assert review["sample"][0]["blocked"] == "owner_excluded"
            assert (root / "stray.url").exists()

            result = await confirm(review, change)

            assert result["outcome"] == "completed"
            assert result["applied"] is True
            assert (result["done"], result["remaining"]) == (4, 0)
            assert not (root / "stray.url").exists()
            # the directory the batch emptied is removed, companions and videos stay
            assert not (root / "Show/Sample").exists()
            assert sorted(path.name for path in (root / "Show").iterdir()) == [
                "Show S01E01.mkv",
                "Show S01E01.nfo",
                "Show S01E02.mkv",
                "Show.nfo",
            ]
            assert await MediaItem.all().count() == 3

            again = await confirm(review, change)

            assert again["replayed"] is True
            assert again["outcome"] == "completed"

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.MOVIE, LibType.TV_SHOW])
def test_delete_last_child(tmp_path, lib_type):
    async def run():
        async with memory_db():
            lib, parent, episodes = await show(tmp_path)
            lib.lib_type = lib_type
            await lib.save(update_fields=["lib_type"])
            root = Path(lib.dir)
            write(root, "Show/poster.jpg")
            Path(episodes[1].path).unlink()
            await episodes[1].delete()
            change = deletion(lib, item_ids=[episodes[0].id])

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change, ADMIN)

            assert result["done"] == 1
            assert not Path(episodes[0].path).exists()
            assert not Path(episodes[0].path).with_suffix(".nfo").exists()
            assert not await MediaItem.exists(id=parent.id)
            assert not Path(parent.nfo_path).exists()
            assert not (root / "Show/poster.jpg").exists()
            assert (root / "Show/info.url").read_bytes() == b"x" * 10
            assert review["planned"]["files"] == 4
            assert {row["src"] for row in review["sample"]} >= {
                "Show",
                "Show/Show.nfo",
                "Show/poster.jpg",
            }

    asyncio.run(run())


def test_delete_items(tmp_path):
    async def run():
        async with memory_db():
            lib, parent, episodes = await show(tmp_path)
            root = Path(lib.dir)
            change = MediaBatch(
                library_id=lib.id, operation="delete", item_ids=[episodes[0].id]
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)

            assert review["planned"]["by_owner"] == {"indexed": 1}
            assert result["outcome"] == "completed"
            assert not (root / "Show/Show S01E01.mkv").exists()
            assert not (root / "Show/Show S01E01.nfo").exists()
            assert (root / "Show/Show S01E02.mkv").exists()
            assert not await MediaItem.exists(id=episodes[0].id)
            assert await MediaItem.exists(id=parent.id)

    asyncio.run(run())


def test_delete_with_companions(tmp_path):
    async def run():
        async with memory_db():
            lib, parent, episodes = await show(tmp_path)
            change = deletion(
                lib,
                directory="Show",
                name="Show S01E01.*",
                owners=["indexed", "companion"],
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)

            assert review["planned"]["units"] == 1
            assert review["blocked"]["reasons"] == {"follows_item": 1}
            assert result["outcome"] == "completed"
            assert result["done"] == 1 and result["skipped"] == 0
            assert not Path(episodes[0].path).exists()
            assert not Path(episodes[0].path).with_suffix(".nfo").exists()
            assert Path(episodes[1].path).exists()
            assert Path(parent.nfo_path).exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("last", "unit", "children"),
    [
        (False, "Show/Show S01E01.mkv", ["Show/Show S01E01.nfo"]),
        (
            True,
            "Show",
            ["Show/Show.nfo", "Show/Show S01E01.mkv", "Show/Show S01E01.nfo"],
        ),
    ],
)
def test_units(tmp_path, last, unit, children):
    async def run():
        async with memory_db():
            lib, _, episodes = await show(tmp_path)
            if last:
                Path(episodes[1].path).unlink()
                await episodes[1].delete()
                change = deletion(lib, item_ids=[episodes[0].id])
            else:
                change = deletion(
                    lib,
                    directory="Show",
                    name="Show S01E01.*",
                    owners=["indexed", "companion"],
                )
            review = await MediaBatchService.review(change, ADMIN)
            batch_id = review["batch_id"]
            page = Pageable(page_num=1, page_size=50)

            grouped = await MediaBatchService.units(batch_id, page)
            blocked = await MediaBatchService.units(batch_id, page, "blocked")
            # a file whose unit cannot be found stays visible on its own
            await MediaBatchEntry.filter(receipt_id=batch_id, src=children[0]).update(
                item_id=None, src="stray.nfo"
            )
            orphaned = await MediaBatchService.units(batch_id, page, "blocked")

            assert grouped["total"] == 1
            assert grouped["items"][0]["src"] == unit
            assert [row["src"] for row in grouped["items"][0]["children"]] == children
            assert blocked == {"total": 0, "items": []}
            assert [row["src"] for row in orphaned["items"]] == ["stray.nfo"]
            assert orphaned["items"][0]["children"] == []

    asyncio.run(run())


@pytest.mark.parametrize("missing", [False, True])
def test_hide_items(tmp_path, missing):
    async def run():
        async with memory_db():
            lib, parent, episodes = await show(tmp_path)
            if missing:
                Path(episodes[0].path).unlink()
            change = deletion(lib, item_ids=[episodes[0].id], delete_files=False)

            response = await tools.execute_tool(
                "review_media_batch",
                change.model_dump_json(exclude_unset=True),
                ADMIN,
                conversation_id=1,
                call_id="keep-files",
            )
            review = response["data"]
            details = await MediaBatchService.details(review["batch_id"])
            result = await confirm(review, change)
            hidden = await MediaItem.get(id=episodes[0].id)

            assert review["planned"]["units"] == 1
            assert review["deletion"] is None
            assert review["delete_files"] is False
            assert details["hideable"] is True and details["delete_files"] is False
            assert result["outcome"] == "completed"
            assert result["delete_files"] is False
            assert not hidden.visible
            assert (await MediaItem.get(id=episodes[1].id)).visible
            assert (await MediaItem.get(id=parent.id)).visible
            assert Path(episodes[0].path).exists() is not missing
            assert Path(episodes[0].path).with_suffix(".nfo").exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "fields",
    [
        {"extensions": ["url"]},
        {"directory": "Show", "empty_dirs": True},
        {"operation": "rename", "pattern": "a"},
    ],
)
def test_hide_needs_items(fields):
    fields = {"operation": "delete", "item_ids": [], **fields}

    with pytest.raises(ValueError, match="delete_files"):
        MediaBatch(library_id=1, delete_files=False, **fields)


def test_indexed_by_filter(tmp_path):
    async def run():
        async with memory_db():
            lib, parent, _ = await show(tmp_path)
            change = MediaBatch(
                library_id=lib.id,
                operation="delete",
                extensions=["mkv"],
                owners=["indexed"],
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)

            assert review["planned"]["units"] == 1
            assert result["outcome"] == "completed"
            assert not await MediaItem.exists(id=parent.id)
            assert not Path(parent.nfo_path).exists()
            assert not list(Path(lib.dir).rglob("*.mkv"))

    asyncio.run(run())


@pytest.mark.parametrize(
    "name", ["extra.mkv", "extra.TS", "extra.m2ts", "extra.iso", "Extras/extra.mp4"]
)
@pytest.mark.parametrize("selection", ["children", "parent"])
def test_parent_unindexed(tmp_path, name, selection):
    async def run():
        async with memory_db():
            lib, parent, episodes = await show(tmp_path)
            root = Path(lib.dir)
            write(root, f"Show/{name}", "Show/poster.jpg")
            change = deletion(
                lib,
                item_ids=[parent.id]
                if selection == "parent"
                else [item.id for item in episodes],
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)

            assert review["planned"]["units"] == 2
            assert result["outcome"] == "completed"
            assert await MediaItem.exists(id=parent.id)
            assert (root / "Show" / name).read_bytes() == b"x" * 10
            assert Path(parent.nfo_path).read_bytes() == b"x" * 10
            assert (root / "Show/poster.jpg").read_bytes() == b"x" * 10
            assert not any(Path(item.path).exists() for item in episodes)

    asyncio.run(run())


@pytest.mark.parametrize("edit", ["unindexed", "body", "metadata", "child"])
@pytest.mark.parametrize("selection", ["children", "parent"])
def test_parent_changed(tmp_path, edit, selection):
    async def run():
        async with memory_db():
            lib, parent, episodes = await show(tmp_path)
            root = Path(lib.dir)
            change = deletion(
                lib,
                item_ids=[parent.id]
                if selection == "parent"
                else [item.id for item in episodes],
            )
            review = await MediaBatchService.review(change, ADMIN)

            if edit == "unindexed":
                write(root, "Show/late.mkv")
            elif edit == "body":
                Path(episodes[0].path).write_bytes(b"changed body")
            elif edit == "metadata":
                Path(parent.nfo_path).write_bytes(b"changed metadata")
            else:
                write(root, "Show/late.mkv")
                await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(root / "Show/late.mkv"),
                    dir=parent.dir,
                    name="late",
                )

            result = await confirm(review, change)

            assert result["done"] == 0
            assert result["reasons"] == {"content_changed": 1}
            assert await MediaItem.exists(id=parent.id)
            assert Path(parent.nfo_path).exists()
            assert all(Path(item.path).exists() for item in episodes)

    asyncio.run(run())


def test_changed_file_skipped(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            root = Path(lib.dir)
            change = MediaBatch(
                library_id=lib.id, operation="delete", extensions=["url"]
            )
            review = await MediaBatchService.review(change, ADMIN)
            (root / "stray.url").write_bytes(b"edited after the review")
            (root / "Show/info.url").unlink()
            (root / "later.url").write_bytes(b"created after the review")

            result = await confirm(review, change)

            assert result["outcome"] == "partial"
            assert (result["done"], result["skipped"]) == (1, 2)
            assert result["reasons"] == {"content_changed": 1, "missing": 1}
            assert (root / "stray.url").exists()
            # a confirmed batch never grows to files it did not show
            assert (root / "later.url").exists()

    asyncio.run(run())


def test_stale_and_conflict(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            change = MediaBatch(
                library_id=lib.id, operation="delete", extensions=["url"]
            )
            review = await MediaBatchService.review(change, ADMIN)
            other = MediaBatch(
                library_id=lib.id, operation="delete", extensions=["txt"]
            )

            conflict = await confirm(review, other)
            unconfirmed = await MediaChangeService.apply_change(
                MediaChangeApply(
                    request_id=review["request_id"],
                    change=change,
                    revision=review["revision"],
                ),
                ADMIN,
            )
            await MediaLib.filter(id=lib.id).update(rename_template="{{ title }}")
            stale = await confirm(review, change)

            assert conflict["conflict"] is True
            assert unconfirmed["confirmation_required"] is True
            assert unconfirmed["planned"]["units"] == 3
            assert stale["stale"] is True
            assert (Path(lib.dir) / "stray.url").exists()

    asyncio.run(run())


def test_invalid_reviews(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)

            nothing = await MediaBatchService.review(
                deletion(lib, extensions=["iso"]),
                ADMIN,
            )
            blocked = await MediaBatchService.review(
                deletion(lib, extensions=["nfo"]),
                ADMIN,
            )
            foreign = await MediaBatchService.review(
                MediaBatch(library_id=lib.id, operation="delete", item_ids=[999]), ADMIN
            )

            assert [nothing["valid"], blocked["valid"], foreign["valid"]] == [False] * 3
            assert "request_id" not in blocked
            assert blocked["issues"][0]["path"] == "owners"
            assert foreign["issues"][0]["path"] == "item_ids"
            with pytest.raises(ForbiddenException):
                await MediaBatchService.review(MediaBatch(library_id=lib.id), USER)
            with pytest.raises(NotFoundException):
                await MediaBatchService.review(MediaBatch(library_id=99), ADMIN)

    asyncio.run(run())


def test_cancel_and_details(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            change = MediaBatch(
                library_id=lib.id, operation="delete", extensions=["url"]
            )
            review = await MediaBatchService.review(change, ADMIN)
            batch_id = review["batch_id"]
            monkeypatch.setattr(service, "FILE_CHUNK", 1)
            original = MediaBatchService._delete_files

            async def stop_after_first(chunk, lib, root):
                outcomes = await original(chunk, lib, root)
                await MediaBatchService.cancel(batch_id)
                return outcomes

            monkeypatch.setattr(
                MediaBatchService, "_delete_files", staticmethod(stop_after_first)
            )

            before = await MediaBatchService.details(batch_id)
            result = await confirm(review, change)
            after = await MediaBatchService.details(batch_id)
            page = await MediaBatchService.entries(
                batch_id, Pageable(page_num=1, page_size=10), "done"
            )

            assert before["state"] == "reviewed"
            assert result["outcome"] == "cancelled"
            assert (result["done"], result["remaining"]) == (1, 2)
            assert after["state"] == "finished"
            assert after["counts"]["done"] == 1
            assert [entry["status"] for entry in page["items"]] == ["done"]
            with pytest.raises(NotFoundException):
                await MediaBatchService.details(batch_id + 1)

    asyncio.run(run())


def test_interrupted(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            root = Path(lib.dir)
            change = MediaBatch(
                library_id=lib.id, operation="delete", extensions=["url"]
            )
            review = await MediaBatchService.review(change, ADMIN)
            # a server stop leaves a claim, no outcome and units marked as started
            await MediaChangeReceipt.filter(id=review["batch_id"]).update(claimed=True)
            entries = await MediaBatchEntry.filter(receipt_id=review["batch_id"])
            await MediaBatchEntry.filter(id__in=[entries[0].id, entries[1].id]).update(
                status="started"
            )
            (root / entries[0].src).unlink()

            result = await confirm(review, change)

            assert result["outcome"] == "interrupted"
            assert result["replayed"] is True
            assert (result["done"], result["remaining"]) == (1, 2)
            assert (root / entries[1].src).exists()

    asyncio.run(run())


def test_retention(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            monkeypatch.setattr(service, "PLAN_KEEP", 2)
            change = MediaBatch(
                library_id=lib.id, operation="delete", extensions=["url"]
            )

            first = await MediaBatchService.review(change, ADMIN)
            await confirm(first, change)
            write(Path(lib.dir), "a.url")
            reviews = [await MediaBatchService.review(change, ADMIN) for _ in range(3)]

            kept = set(await MediaChangeReceipt.all().values_list("id", flat=True))
            # applied batches stay; only the newest unconfirmed plans are kept
            assert kept == {first["batch_id"], *(r["batch_id"] for r in reviews[1:])}
            assert not await MediaBatchEntry.filter(
                receipt_id=reviews[0]["batch_id"]
            ).exists()

    asyncio.run(run())


def test_comic_pages(tmp_path):
    async def run():
        async with memory_db():
            root = tmp_path / "comics"
            write(root, "Pages/001.jpg", "Pages/credit.jpg", "Pages/cover.jpg")
            lib = await MediaLib.create(
                lib_type=LibType.COMIC, dir=str(root), name="Comics", priority=1
            )
            await MediaItem.create(
                lib=lib,
                dir=str(root / "Pages"),
                path=str(root / "Pages"),
                name="Pages",
                format=MediaFormat.DIR,
            )
            default = MediaBatch(
                library_id=lib.id, operation="delete", extensions=["jpg"]
            )
            change = MediaBatch(
                library_id=lib.id, operation="delete", name="*credit*", owners=["page"]
            )
            every = MediaBatch(
                library_id=lib.id, operation="delete", name="0*", owners=["page"]
            )

            protected = await MediaBatchService.review(default, ADMIN)
            emptying = await MediaBatchService.review(
                MediaBatch(
                    library_id=lib.id,
                    operation="delete",
                    extensions=["jpg"],
                    owners=["page"],
                ),
                ADMIN,
            )
            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)
            last = await MediaBatchService.review(every, ADMIN)

            # no image in a comic library is deletable by default
            assert not protected["valid"]
            assert protected["blocked"]["reasons"] == {"owner_excluded": 3}
            assert emptying["blocked"]["reasons"] == {
                "owner_excluded": 1,
                "empties_item": 2,
            }
            assert result["outcome"] == "completed"
            assert sorted(p.name for p in (root / "Pages").iterdir()) == [
                "001.jpg",
                "cover.jpg",
            ]
            # the comic is queued for indexing without waiting for the watcher
            assert await MediaEvent.filter(lib_id=lib.id).values_list(
                "event_type", "src_path"
            ) == [("modified", str(root / "Pages"))]
            assert last["blocked"]["reasons"] == {"empties_item": 1}

    asyncio.run(run())


def test_conversation(monkeypatch, tmp_path):
    streams, offered = [], []

    def offer(body, *calls):
        offered.append(names(body))
        return list(calls)

    selection = {"library_id": 60, "extensions": ["url"]}
    steps = [
        lambda body: offer(body, ("list_media_libraries", {})),
        lambda body: offer(body, ("review_media_batch", selection)),
        lambda body: offer(
            body, ("review_media_batch", {**selection, "operation": "delete"})
        ),
        lambda body: offer(body),
        lambda body: offer(
            body,
            (
                "apply_media_change",
                {"request_id": latest_request(body), "confirmed": True},
            ),
        ),
        lambda body: offer(body),
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            root = Path(env.movies.dir)
            write(root, "a.url", "Extras/b.url", "Extras/c.txt")
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )

            _, reviewed = await converse(
                conversation["id"], env.owner, "Delete the url files.", key="a"
            )
            _, applied = await converse(conversation["id"], env.owner, "Yes.", key="b")

            assert "review_media_batch" not in offered[0]
            assert "review_media_batch" in offered[1]
            # the first batch call narrows the turn, and the next turn starts narrow
            assert offered[2] == offered[3] == offered[4] == tools.BATCH_FOCUS
            assert reviewed[1]["data"]["matched"]["files"] == 2
            assert reviewed[2]["data"]["planned"]["units"] == 2
            assert applied[0]["data"]["outcome"] == "completed"
            assert not (root / "a.url").exists()
            assert (root / "Extras/c.txt").exists()

    asyncio.run(run())


def test_hosted_turn_start(monkeypatch, tmp_path):
    streams, offered = [], []

    def offer(body, *calls):
        offered.append(names(body))
        return list(calls)

    steps = [
        lambda body: offer(
            body,
            (
                "review_media_batch",
                {"library_id": 60, "operation": "delete", "extensions": ["url"]},
            ),
        ),
        lambda body: offer(body),
    ]
    configure(
        monkeypatch,
        scripted(steps, streams),
        preset=AIProviderPreset.DEEPSEEK,
        cloud_budget=True,
    )

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            write(Path(env.movies.dir), "a.url")
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )

            _, reviewed = await converse(
                conversation["id"], env.owner, "Delete the url files.", key="a"
            )

            # a hosted budget offers the batch review before any media is read,
            # without the other reviews or the apply tool
            assert "review_media_batch" in offered[0]
            assert not {"review_library_scan", "apply_media_change"} & offered[0]
            assert reviewed[0]["data"]["planned"]["units"] == 1

    asyncio.run(run())


def test_mcp(monkeypatch, tmp_path):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            stored = await store(tmp_path)
            app.ctx.lib_watcher = monitor_for(stored.movies, stored.shows)
            admin = app.shared_ctx.sessions["admin"]
            write(Path(stored.movies.dir), "a.url", "b.url")
            change = {
                "action": "batch",
                "library_id": 60,
                "operation": "delete",
                "extensions": ["url"],
            }

            with pytest.raises(ForbiddenException):
                await call(
                    app, "review_media_batch", {"change": change}, token="reader"
                )
            listed = await call(
                app,
                "review_media_batch",
                {"change": {"library_id": 60, "extensions": ["url"]}},
                token="admin",
            )
            reviewed = await call(
                app, "review_media_batch", {"change": change}, token="admin"
            )
            data = reviewed["structuredContent"]
            plan = await call(
                app, "get_media_batch", {"batch_id": data["batch_id"]}, token="admin"
            )
            arguments = {
                "application": {
                    "request_id": data["request_id"],
                    "change": change,
                    "revision": data["revision"],
                    "confirmed": True,
                }
            }
            read_only = await call(app, "apply_media_change", arguments, token="admin")

            write_token = (
                await MCPAccessTokenService.create(
                    admin.id,
                    MCPAccessTokenCreate(name="batch", scope=MCPTokenScope.READ_WRITE),
                )
            )["token"]
            applied = await call(
                app, "apply_media_change", arguments, token=write_token
            )
            done = await call(
                app,
                "get_media_batch",
                {"batch_id": data["batch_id"], "status": "done"},
                token="admin",
            )

            refused = await call(
                app, "cancel_media_batch", {"batch_id": data["batch_id"]}, token="admin"
            )
            stopped = await call(
                app,
                "cancel_media_batch",
                {"batch_id": data["batch_id"]},
                token=write_token,
            )

            assert refused["isError"]
            # a finished batch has nothing left to stop
            assert stopped["structuredContent"]["state"] == "finished"
            assert stopped["structuredContent"]["stop_requested"] is False
            assert listed["structuredContent"]["matched"]["files"] == 2
            assert "request_id" not in listed["structuredContent"]
            assert plan["structuredContent"]["state"] == "reviewed"
            assert [e["src"] for e in plan["structuredContent"]["items"]] == [
                "a.url",
                "b.url",
            ]
            assert read_only["isError"]
            assert applied["structuredContent"]["outcome"] == "completed"
            assert done["structuredContent"]["total"] == 2
            assert not (Path(stored.movies.dir) / "a.url").exists()

    asyncio.run(run())


def test_routes(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            review = await MediaBatchService.review(
                deletion(lib, extensions=["url"]), ADMIN
            )
            batch_id = review["batch_id"]

            async with _client(_user()) as client:
                details = await client.get(f"/_api/media/batch/{batch_id}")
                entries = await client.get(
                    f"/_api/media/batch/{batch_id}/entries",
                    params={"status": "planned", "page_size": 2},
                )
                cancelled = await client.post(f"/_api/media/batch/{batch_id}/cancel")
                missing = await client.get(f"/_api/media/batch/{batch_id + 1}")
            async with _client(_user([])) as client:
                denied = await client.get(f"/_api/media/batch/{batch_id}")

            assert details.json()["data"]["planned"]["units"] == 3
            assert entries.json()["data"]["total"] == 3
            assert len(entries.json()["data"]["items"]) == 2
            # a plan that is not running has nothing to stop
            assert cancelled.json()["data"]["stop_requested"] is False
            assert missing.status_code == 404
            assert denied.status_code == 403

    asyncio.run(run())


def test_dialog_confirmation(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            root = Path(lib.dir)
            body = {"library_id": lib.id, "operation": "delete", "extensions": ["url"]}
            other = cast(
                UserInfo, SimpleNamespace(id=7, role=UserRole.ADMIN, perms=None)
            )
            stranger = await MediaBatchService.review(
                deletion(lib, extensions=["txt"]), other
            )

            async with _client(_user()) as client:
                listed = await client.post(
                    "/_api/media/batch/review",
                    json={"library_id": lib.id, "extensions": ["url"]},
                )
                reviewed = await client.post("/_api/media/batch/review", json=body)
                batch_id = reviewed.json()["data"]["batch_id"]
                applied = await client.post(f"/_api/media/batch/{batch_id}/apply")
                again = await client.post(f"/_api/media/batch/{batch_id}/apply")
                foreign = await client.post(
                    f"/_api/media/batch/{stranger['batch_id']}/apply"
                )
            async with _client(_user([])) as client:
                denied = await client.post("/_api/media/batch/review", json=body)

            assert listed.json()["data"]["matched"]["files"] == 3
            assert reviewed.json()["data"]["planned"]["units"] == 3
            assert applied.json()["data"]["outcome"] == "completed"
            # a second click reads the outcome instead of running the plan again
            assert again.json()["data"]["replayed"] is True
            assert not (root / "stray.url").exists()
            # only the account that reviewed a plan may confirm it
            assert foreign.status_code == 403
            assert (root / "Show/Sample/readme.txt").is_file()
            assert denied.status_code == 403

    asyncio.run(run())


def renaming(
    lib: MediaLib, pattern: str, replacement: str = "", **fields
) -> MediaBatch:
    return MediaBatch(
        library_id=lib.id,
        operation="rename",
        pattern=pattern,
        replacement=replacement,
        **fields,
    )


def test_rename_videos(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            folder = Path(env.show.dir)
            write(folder, "S01E01.zh.ass", "S01E01-thumb.jpg", ".S01E01.json")
            change = renaming(env.shows, "^S01", "Old Show S01", item_ids=[env.show.id])

            review = await MediaBatchService.review(change, env.owner)
            before = sorted(path.name for path in folder.iterdir())
            result = await confirm(review, change, env.owner)
            first = await MediaItem.get(id=env.episodes[0].id)

            assert review["planned"]["units"] == 2
            assert review["unchanged"] == 1
            assert review["deletion"] is None
            assert review["sample"][0]["dst"] == "Old Show/Old Show S01E01.mkv"
            assert "S01E01.mkv" in before
            assert result["outcome"] == "completed"
            assert sorted(path.name for path in folder.iterdir()) == [
                ".Old Show S01E01.json",
                "Extra.mkv",
                "Old Show S01E01-thumb.jpg",
                "Old Show S01E01.mkv",
                "Old Show S01E01.nfo",
                "Old Show S01E01.zh.ass",
                "Old Show S01E02.mkv",
                "Old Show.nfo",
            ]
            assert first.name == "Old Show S01E01"
            assert first.path == str(folder / "Old Show S01E01.mkv")
            assert first.nfo_path == str(folder / "Old Show S01E01.nfo")
            assert first.parent_id == env.show.id
            assert (await confirm(review, change, env.owner))["replayed"] is True

    asyncio.run(run())


def test_rename_metadata(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await MediaItem.filter(id=env.show.id).update(title="Real Show")
            await MediaItem.filter(id=env.episodes[0].id).update(
                title="Pilot", season=1, episode=1
            )
            change = renaming(
                env.shows,
                "^.*$",
                "{{ title }} {{ season }}x{{ '%02d' % episode }} {{ episode_title }}",
                item_ids=[env.episodes[0].id],
            )

            review = await MediaBatchService.review(change, env.owner)

            # the show's saved title wins over the one parsed from the file name
            assert review["sample"][0]["dst"] == "Old Show/Real Show 1x01 Pilot.mkv"

    asyncio.run(run())


def test_rename_files(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            root = Path(env.shows.dir)
            write(
                root,
                "[x] a.txt",
                "[y] a.txt",
                "[x] b.txt",
                "b.txt",
                "[x] c.txt",
                "Old Show/S01E01.srt",
            )
            change = renaming(
                env.shows,
                r"^\[\w\] ",
                extensions=["txt", "srt", "nfo"],
                owners=["loose", "companion"],
            )

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)

            assert review["planned"]["units"] == 1
            assert review["blocked"]["reasons"] == {
                "name_conflict": 2,
                "destination_exists": 1,
                "follows_item": 3,
            }
            assert result["outcome"] == "completed"
            assert (root / "c.txt").exists() and not (root / "[x] c.txt").exists()
            assert (root / "[x] a.txt").exists() and (root / "[x] b.txt").exists()

    asyncio.run(run())


def test_rename_refusals(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            invalid = await MediaBatchService.review(
                renaming(env.shows, "(", item_ids=[env.show.id]), env.owner
            )
            emptied = await MediaBatchService.review(
                renaming(env.shows, "^.*$", item_ids=[env.show.id]), env.owner
            )
            await MediaLib.filter(id=env.shows.id).update(rename_template=TV_TEMPLATE)
            templated = await MediaBatchService.review(
                renaming(env.shows, "^S01", "S1", item_ids=[env.show.id]), env.owner
            )

            assert invalid["issues"][0]["path"] == "pattern"
            assert emptied["blocked"]["reasons"] == {"invalid_name": 3}
            assert templated["blocked"]["reasons"] == {"template_managed": 3}
            assert not any(r["valid"] for r in (invalid, emptied, templated))

    asyncio.run(run())


def test_organize(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            root = Path(env.shows.dir)
            change = MediaBatch(library_id=env.shows.id, operation="organize")

            missing = await MediaBatchService.review(change, env.owner)
            await MediaLib.filter(id=env.shows.id).update(rename_template=TV_TEMPLATE)
            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            again = await MediaBatchService.review(change, env.owner)

            assert not missing["valid"]
            assert missing["issues"][0]["path"] == "library_id"
            assert review["planned"]["units"] == 1
            assert review["sample"][0]["dst"] == "Old Show/Season 01"
            assert result["outcome"] == "completed"
            assert (root / "Old Show/Season 01/S01E01 - Pilot.mkv").is_file()
            assert (root / "Old Show/Season 01/S01E02.mkv").is_file()
            # organized works are left out of the next review
            assert not again["valid"]
            assert again["unchanged"] == 1

    asyncio.run(run())


def test_organize_video_nfo(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            root = Path(env.movies.dir)
            folder = root / "alien_dl"
            folder.mkdir()
            (folder / "alien.mkv").write_bytes(b"alien")
            await write_nfo(
                NFOType.MOVIE,
                folder / "alien.nfo",
                {"title": "Alien", "year": 1979, "site": "tmdb", "id": "348"},
            )
            parent = await MediaItem.create(
                lib=env.movies, path=str(folder), dir=str(folder), name="alien_dl"
            )
            await MediaItem.create(
                lib=env.movies,
                parent=parent,
                path=str(folder / "alien.mkv"),
                dir=str(folder),
                name="alien",
                nfo_path=str(folder / "alien.nfo"),
                title="Alien",
                year=1979,
            )
            await MediaLib.filter(id=env.movies.id).update(
                rename_template=MOVIE_TEMPLATE
            )
            change = MediaBatch(
                library_id=env.movies.id, operation="organize", item_ids=[parent.id]
            )

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            moved = await MediaItem.get(id=parent.id)

            # a folder without its own NFO is named from its video's NFO
            assert review["planned"]["units"] == 1
            assert result["outcome"] == "completed"
            assert (root / "Alien (1979)/Alien (1979).mkv").is_file()
            assert (root / "Alien (1979)/Alien (1979).nfo").is_file()
            assert not folder.exists()
            assert (moved.path, moved.title, moved.year) == (
                str(root / "Alien (1979)"),
                "Alien",
                1979,
            )

    asyncio.run(run())


def test_organize_changed(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await MediaLib.filter(id=env.shows.id).update(rename_template=TV_TEMPLATE)
            change = MediaBatch(
                library_id=env.shows.id, operation="organize", item_ids=[env.show.id]
            )
            review = await MediaBatchService.review(change, env.owner)
            nfo = Path(env.episodes[0].nfo_path)
            nfo.write_text(nfo.read_text().replace("Pilot", "Renamed"))

            result = await confirm(review, change, env.owner)

            # the work would no longer move as it was shown, so it stays put
            assert result["outcome"] == "partial"
            assert result["reasons"] == {"content_changed": 1}
            assert (Path(env.show.dir) / "S01E01.mkv").is_file()

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.CBZ, MediaFormat.DIR])
def test_organize_reading(tmp_path, format):
    async def run():
        async with memory_db():
            lib, item = await reading_work(tmp_path, format)
            root = Path(lib.dir)
            change = MediaBatch(library_id=lib.id, operation="organize")

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)
            moved = await MediaItem.get(id=item.id)
            again = await MediaBatchService.review(change, ADMIN)

            assert review["planned"]["units"] == 1
            assert review["sample"][0]["src"].startswith("original")
            # the plan shows where the work itself goes
            assert review["sample"][0]["dst"].startswith("New Work")
            assert result["outcome"] == "completed"
            assert moved.dir == str(root / "New Work")
            assert not (root / "original").exists()
            assert Path(moved.path).exists()
            # the work is queued for indexing under its new place
            assert await MediaEvent.filter(
                event_type="modified", src_path=str(root / "New Work")
            ).exists()
            assert not again["valid"]
            assert again["unchanged"] == 1

    asyncio.run(run())


async def second_folder(env, **fields) -> tuple[MediaItem, list[MediaItem]]:
    """Add another folder of the same show with a new, a duplicate and a later episode.

    Args:
        env: Stored libraries from `media`.
        **fields: Extra fields of the folder's directory item.

    Returns:
        The folder's directory item and its episodes.
    """
    folder = Path(env.shows.dir) / "Old Show Part 2"
    folder.mkdir()
    parent = await MediaItem.create(
        lib=env.shows,
        path=str(folder),
        dir=str(folder),
        name=folder.name,
        season=1,
        **fields,
    )
    episodes = []
    for name, season, episode in (
        ("S01E02", 1, 2),
        ("S01E03", 1, 3),
        ("S02E01", 2, 1),
    ):
        video = folder / f"{name}.mkv"
        video.write_bytes(name.encode())
        episodes.append(
            await MediaItem.create(
                lib=env.shows,
                parent=parent,
                path=str(video),
                dir=str(folder),
                name=name,
                season=season,
                episode=episode,
            )
        )
    write(folder, "S01E03.zh.ass", "poster.jpg")
    return parent, episodes


def moving(lib: MediaLib, destination: str, **fields) -> MediaBatch:
    return MediaBatch(
        library_id=lib.id, operation="move", destination=destination, **fields
    )


def test_merge_episodes(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            parent, episodes = await second_folder(env)
            root = Path(env.shows.dir)
            change = moving(env.shows, "Old Show", item_ids=[parent.id])

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            moved = await MediaItem.get(id=episodes[1].id)

            assert review["planned"]["units"] == 2
            assert review["blocked"]["reasons"] == {"duplicate_episode": 1}
            # both copies are named so the user can drop one before moving again
            assert [
                (row["episode"], row["incoming"]["item_id"], row["existing"]["path"])
                for row in review["duplicates"]
            ] == [(2, episodes[0].id, "Old Show/S01E02.mkv")]
            assert "leftovers" not in review
            assert result["outcome"] == "completed"
            assert (root / "Old Show/S01E03.mkv").is_file()
            assert (root / "Old Show/S01E03.zh.ass").is_file()
            assert (root / "Old Show/S02E01.mkv").is_file()
            assert moved.parent_id == env.show.id
            assert moved.path == str(root / "Old Show/S01E03.mkv")
            # the duplicate stays, so its folder and directory item stay too
            assert (root / "Old Show Part 2/S01E02.mkv").is_file()
            assert (root / "Old Show Part 2/poster.jpg").is_file()
            assert await MediaItem.exists(id=parent.id)
            assert (root / "Old Show/S01E02.mkv").read_bytes() == b"episode"

    asyncio.run(run())


def test_leftovers(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            parent, episodes = await second_folder(env)
            root = Path(env.shows.dir)
            Path(episodes[0].path).unlink()
            await MediaItem.filter(id=episodes[0].id).delete()
            write(root, "Junk/a.url", "Junk/Deep/b.txt", "Mixed/c.url", "Mixed/d.mkv")
            merge = moving(env.shows, "Old Show", item_ids=[parent.id])
            clean = MediaBatch(
                library_id=env.shows.id,
                operation="delete",
                empty_dirs=True,
                leftovers=True,
            )

            merging = await MediaBatchService.review(merge, env.owner)
            await confirm(merging, merge, env.owner)
            review = await MediaBatchService.review(clean, env.owner)
            write(root, "Junk/late.url")
            result = await confirm(review, clean, env.owner)
            entries = await MediaBatchService.entries(
                review["batch_id"], Pageable(page_num=1, page_size=10), None
            )

            assert merging["leftovers"] == {
                "folders": 1,
                "files": 1,
                "bytes": 10,
                "sample": [
                    {
                        "directory": "Old Show Part 2",
                        "files": 1,
                        "bytes": 10,
                        "names": ["poster.jpg"],
                    }
                ],
            }
            assert sorted(row["src"] for row in review["sample"]) == [
                "Junk",
                "Old Show Part 2",
            ]
            assert review["planned"]["files"] == 3
            assert [row["names"] for row in review["leftovers"]["sample"]] == [
                ["Deep/b.txt", "a.url"],
                ["poster.jpg"],
            ]
            assert result["outcome"] == "partial"
            assert not (root / "Old Show Part 2").exists()
            # a folder that gained a file after the review is kept whole
            assert (root / "Junk/a.url").is_file()
            assert {row["src"]: row["reason"] for row in entries["items"]} == {
                "Junk": "content_changed",
                "Old Show Part 2": None,
            }
            assert (root / "Mixed/c.url").is_file()

    asyncio.run(run())


def test_move_by_season(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            parent, episodes = await second_folder(env)
            await MediaItem.filter(id=episodes[0].id).delete()
            (Path(parent.path) / "S01E02.mkv").unlink()
            (Path(parent.path) / "poster.jpg").unlink()
            root = Path(env.shows.dir)
            change = moving(env.shows, "Old Show", item_ids=[parent.id], by_season=True)

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            season = await MediaItem.get(path=str(root / "Old Show/Season 02"))
            later = await MediaItem.get(id=episodes[2].id)

            assert review["planned"]["units"] == 2
            assert result["outcome"] == "completed"
            assert (root / "Old Show/Season 01/S01E03.mkv").is_file()
            assert (root / "Old Show/Season 02/S02E01.mkv").is_file()
            assert (season.season, season.title) == (2, None)
            assert later.parent_id == season.id
            # the emptied folder and its directory item are gone
            assert not (root / "Old Show Part 2").exists()
            assert not await MediaItem.exists(id=parent.id)

    asyncio.run(run())


def test_move_folder(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            root = Path(env.shows.dir)
            change = moving(env.shows, "Renamed Show", item_ids=[env.show.id])

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            show_item = await MediaItem.get(id=env.show.id)
            episode = await MediaItem.get(id=env.episodes[0].id)

            assert review["planned"] == {
                "units": 1,
                "files": 3,
                "bytes": 0,
                "by_owner": {"indexed": 1},
            }
            assert review["sample"][0]["dst"] == "Renamed Show"
            assert result["outcome"] == "completed"
            assert not (root / "Old Show").exists()
            assert (root / "Renamed Show/Renamed Show.nfo").is_file()
            assert (show_item.path, show_item.name) == (
                str(root / "Renamed Show"),
                "Renamed Show",
            )
            assert show_item.nfo_path == str(root / "Renamed Show/Renamed Show.nfo")
            assert episode.path == str(root / "Renamed Show/S01E01.mkv")
            assert episode.nfo_path == str(root / "Renamed Show/S01E01.nfo")
            assert episode.parent_id == env.show.id

    asyncio.run(run())


def test_move_refusals(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            parent, _ = await second_folder(env, unique_id="999", nfo_source="tmdb")

            mismatch = await MediaBatchService.review(
                moving(env.shows, "Old Show", item_ids=[parent.id]), env.owner
            )
            taken = await MediaBatchService.review(
                moving(env.movies, "Old Movie", item_ids=[env.movie.id]), env.owner
            )
            video = await MediaBatchService.review(
                moving(env.movies, "Elsewhere", item_ids=[501]), env.owner
            )
            deep = await MediaBatchService.review(
                moving(env.shows, "A/B/C", item_ids=[parent.id]), env.owner
            )
            books = await MediaBatchService.review(
                moving(env.books, "Work", name="*"), env.owner
            )
            await MediaLib.filter(id=env.shows.id).update(rename_template=TV_TEMPLATE)
            templated = await MediaBatchService.review(
                moving(env.shows, "Old Show", item_ids=[parent.id]), env.owner
            )

            assert mismatch["blocked"]["reasons"] == {"identity_mismatch": 3}
            assert taken["issues"][0]["message"].startswith("Every selected")
            assert video["blocked"]["reasons"] == {"unsupported_move": 1}
            assert deep["issues"][0]["path"] == "destination"
            assert books["issues"][0]["path"] == "library_id"
            assert templated["issues"][0]["path"] == "library_id"
            assert not any(
                r["valid"] for r in (mismatch, taken, video, deep, books, templated)
            )

    asyncio.run(run())


def png(color: int) -> bytes:
    """Build a tiny valid PNG so comic pages pass source checks.

    Args:
        color: Red channel value distinguishing pages.

    Returns:
        Encoded image bytes.
    """
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + bytes((color, 0, 0)) * 4 for _ in range(4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


async def comics(tmp_path: Path) -> SimpleNamespace:
    """Create a comic library with two collections and a standalone comic.

    Args:
        tmp_path: Isolated filesystem root.

    Returns:
        The library, both collections, their chapters and the standalone comic.
    """
    root = tmp_path / "comics"
    lib = await MediaLib.create(
        lib_type=LibType.COMIC, dir=str(root), name="Comics", priority=9
    )

    async def chapter(parent: MediaItem, name: str) -> MediaItem:
        folder = Path(parent.path) / name
        folder.mkdir(parents=True)
        for page in range(2):
            (folder / f"{page:03}.png").write_bytes(png(page))
        return await MediaItem.create(
            lib=lib,
            parent=parent,
            dir=str(folder),
            path=str(folder),
            name=name,
            format=MediaFormat.DIR,
        )

    async def collection(name: str) -> MediaItem:
        (root / name).mkdir(parents=True)
        return await MediaItem.create(
            lib=lib, dir=str(root / name), path=str(root / name), name=name
        )

    first = await collection("Manga")
    second = await collection("[Scan] Manga 2")
    chapters = [
        await chapter(first, "Vol 1"),
        await chapter(second, "Vol 1"),
        await chapter(second, "[Scan] Vol 2"),
    ]
    single = root / "One Shot"
    single.mkdir()
    (single / "000.png").write_bytes(png(9))
    alone = await MediaItem.create(
        lib=lib,
        dir=str(single),
        path=str(single),
        name="One Shot",
        format=MediaFormat.DIR,
    )
    return SimpleNamespace(
        lib=lib, first=first, second=second, chapters=chapters, alone=alone
    )


def test_comic_rename(tmp_path):
    async def run():
        async with memory_db():
            env = await comics(tmp_path)
            root = Path(env.lib.dir)
            change = renaming(
                env.lib,
                r"^\[Scan\] ",
                item_ids=[env.second.id, env.chapters[2].id, env.alone.id],
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)
            second = await MediaItem.get(id=env.second.id)
            later = await MediaItem.get(id=env.chapters[2].id)
            events = await MediaEvent.filter(lib_id=env.lib.id).count()

            assert review["planned"]["units"] == 2
            assert review["unchanged"] == 1
            assert sorted(row["dst"] for row in review["sample"]) == [
                "Manga 2",
                "[Scan] Manga 2/Vol 2",
            ]
            assert result["outcome"] == "completed"
            assert (root / "Manga 2/Vol 2/000.png").is_file()
            assert not (root / "[Scan] Manga 2").exists()
            assert (second.path, second.name) == (str(root / "Manga 2"), "Manga 2")
            assert later.path == str(root / "Manga 2/Vol 2")
            assert later.parent_id == env.second.id
            assert later.index_state == "pending"
            # the library's own reconcile is asked to index the renamed works
            assert events >= 2

    asyncio.run(run())


def test_comic_merge(tmp_path):
    async def run():
        async with memory_db():
            env = await comics(tmp_path)
            root = Path(env.lib.dir)
            change = moving(
                env.lib,
                "Manga",
                item_ids=[env.chapters[1].id, env.chapters[2].id, env.alone.id],
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)
            moved = await MediaItem.get(id=env.chapters[2].id)

            assert review["planned"]["units"] == 1
            # a same-named chapter stays, and a standalone comic cannot become one
            assert review["blocked"]["reasons"] == {
                "destination_exists": 1,
                "layout_change": 1,
            }
            assert result["outcome"] == "completed"
            assert (root / "Manga/[Scan] Vol 2/001.png").is_file()
            assert (root / "[Scan] Manga 2/Vol 1/000.png").is_file()
            assert moved.parent_id == env.first.id
            assert moved.path == str(root / "Manga/[Scan] Vol 2")

    asyncio.run(run())


def test_reading_work_move(tmp_path):
    async def run():
        async with memory_db():
            env = await comics(tmp_path)
            root = tmp_path / "books"
            write(root, "Old Title/Book.txt", "Old Title/Book.opf", "Old Title/a.url")
            books = await MediaLib.create(
                lib_type=LibType.NOVEL, dir=str(root), name="Books", priority=8
            )
            book = await MediaItem.create(
                lib=books,
                dir=str(root / "Old Title"),
                path=str(root / "Old Title/Book.txt"),
                name="Book",
                format=MediaFormat.TXT,
            )
            folder = moving(books, "New Title", item_ids=[book.id])
            body = renaming(books, "^Book$", "Novel", item_ids=[book.id])

            moved = await confirm(await MediaBatchService.review(folder, ADMIN), folder)
            renamed = await confirm(await MediaBatchService.review(body, ADMIN), body)
            current = await MediaItem.get(id=book.id)
            orphan = await MediaBatchService.review(
                moving(env.lib, "One Shot", item_ids=[env.chapters[0].id]), ADMIN
            )
            taken = await MediaBatchService.review(
                moving(env.lib, "Manga", item_ids=[env.second.id]), ADMIN
            )
            deep = await MediaBatchService.review(
                moving(env.lib, "A/B", item_ids=[env.alone.id]), ADMIN
            )

            assert moved["outcome"] == renamed["outcome"] == "completed"
            assert sorted(p.name for p in (root / "New Title").iterdir()) == [
                "Novel.opf",
                "Novel.txt",
                "a.url",
            ]
            assert (current.path, current.dir, current.name) == (
                str(root / "New Title/Novel.txt"),
                str(root / "New Title"),
                "Novel",
            )
            # chapters cannot join a standalone comic, nor a collection another
            assert orphan["blocked"]["reasons"] == {"unindexable_destination": 1}
            assert taken["blocked"]["reasons"] == {"layout_change": 1}
            assert deep["issues"][0]["path"] == "destination"

    asyncio.run(run())


def test_comic_split(tmp_path):
    async def run():
        async with memory_db():
            env = await comics(tmp_path)
            root = Path(env.lib.dir)
            change = moving(
                env.lib, "Side Story", item_ids=[c.id for c in env.chapters[1:]]
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)
            created = await MediaItem.get(
                lib_id=env.lib.id, path=str(root / "Side Story")
            )
            moved = await MediaItem.filter(parent_id=created.id).order_by("path")
            undo = reverting(env.lib, result["batch_id"])
            undone = await confirm(await MediaBatchService.review(undo, ADMIN), undo)
            back = await MediaItem.get(id=env.chapters[1].id)

            assert review["planned"]["units"] == 2
            assert result["outcome"] == "completed"
            # the new folder is registered as a collection before chapters join it
            assert (created.format, created.parent_id) == (None, None)
            assert [Path(row.path).name for row in moved] == ["Vol 1", "[Scan] Vol 2"]
            assert undone["outcome"] == "completed"
            assert back.parent_id == env.second.id
            assert (root / "[Scan] Manga 2/Vol 1/000.png").is_file()

    asyncio.run(run())


def test_move_movie(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            root = Path(env.movies.dir)
            write(root, "Loose.2020.mkv", "Loose.2020.zh.ass")
            (root / "Loose.2020.nfo").write_text(
                "<movie><title>Loose</title></movie>", encoding="utf-8"
            )
            loose = await MediaItem.create(
                lib=env.movies,
                dir=str(root),
                path=str(root / "Loose.2020.mkv"),
                name="Loose.2020",
                nfo_path=str(root / "Loose.2020.nfo"),
                title="Loose",
                year=2020,
                unique_id="77",
                nfo_source="tmdb",
            )
            change = moving(env.movies, "Loose (2020)", item_ids=[loose.id])

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            video = await MediaItem.get(id=loose.id)
            folder = await MediaItem.get(id=video.parent_id)
            crowd = await MediaBatchService.review(
                moving(env.movies, "Loose (2020)", item_ids=[501]), env.owner
            )
            undo = reverting(env.movies, result["batch_id"])
            undone = await confirm(
                await MediaBatchService.review(undo, env.owner), undo, env.owner
            )
            restored = await MediaItem.get(id=loose.id)

            assert review["sample"][0]["dst"] == "Loose (2020)/Loose.2020.mkv"
            assert result["outcome"] == "completed"
            assert video.path == str(root / "Loose (2020)/Loose.2020.mkv")
            assert video.nfo_path == str(root / "Loose (2020)/Loose.2020.nfo")

            # the folder takes over what the library knows about the movie
            assert (folder.path, folder.title, folder.year, folder.unique_id) == (
                str(root / "Loose (2020)"),
                "Loose",
                2020,
                "77",
            )
            assert folder.nfo_path == video.nfo_path
            assert crowd["blocked"]["reasons"] == {"identity_mismatch": 1}

            assert undone["outcome"] == "completed"
            assert (restored.path, restored.parent_id) == (
                str(root / "Loose.2020.mkv"),
                None,
            )
            assert sorted(p.name for p in root.iterdir() if p.is_file()) == [
                "Loose.2020.mkv",
                "Loose.2020.nfo",
                "Loose.2020.zh.ass",
            ]
            assert not (root / "Loose (2020)").exists()
            assert not await MediaItem.exists(id=folder.id)

    asyncio.run(run())


def reverting(lib: MediaLib, batch_id: int) -> MediaBatch:
    return MediaBatch(library_id=lib.id, operation="revert", batch_id=batch_id)


def test_empty_folders(tmp_path):
    async def run():
        async with memory_db():
            env = await comics(tmp_path)
            root = Path(env.lib.dir)
            for name in ("Gone/Deep/Deeper", "Kept"):
                (root / name).mkdir(parents=True)
            write(root, "Kept/note.txt")
            merge = moving(env.lib, "Manga", item_ids=[env.chapters[2].id])
            await confirm(await MediaBatchService.review(merge, ADMIN), merge)
            shutil.rmtree(root / "[Scan] Manga 2/Vol 1")
            await MediaItem.filter(id=env.chapters[1].id).delete()
            change = MediaBatch(
                library_id=env.lib.id, operation="delete", empty_dirs=True
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)

            assert sorted(row["src"] for row in review["sample"]) == [
                "Gone",
                "[Scan] Manga 2",
            ]
            assert result["outcome"] == "completed"
            assert not (root / "Gone").exists()
            # the collection that lost its chapters leaves the library with its folder
            assert not (root / "[Scan] Manga 2").exists()
            assert not await MediaItem.exists(id=env.second.id)
            assert (root / "Kept/note.txt").is_file()
            assert await MediaItem.exists(id=env.first.id)

    asyncio.run(run())


def test_revert_videos(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            folder = Path(env.show.dir)
            write(folder, "S01E01.zh.ass")
            before = sorted(path.name for path in folder.iterdir())
            rename = renaming(env.shows, "^S01", "Old Show S01", item_ids=[env.show.id])
            done = await confirm(
                await MediaBatchService.review(rename, env.owner), rename, env.owner
            )
            change = reverting(env.shows, done["batch_id"])

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            first = await MediaItem.get(id=env.episodes[0].id)
            details = await MediaBatchService.details(review["batch_id"])
            again = await MediaBatchService.review(change, env.owner)

            assert review["operation"] == "revert"
            assert review["planned"]["units"] == 2
            assert review["sample"][0]["dst"].startswith("Old Show/S01E0")
            assert result["outcome"] == "completed"
            assert sorted(path.name for path in folder.iterdir()) == before
            assert first.path == str(folder / "S01E01.mkv")
            assert first.nfo_path == str(folder / "S01E01.nfo")
            assert (details["operation"], details["reverts"]) == (
                "rename",
                done["batch_id"],
            )
            # nothing is left where that batch put it, so a second revert has no work
            assert not again["valid"]
            assert again["blocked"]["reasons"] == {"missing": 2}

    asyncio.run(run())


def test_revert_merge(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            parent, episodes = await second_folder(env)
            await MediaItem.filter(id=episodes[0].id).delete()
            (Path(parent.path) / "S01E02.mkv").unlink()
            (Path(parent.path) / "poster.jpg").unlink()
            root = Path(env.shows.dir)
            merge = moving(env.shows, "Old Show", item_ids=[parent.id], by_season=True)
            done = await confirm(
                await MediaBatchService.review(merge, env.owner), merge, env.owner
            )
            change = reverting(env.shows, done["batch_id"])

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            back = await MediaItem.get(id=episodes[1].id)
            folder = await MediaItem.get(path=str(root / "Old Show Part 2"))

            assert not await MediaItem.exists(id=parent.id)
            assert result["outcome"] == "completed"
            assert (root / "Old Show Part 2/S01E03.mkv").is_file()
            assert (root / "Old Show Part 2/S01E03.zh.ass").is_file()
            assert (root / "Old Show Part 2/S02E01.mkv").is_file()
            # the merge removed the folder's directory item; it is registered again
            assert back.parent_id == folder.id
            assert not (root / "Old Show/Season 01").exists()

    asyncio.run(run())


def test_revert_comics(tmp_path):
    async def run():
        async with memory_db():
            env = await comics(tmp_path)
            root = Path(env.lib.dir)
            rename = renaming(
                env.lib, r"^\[Scan\] ", item_ids=[env.second.id, env.chapters[2].id]
            )
            done = await confirm(await MediaBatchService.review(rename, ADMIN), rename)
            change = reverting(env.lib, done["batch_id"])

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)
            chapter = await MediaItem.get(id=env.chapters[2].id)

            # the collection goes back first, carrying the chapter that follows it
            assert [row["src"] for row in review["sample"]] == [
                "Manga 2",
                "[Scan] Manga 2/Vol 2",
            ]
            assert result["outcome"] == "completed"
            assert (root / "[Scan] Manga 2/[Scan] Vol 2/000.png").is_file()
            assert chapter.path == str(root / "[Scan] Manga 2/[Scan] Vol 2")

    asyncio.run(run())


def test_revert_refusals(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            delete = deletion(lib, extensions=["url"])
            reviewed = await MediaBatchService.review(delete, ADMIN)
            pending = await MediaBatchService.review(
                reverting(lib, reviewed["batch_id"]), ADMIN
            )
            await confirm(reviewed, delete)

            deleted = await MediaBatchService.review(
                reverting(lib, reviewed["batch_id"]), ADMIN
            )
            unknown = await MediaBatchService.review(reverting(lib, 9999), ADMIN)

            assert [r["issues"][0]["path"] for r in (pending, deleted, unknown)] == [
                "batch_id"
            ] * 3
            assert "finished" in pending["issues"][0]["message"]
            assert "rename and move" in deleted["issues"][0]["message"]

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["rename", "move", "organize", "delete"])
def test_revert_capability(monkeypatch, tmp_path, operation):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            if operation == "rename":
                change = renaming(env.shows, "^S01", "New S01", item_ids=[env.show.id])
            elif operation == "move":
                change = moving(env.shows, "Target", item_ids=[env.show.id])
            elif operation == "organize":
                await MediaLib.filter(id=env.shows.id).update(
                    rename_template=TV_TEMPLATE
                )
                change = MediaBatch(library_id=env.shows.id, operation="organize")
            else:
                change = deletion(env.shows, item_ids=[env.episodes[0].id])
            expected = operation in ("rename", "move")

            review = await MediaBatchService.review(change, env.owner)
            result = await confirm(review, change, env.owner)
            details = await MediaBatchService.details(review["batch_id"])
            receipt = await MediaChangeReceipt.get(id=review["batch_id"])
            legacy = {
                key: value for key, value in result.items() if key != "can_revert"
            }
            await MediaChangeReceipt.filter(id=receipt.id).update(result=legacy)
            replay = await confirm(review, change, env.owner)

            assert review["valid"] and result["outcome"] == "completed"
            assert all(
                value["can_revert"] is expected
                for value in (review, result, details, replay)
            )
            for name, value in (
                ("review_media_batch", review),
                ("apply_media_change", result),
            ):
                fact = tools.tool_facts({"name": name, "ok": True, "data": value})
                assert fact["data"]["can_revert"] is expected
            if operation == "organize":
                assert any("cannot be undone" in note for note in review["notes"])
                undo = await MediaBatchService.review(
                    reverting(env.shows, result["batch_id"]), env.owner
                )
                assert not undo["valid"]
                assert "rename and move" in undo["issues"][0]["message"]

    asyncio.run(run())


def test_undo_conversation(monkeypatch, tmp_path):
    streams, offered = [], []

    def offer(body, *calls):
        offered.append(body)
        return list(calls)

    def confirm_latest(body):
        return offer(
            body,
            (
                "apply_media_change",
                {"request_id": latest_request(body), "confirmed": True},
            ),
        )

    def batch_id(body):
        import re

        return int(
            re.findall(r'batch_id\\?": ?(\d+)', dumps(body["messages"]).decode())[-1]
        )

    steps = [
        lambda body: offer(body, ("list_media_items", {"library_id": 61})),
        lambda body: offer(body, ("review_media_batch", {"library_id": 61})),
        lambda body: offer(
            body,
            (
                "review_media_batch",
                {
                    "library_id": 61,
                    "operation": "rename",
                    "item_ids": [600],
                    "pattern": "^S01",
                    "replacement": "X",
                },
            ),
        ),
        lambda body: offer(body),
        confirm_latest,
        lambda body: offer(body),
        lambda body: offer(
            body,
            (
                "review_media_batch",
                {"library_id": 61, "operation": "revert", "batch_id": batch_id(body)},
            ),
        ),
        lambda body: offer(body),
        confirm_latest,
        lambda body: offer(body),
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            folder = Path(env.show.dir)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            for key, prompt in enumerate(("Rename", "Yes", "Undo that", "Yes")):
                await converse(conversation["id"], env.owner, prompt, key=f"k{key}")

            def pattern(body):
                tool = next(
                    tool["function"]
                    for tool in body["tools"]
                    if tool["function"]["name"] == "review_media_batch"
                )
                return tool["parameters"]["properties"]["pattern"]

            # every field is named from the start; limits arrive with the batch focus
            assert "maxLength" not in pattern(offered[1])
            assert pattern(offered[2])["maxLength"] == 256
            # a turn after an applied batch starts in the focus, ready to undo it
            assert names(offered[6]) == tools.BATCH_FOCUS
            assert (folder / "S01E01.mkv").is_file()
            assert not (folder / "XE01.mkv").exists()

    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["rename", "replace", "rewrite"])
def test_leftover_changes(tmp_path, mutation):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            root = Path(lib.dir)
            write(root, "Junk/old.url")
            source = root / "Junk/old.url"
            change = deletion(lib, empty_dirs=True, leftovers=True, directory="Junk")
            review = await MediaBatchService.review(change, ADMIN)

            if mutation == "rename":
                source.rename(source.with_name("new.url"))
            else:
                if mutation == "replace":
                    source.unlink()
                source.write_bytes(b"y" * 10)
            result = await confirm(review, change)

            assert result["reasons"] == {"content_changed": 1}
            assert len(list((root / "Junk").iterdir())) == 1

    asyncio.run(run())


def test_legacy_leftover(tmp_path):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            root = Path(lib.dir)
            write(root, "Junk/old.url")
            change = deletion(lib, empty_dirs=True, leftovers=True, directory="Junk")
            review = await MediaBatchService.review(change, ADMIN)
            await MediaBatchEntry.filter(receipt_id=review["batch_id"]).update(
                identity=[1, 10]
            )

            result = await confirm(review, change)

            assert result["reasons"] == {"content_changed": 1}
            assert (root / "Junk/old.url").exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("guard", "reason"),
    [
        ("companion", "item_changed"),
        ("download", "download_active"),
        ("link", "linked"),
    ],
)
def test_delete_guards(tmp_path, guard, reason):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            root = Path(lib.dir)
            write(root, "Staged/episode.zh.srt")
            source = root / "Staged/episode.zh.srt"
            change = deletion(lib, name="episode.zh.srt")
            review = await MediaBatchService.review(change, ADMIN)
            assert review["planned"]["units"] == 1

            if guard == "companion":
                write(root, "Staged/episode.mkv")
                await MediaItem.create(
                    lib=lib,
                    dir=str(source.parent),
                    path=str(source.with_name("episode.mkv")),
                    name="episode",
                )
            elif guard == "download":
                downloader = await Downloader.create(
                    name="Active", priority=1, config="{}"
                )
                await DownloadTask.create(
                    downloader=downloader,
                    dir=str(source.parent),
                    name="Active",
                    files=[source.name],
                    state=DownloadState.DOWNLOADING,
                )
            else:
                (root / "alias.srt").symlink_to(source)
            result = await confirm(review, change)

            assert result["reasons"] == {reason: 1}
            assert source.exists()

    asyncio.run(run())


def test_last_page_guard(tmp_path):
    async def run():
        async with memory_db():
            root = tmp_path / "comics"
            write(root, "Book/001.png", "Book/002.png")
            lib = await MediaLib.create(
                name="Comics", lib_type=LibType.COMIC, dir=str(root), priority=1
            )
            await MediaItem.create(
                lib=lib,
                path=str(root / "Book"),
                dir=str(root / "Book"),
                name="Book",
                format=MediaFormat.DIR,
            )
            change = deletion(lib, name="001.png", owners=["page"])
            review = await MediaBatchService.review(change, ADMIN)
            (root / "Book/002.png").unlink()

            result = await confirm(review, change)

            assert result["reasons"] == {"empties_item": 1}
            assert (root / "Book/001.png").exists()

    asyncio.run(run())


@pytest.mark.parametrize("trash", [False, True])
def test_deletion_mode_changed(tmp_path, monkeypatch, trash):
    async def run():
        async with memory_db():
            lib, _, _ = await show(tmp_path)
            config = SimpleNamespace(filesystem_trash_mode=trash)
            monkeypatch.setattr(KaloscopeConfig, "get", lambda: config)
            change = deletion(lib, name="stray.url")
            review = await MediaBatchService.review(change, ADMIN)
            config.filesystem_trash_mode = not trash

            result = await confirm(review, change)

            assert result["stale"] is True
            assert not result["applied"]
            assert (Path(lib.dir) / "stray.url").exists()
            assert not (await MediaChangeReceipt.get(id=review["batch_id"])).claimed

    asyncio.run(run())


def test_reading_recovery(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            root = tmp_path / "books"
            write(root, "Book/old.txt", "Book/old.opf")
            lib = await MediaLib.create(
                name="Books", lib_type=LibType.NOVEL, dir=str(root), priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(root / "Book/old.txt"),
                dir=str(root / "Book"),
                name="old",
                format=MediaFormat.TXT,
            )
            change = renaming(lib, "^old$", "new", item_ids=[item.id])
            review = await MediaBatchService.review(change, ADMIN)
            original = MediaItemService.move_reading_file
            monkeypatch.setattr(
                MediaItemService,
                "move_reading_file",
                AsyncMock(side_effect=OperationalError("database unavailable")),
            )

            result = await confirm(review, change)

            assert result["reasons"] == {"organize_pending": 1}
            assert await MediaEvent.filter(
                lib_id=lib.id, event_type="organize"
            ).exists()
            monkeypatch.setattr(MediaItemService, "move_reading_file", original)
            async with library_lock(lib.dir):
                await recover_organizing(lib)
            await item.refresh_from_db()

            assert item.path == str(root / "Book/new.txt")
            assert Path(item.path).exists()
            assert (root / "Book/new.opf").exists()
            assert not await MediaEvent.filter(
                lib_id=lib.id, event_type="organize"
            ).exists()
            assert await MediaItem.filter(lib_id=lib.id).count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["before_files", "after_files", "index"])
def test_reading_interruption(tmp_path, monkeypatch, stage):
    async def run():
        async with memory_db():
            lib, item = await reading_work(tmp_path, MediaFormat.TXT)
            lib.rename_template = None
            await lib.save(update_fields=["rename_template"])
            source = Path(item.path)
            snapshot = item.original
            owner = await User.create(
                username="Reader", password="unused", role="admin"
            )
            history = await UserHistory.create(user=owner, **await _entry(item))
            locator = history.locator
            change = renaming(lib, "^old$", "new", item_ids=[item.id])
            review = await MediaBatchService.review(change, ADMIN)
            entry = await MediaBatchEntry.get(receipt_id=review["batch_id"])
            original = organizer._move_files

            def interrupt(root, payload):
                if stage == "after_files":
                    original(root, payload)
                raise asyncio.CancelledError

            with monkeypatch.context() as patch:
                if stage == "index":
                    patch.setattr(
                        MediaItemService,
                        "move_reading_file",
                        AsyncMock(side_effect=asyncio.CancelledError),
                    )
                else:
                    patch.setattr(organizer, "_move_files", interrupt)
                with pytest.raises(asyncio.CancelledError):
                    await MediaBatchService._rename_item(entry, lib, Path(lib.dir))

            assert await MediaEvent.filter(
                lib_id=lib.id, event_type="organize"
            ).exists()
            async with library_lock(lib.dir):
                await recover_organizing(lib)
                assert await recover_organizing(lib) == {}
            await item.refresh_from_db()
            await history.refresh_from_db()

            assert item.path == str(source.with_stem("new"))
            assert Path(item.path).exists() and not source.exists()
            assert item.original == snapshot
            assert history.rel_id == item.id and history.locator == locator
            assert not await MediaEvent.filter(
                lib_id=lib.id, event_type="organize"
            ).exists()

    asyncio.run(run())


def test_reading_collection_recovery(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            env = await comics(tmp_path)
            item = env.chapters[2]
            root = Path(env.lib.dir)
            change = moving(env.lib, "New Collection", item_ids=[item.id])
            review = await MediaBatchService.review(change, ADMIN)
            original = MediaItemService.move_reading_directory
            with monkeypatch.context() as patch:
                patch.setattr(
                    MediaItemService,
                    "move_reading_directory",
                    AsyncMock(side_effect=OperationalError("database unavailable")),
                )
                result = await confirm(review, change)

            assert result["reasons"] == {"organize_pending": 1}
            assert not await MediaItem.filter(
                path=str(root / "New Collection")
            ).exists()
            async with library_lock(env.lib.dir):
                await recover_organizing(env.lib)
            await item.refresh_from_db()
            parent = await MediaItem.get(id=item.parent_id)

            assert parent.path == str(root / "New Collection")
            assert item.path == str(root / "New Collection/[Scan] Vol 2")
            assert (Path(item.path) / "000.png").exists()
            assert not await MediaEvent.filter(
                lib_id=env.lib.id, event_type="organize"
            ).exists()
            assert MediaItemService.move_reading_directory == original

    asyncio.run(run())


@pytest.mark.parametrize("occupied", [False, True])
def test_reading_rollback(tmp_path, monkeypatch, occupied):
    async def run():
        async with memory_db():
            lib, item = await reading_work(tmp_path, MediaFormat.TXT)
            lib.rename_template = None
            await lib.save(update_fields=["rename_template"])
            source = Path(item.path)
            before = source.read_bytes()
            change = renaming(lib, "^old$", "new", item_ids=[item.id])
            review = await MediaBatchService.review(change, ADMIN)

            async def refuse(*args, **kwargs):
                if occupied:
                    source.write_bytes(b"replacement")
                raise ContentError("unsupported_layout")

            with monkeypatch.context() as patch:
                patch.setattr(MediaItemService, "move_reading_file", refuse)
                result = await confirm(review, change)
            await item.refresh_from_db()

            assert result["reasons"] == {
                "organize_pending" if occupied else "organize_blocked": 1
            }
            assert item.path == str(source)
            assert source.read_bytes() == (b"replacement" if occupied else before)
            assert (
                await MediaEvent.filter(lib_id=lib.id, event_type="organize").exists()
                is occupied
            )
            if occupied:
                assert source.with_stem("new").read_bytes() == before
                source.unlink()
                async with library_lock(lib.dir):
                    await recover_organizing(lib)
                await item.refresh_from_db()
                assert item.path == str(source.with_stem("new"))

    asyncio.run(run())


@pytest.mark.parametrize("folder", [False, True])
def test_item_download_guard(tmp_path, folder):
    async def run():
        async with memory_db():
            lib, parent, episodes = await show(tmp_path)
            change = deletion(lib, item_ids=[parent.id if folder else episodes[0].id])
            review = await MediaBatchService.review(change, ADMIN)
            downloader = await Downloader.create(name="Active", priority=1, config="{}")
            source = Path(episodes[0].path)
            await DownloadTask.create(
                downloader=downloader,
                dir=str(source.parent),
                name="Active",
                files=[source.name],
                state=DownloadState.DOWNLOADING,
            )

            result = await confirm(review, change)

            assert result["reasons"] == {"download_active": 1}
            assert source.exists()
            assert await MediaItem.filter(lib_id=lib.id).count() == 3

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["loose", "video", "novel"])
@pytest.mark.parametrize("mutation", ["replace", "rewrite"])
def test_revert_identity(tmp_path, kind, mutation):
    async def run():
        async with memory_db():
            root = tmp_path / "library"
            name = (
                "Book/old.txt"
                if kind == "novel"
                else "old.mkv"
                if kind == "video"
                else "old.url"
            )
            write(root, name)
            source = root / name
            lib = await MediaLib.create(
                name="Identity",
                lib_type=LibType.NOVEL if kind == "novel" else LibType.TV_SHOW,
                dir=str(root),
                priority=1,
            )
            fields = {"extensions": ["url"]}
            if kind != "loose":
                item = await MediaItem.create(
                    lib=lib,
                    path=str(source),
                    dir=str(source.parent),
                    name="old",
                    format=MediaFormat.TXT if kind == "novel" else None,
                )
                fields = {"item_ids": [item.id]}
            change = renaming(lib, "^old$", "new", **fields)
            result = await confirm(
                await MediaBatchService.review(change, ADMIN), change
            )
            assert result["done"] == 1
            target = source.with_stem("new")
            if mutation == "replace":
                target.unlink()
            target.write_bytes(b"replacement")

            review = await MediaBatchService.review(
                reverting(lib, result["batch_id"]), ADMIN
            )

            assert review["planned"]["units"] == 0
            assert review["blocked"]["reasons"] == {"content_changed": 1}
            assert target.read_bytes() == b"replacement"
            assert not source.exists()

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.CBZ, MediaFormat.ZIP])
def test_archive_chapter_rename(tmp_path, format):
    async def run():
        async with memory_db():
            root = tmp_path / "library"
            directory = root / "Series/Chapter"
            reading_source(directory, format, "Chapter")
            lib = await MediaLib.create(
                name="Archives", lib_type=LibType.COMIC, dir=str(root), priority=1
            )
            parent = await MediaItemService.create_reading(
                lib.id, ReadingSource(directory.parent, None)
            )
            source = directory / f"old.{format.value.upper()}"
            item = await MediaItemService.create_reading(
                lib.id, ReadingSource(source, format, directory.parent)
            )
            change = renaming(lib, "^old$", "new", item_ids=[item.id])

            result = await confirm(
                await MediaBatchService.review(change, ADMIN), change
            )
            await item.refresh_from_db()

            assert result["done"] == 1
            assert item.path == str(source.with_stem("new"))
            assert item.parent_id == parent.id
            assert await MediaItem.filter(lib_id=lib.id).count() == 2
            undo = reverting(lib, result["batch_id"])
            reverted = await confirm(await MediaBatchService.review(undo, ADMIN), undo)
            await item.refresh_from_db()
            assert reverted["done"] == 1 and item.path == str(source)
            assert source.exists()

    asyncio.run(run())


@pytest.mark.parametrize("reference", ["absolute", "relative", "remote"])
def test_folder_artwork(tmp_path, reference):
    async def run():
        async with memory_db():
            lib, parent, episodes = await show(tmp_path)
            root = Path(lib.dir)
            write(root, "Show/poster.jpg", "Show/fanart.jpg", "Show/episode-thumb.jpg")
            values = {
                "absolute": (
                    str(root / "Show/poster.jpg"),
                    str(root / "Show/fanart.jpg"),
                ),
                "relative": ("poster.jpg", "fanart.jpg"),
                "remote": (
                    "https://example.test/poster.jpg",
                    "https://example.test/fanart.jpg",
                ),
            }[reference]
            await MediaItem.filter(id=parent.id).update(
                poster=values[0], backdrop=values[1]
            )
            await MediaItem.filter(id=episodes[0].id).update(
                poster=str(root / "Show/episode-thumb.jpg")
            )
            change = moving(lib, "Renamed", item_ids=[parent.id])

            result = await confirm(
                await MediaBatchService.review(change, ADMIN), change
            )
            await parent.refresh_from_db()
            await episodes[0].refresh_from_db()

            assert result["done"] == 1
            expected = (
                (str(root / "Renamed/poster.jpg"), str(root / "Renamed/fanart.jpg"))
                if reference == "absolute"
                else values
            )
            assert (parent.poster, parent.backdrop) == expected
            assert episodes[0].poster == str(root / "Renamed/episode-thumb.jpg")
            assert Path(episodes[0].poster).exists()

    asyncio.run(run())


@pytest.mark.parametrize("nested", [False, True])
def test_empty_work_move(tmp_path, nested):
    async def run():
        async with memory_db():
            root = tmp_path / "library"
            source = root / "Empty"
            (source / "A/B" if nested else source).mkdir(parents=True)
            lib = await MediaLib.create(
                name="Empty", lib_type=LibType.TV_SHOW, dir=str(root), priority=1
            )
            item = await MediaItem.create(
                lib=lib, path=str(source), dir=str(source), name="Empty"
            )
            change = moving(lib, "Target", item_ids=[item.id])

            result = await confirm(
                await MediaBatchService.review(change, ADMIN), change
            )
            await item.refresh_from_db()

            assert result["done"] == 1
            assert Path(item.path).is_dir() and not source.exists()
            if nested:
                assert (Path(item.path) / "A/B").is_dir()
            undo = reverting(lib, result["batch_id"])
            reverted = await confirm(await MediaBatchService.review(undo, ADMIN), undo)
            assert reverted["done"] == 1 and source.is_dir()
            assert not (root / "Target").exists()

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.DIR])
def test_reading_case_rename(tmp_path, format):
    async def run():
        async with memory_db():
            lib, item = await reading_work(tmp_path, format)
            lib.rename_template = None
            await lib.save(update_fields=["rename_template"])
            source = Path(item.path)
            if format == MediaFormat.TXT:
                source.with_suffix(".opf").write_bytes(
                    (source.parent / "metadata.opf").read_bytes()
                )
            name = source.stem if format == MediaFormat.TXT else source.name
            change = renaming(lib, f"^{name}$", name.upper(), item_ids=[item.id])

            review = await MediaBatchService.review(change, ADMIN)
            assert review["valid"], review
            result = await confirm(review, change)
            await item.refresh_from_db()

            assert result["done"] == 1
            target = (
                source.with_stem(name.upper())
                if format == MediaFormat.TXT
                else source.with_name(name.upper())
            )
            assert item.path == str(target) and target.exists()
            assert target.name in {path.name for path in target.parent.iterdir()}
            if format == MediaFormat.TXT:
                assert target.with_suffix(".opf").name in {
                    path.name for path in target.parent.iterdir()
                }
            undo = reverting(lib, result["batch_id"])
            reverted = await confirm(await MediaBatchService.review(undo, ADMIN), undo)
            await item.refresh_from_db()
            assert reverted["done"] == 1 and item.path == str(source)
            assert source.name in {path.name for path in source.parent.iterdir()}

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["tv", "novel", "comic"])
@pytest.mark.parametrize("allowed", [False, True])
def test_move_owners(tmp_path, kind, allowed):
    async def run():
        async with memory_db():
            if kind == "tv":
                lib, _, _ = await show(tmp_path)
                extension = "mkv"
                count = 2
            else:
                format = MediaFormat.TXT if kind == "novel" else MediaFormat.CBZ
                lib, _ = await reading_work(tmp_path, format)
                lib.rename_template = None
                await lib.save(update_fields=["rename_template"])
                extension, count = format.value, 1
            change = moving(
                lib,
                "Target",
                extensions=[extension],
                owners=["indexed"] if allowed else ["loose"],
            )

            review = await MediaBatchService.review(change, ADMIN)

            assert review["planned"]["units"] == (count if allowed else 0)
            if allowed:
                result = await confirm(review, change)
                assert result["done"] == count
            else:
                assert review["blocked"]["reasons"] == {"owner_excluded": count}
                assert not (Path(lib.dir) / "Target").exists()

    asyncio.run(run())


def test_archive_parent_revert(tmp_path):
    async def run():
        async with memory_db():
            root = tmp_path / "library"
            directory = root / "Series/Chapter"
            reading_source(directory, MediaFormat.CBZ, "Chapter")
            lib = await MediaLib.create(
                name="Archives", lib_type=LibType.COMIC, dir=str(root), priority=1
            )
            parent = await MediaItemService.create_reading(
                lib.id, ReadingSource(directory.parent, None)
            )
            source = directory / "old.CBZ"
            item = await MediaItemService.create_reading(
                lib.id, ReadingSource(source, MediaFormat.CBZ, directory.parent)
            )
            change = renaming(
                lib, "^(Series|old)$", r"new-\1", item_ids=[parent.id, item.id]
            )

            result = await confirm(
                await MediaBatchService.review(change, ADMIN), change
            )
            assert result["done"] == 2
            assert (root / "new-Series/Chapter/new-old.CBZ").exists()
            undo = reverting(lib, result["batch_id"])
            reverted = await confirm(await MediaBatchService.review(undo, ADMIN), undo)
            await item.refresh_from_db()

            assert reverted["done"] == 2
            assert item.path == str(source) and source.exists()
            assert item.parent_id == parent.id

    asyncio.run(run())


@pytest.mark.parametrize("checkpoint", [1, 2, 4])
def test_case_recovery(tmp_path, monkeypatch, checkpoint):
    async def run():
        async with memory_db():
            lib, item = await reading_work(tmp_path, MediaFormat.TXT)
            lib.rename_template = None
            await lib.save(update_fields=["rename_template"])
            source = Path(item.path)
            source.with_suffix(".opf").write_bytes(
                (source.parent / "metadata.opf").read_bytes()
            )
            before = source.read_bytes()
            change = renaming(lib, "^old$", "OLD", item_ids=[item.id])
            review = await MediaBatchService.review(change, ADMIN)
            entry = await MediaBatchEntry.get(receipt_id=review["batch_id"])
            original = organizer.rename_exclusive
            writes = 0

            def interrupt(old, new):
                nonlocal writes
                original(old, new)
                writes += 1
                if writes == checkpoint:
                    raise asyncio.CancelledError

            with monkeypatch.context() as patch:
                patch.setattr(organizer, "rename_exclusive", interrupt)
                with pytest.raises(asyncio.CancelledError):
                    await MediaBatchService._rename_item(entry, lib, Path(lib.dir))

            assert await MediaEvent.filter(
                lib_id=lib.id, event_type="organize"
            ).exists()
            async with library_lock(lib.dir):
                await recover_organizing(lib)
            await item.refresh_from_db()
            target = source.with_stem("OLD")
            names = {path.name for path in target.parent.iterdir()}

            assert item.path == str(target) and target.read_bytes() == before
            assert "OLD.TXT" in names and "OLD.opf" in names
            assert not any(name.startswith(".organizing-") for name in names)
            assert not await MediaEvent.filter(
                lib_id=lib.id, event_type="organize"
            ).exists()

    asyncio.run(run())


def test_case_recovery_conflict(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            lib, item = await reading_work(tmp_path, MediaFormat.TXT)
            lib.rename_template = None
            await lib.save(update_fields=["rename_template"])
            source = Path(item.path)
            before = source.read_bytes()
            change = renaming(lib, "^old$", "OLD", item_ids=[item.id])
            review = await MediaBatchService.review(change, ADMIN)
            entry = await MediaBatchEntry.get(receipt_id=review["batch_id"])
            original = organizer.rename_exclusive

            def interrupt(old, new):
                original(old, new)
                source.write_bytes(b"unrelated replacement")
                raise asyncio.CancelledError

            with monkeypatch.context() as patch:
                patch.setattr(organizer, "rename_exclusive", interrupt)
                with pytest.raises(asyncio.CancelledError):
                    await MediaBatchService._rename_item(entry, lib, Path(lib.dir))
            with pytest.raises(organizer.OrganizePendingError):
                async with library_lock(lib.dir):
                    await recover_organizing(lib)
            assert source.read_bytes() == b"unrelated replacement"
            assert await MediaEvent.filter(
                lib_id=lib.id, event_type="organize"
            ).exists()
            source.unlink()
            async with library_lock(lib.dir):
                await recover_organizing(lib)
            await item.refresh_from_db()

            assert item.path == str(source.with_stem("OLD"))
            assert Path(item.path).read_bytes() == before

    asyncio.run(run())


def test_empty_work_link(tmp_path):
    async def run():
        async with memory_db():
            root = tmp_path / "library"
            source = root / "Empty"
            (source / "Child").mkdir(parents=True)
            (root / "Alias").symlink_to(source / "Child", target_is_directory=True)
            lib = await MediaLib.create(
                name="Empty", lib_type=LibType.TV_SHOW, dir=str(root), priority=1
            )
            item = await MediaItem.create(
                lib=lib, path=str(source), dir=str(source), name="Empty"
            )
            change = moving(lib, "Target", item_ids=[item.id])

            review = await MediaBatchService.review(change, ADMIN)

            assert not review["valid"]
            assert review["blocked"]["reasons"] == {"organize_blocked": 1}
            assert (root / "Alias").is_dir()
            assert source.exists() and not (root / "Target").exists()

    asyncio.run(run())
