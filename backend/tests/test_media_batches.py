"""Unit tests for reviewed batches of library files and items."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sanic import Sanic
from tortoise import Tortoise

from app.core.ai import tools
from app.core.config import KaloscopeConfig
from app.core.exceptions import ForbiddenException, NotFoundException
from app.models.assistant import ConversationCreate
from app.models.base import Pageable
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import (
    LibType,
    MediaBatch,
    MediaBatchEntry,
    MediaChangeApply,
    MediaChangeReceipt,
    MediaFormat,
    MediaItem,
    MediaLib,
)
from app.models.user import UserInfo, UserRole
from app.services import media_batches as service
from app.services.mcp import MCPAccessTokenService
from app.services.media_batches import MediaBatchService
from app.services.media_changes import MediaChangeService
from tests.test_download_plan_changes import latest_request
from tests.test_library_config import TV_TEMPLATE
from tests.test_mcp import application, call
from tests.test_media_changes import MODEL, Sessions, media, monitor_for, store
from tests.test_services_assistant import configure
from tests.test_services_media_reading import _client, _user
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


@asynccontextmanager
async def database():
    """Provide an isolated schema.

    Yields:
        Control while the in-memory database is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


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
        async with database():
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
        async with database():
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


def test_delete_items(tmp_path):
    async def run():
        async with database():
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


def test_indexed_by_filter(tmp_path):
    async def run():
        async with database():
            lib, parent, _ = await show(tmp_path)
            change = MediaBatch(
                library_id=lib.id,
                operation="delete",
                extensions=["mkv"],
                owners=["indexed"],
            )

            review = await MediaBatchService.review(change, ADMIN)
            result = await confirm(review, change)

            assert review["planned"]["units"] == 2
            assert result["outcome"] == "completed"
            # the show goes with its last episode, as a manual deletion does
            assert not await MediaItem.exists(id=parent.id)
            assert not list(Path(lib.dir).rglob("*.mkv"))

    asyncio.run(run())


def test_changed_file_skipped(tmp_path):
    async def run():
        async with database():
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
        async with database():
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
        async with database():
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
        async with database():
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
        async with database():
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
        async with database():
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
        async with database():
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

            denied = await call(app, "review_media_batch", {"change": change})
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

            assert denied["isError"]
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
        async with database():
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
