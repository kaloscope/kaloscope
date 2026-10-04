"""Tests for durable, work-scoped reading event coalescing."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from itertools import pairwise
from pathlib import Path
from queue import Queue
from unittest.mock import Mock

import pytest
from filelock import Timeout
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist
from watchdog.events import FileCreatedEvent, FileModifiedEvent

from app.core.config import KaloscopeConfig
from app.core.media import events as media_events
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.events import ReadingReconcile, coalesce_reading_events
from app.core.media.watcher import EventHandler, consume_event
from app.models.media import IndexState, LibType, MediaEvent, MediaItem, MediaLib
from app.services.media import MediaItemService


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Isolate library locks and prevent application notifications.

    Args:
        tmp_path: The temporary filesystem root.
        monkeypatch: The fixture restoring replaced functions after the test.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(cache))
    monkeypatch.setattr(media_events, "notify_media_events", Mock())


@asynccontextmanager
async def _database() -> AsyncGenerator[None]:
    """Provide an isolated schema without starting application resources.

    Yields:
        Control while the in-memory database is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _library(tmp_path: Path, lib_type: LibType = LibType.COMIC) -> MediaLib:
    """Create a library row without requiring its sources to exist.

    Args:
        tmp_path: The temporary filesystem root.
        lib_type: The library type, defaulting to comics.

    Returns:
        The library used by the event producer and coalescer.
    """
    return await MediaLib.create(
        name="Reading", dir=str(tmp_path / "Library"), lib_type=lib_type, priority=1
    )


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_coalesce_burst(tmp_path, monkeypatch, lib_type):
    """Collapse a producer burst and pass its scope to real work ingestion.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture used to observe post-commit notifications.
        lib_type: The reading type whose body and metadata are imported.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path, lib_type)
            work = Path(lib.dir) / "Work"
            target = work / "Chapter" if lib_type == LibType.COMIC else work
            target.mkdir(parents=True)
            filename = "1.png" if lib_type == LibType.COMIC else "Book.txt"
            body = target / filename
            body.write_bytes(
                b"\x89PNG\r\n\x1a\nimage" if lib_type == LibType.COMIC else b"Body"
            )
            xml = (
                "<ComicInfo><Title>Local</Title></ComicInfo>"
                if lib_type == LibType.COMIC
                else '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Local</dc:title></metadata></package>"
            )
            metadata = target / (
                "ComicInfo.xml" if lib_type == LibType.COMIC else "metadata.opf"
            )
            metadata.write_text(xml)
            handler = EventHandler(lib, asyncio.get_running_loop(), Queue())
            await handler._persist(FileCreatedEvent(str(body)))
            for _ in range(200):
                await handler._persist(FileModifiedEvent(str(body)))
            await handler._persist(FileModifiedEvent(str(metadata)))
            await handler._persist(FileCreatedEvent(str(target / ".ignored.png")))
            assert await MediaEvent.all().count() == 202

            notifications = []
            monkeypatch.setattr(
                media_events, "notify_media_events", notifications.append
            )
            tasks = await coalesce_reading_events(lib.id)
            assert len(tasks) == 1
            task = tasks[0]
            payload = ReadingReconcile.model_validate(task.payload)
            assert task.src_path == str(work)
            assert task.is_directory
            assert task.event_type == "reconcile"
            assert payload.targets == [str(target)]
            assert payload.moves == []
            assert await MediaEvent.all().count() == 1
            assert notifications == [lib.id]
            assert await coalesce_reading_events(lib.id) == []
            assert notifications == [lib.id]

            # a stale queue object must not route a saved task through video handling
            queued = await MediaEvent.get(id=task.id)
            queued.event_type = "created"
            await consume_event(queued)
            assert await MediaEvent.filter(id=task.id).exists()

            # task execution and acknowledgement belong to the later consumer
            assert await MediaItem.all().count() == 0
            assert (
                await MediaItemService.ingest_reading_work(
                    lib.id, work, targets={Path(path) for path in payload.targets}
                )
                == {}
            )
            item = await MediaItem.get(
                lib_id=lib.id, dir=str(target), format__isnull=False
            )
            assert item.index_state == IndexState.READY
            assert item.title == "Local"
            assert await MediaEvent.filter(id=task.id).exists()

    asyncio.run(run())


def test_coalesce_scopes(tmp_path):
    """Merge chapter scopes, whole-work changes and repeated calls without loss.

    Args:
        tmp_path: The isolated filesystem root.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            work = Path(lib.dir) / "Work"
            other = Path(lib.dir) / "Other"
            for path in (work / "B/2.png", work / "A/1.png", other / "Book.cbz"):
                await MediaEvent.create(
                    lib=lib, src_path=str(path), event_type="created"
                )
            tasks = await coalesce_reading_events(lib.id)
            assert [task.src_path for task in tasks] == [str(work), str(other)]
            assert tasks[0].payload == {
                "schema_version": 1,
                "targets": [str(work / "A"), str(work / "B")],
                "moves": [],
            }
            unchanged = await MediaEvent.get(id=tasks[1].id)
            duplicate = await MediaEvent.create(
                lib=lib,
                src_path=str(work),
                event_type="reconcile",
                payload={
                    "schema_version": 1,
                    "targets": [str(work / "C")],
                    "moves": [],
                },
            )
            await MediaEvent.create(
                lib=lib, src_path=str(work / "D/1.png"), event_type="created"
            )
            merged = await coalesce_reading_events(lib.id)
            assert [task.id for task in merged] == [tasks[0].id]
            assert ReadingReconcile.model_validate(merged[0].payload).targets == [
                str(work / name) for name in ("A", "B", "C", "D")
            ]
            assert not await MediaEvent.filter(id=duplicate.id).exists()
            await MediaEvent.create(
                lib=lib, src_path=str(work), event_type="modified", is_directory=True
            )
            merged = await coalesce_reading_events(lib.id)
            assert [task.id for task in merged] == [tasks[0].id]
            assert ReadingReconcile.model_validate(merged[0].payload).targets == [
                str(work)
            ]
            await MediaEvent.create(
                lib=lib, src_path=str(work / "C/3.png"), event_type="deleted"
            )
            merged = await coalesce_reading_events(lib.id)
            assert ReadingReconcile.model_validate(merged[0].payload).targets == [
                str(work)
            ]
            assert await MediaEvent.all().count() == 2
            assert (
                await MediaEvent.get(id=unchanged.id)
            ).updated_at == unchanged.updated_at

    asyncio.run(run())


@pytest.mark.parametrize("is_directory", [False, True])
def test_coalesce_moves(tmp_path, is_directory):
    """Keep cross-work moves in persisted order, including both library boundaries.

    Args:
        tmp_path: The isolated filesystem root.
        is_directory: Whether the moved source is a chapter directory or a page.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            root = Path(lib.dir)
            suffix = "Chapter" if is_directory else "Chapter/1.png"
            paths = [
                tmp_path / "Outside" / suffix,
                root / "A" / suffix,
                root / "B" / suffix,
            ]
            moves = []
            for src, dest in pairwise(paths):
                moves.append(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=str(src),
                        dest_path=str(dest),
                        event_type="moved",
                        is_directory=is_directory,
                    )
                )
                await coalesce_reading_events(lib.id)
            tasks = await MediaEvent.filter(lib=lib).order_by("src_path")
            assert len(tasks) == 2
            first = ReadingReconcile.model_validate(tasks[0].payload)
            second = ReadingReconcile.model_validate(tasks[1].payload)
            assert [move.event_id for move in first.moves] == [
                move.id for move in moves
            ]
            assert second.moves == first.moves[-1:]
            assert first.moves[0].src_path == str(paths[0])
            assert first.moves[1].dest_path == str(paths[-1])
            assert all(move.is_directory == is_directory for move in first.moves)
            assert first.targets == [str(root / "A/Chapter")]
            assert second.targets == [str(root / "B/Chapter")]
            await MediaEvent.create(
                lib=lib,
                src_path=str(paths[-1]),
                dest_path=str(paths[0]),
                event_type="moved",
                is_directory=is_directory,
            )
            merged = await coalesce_reading_events(lib.id)
            assert [task.id for task in merged] == [tasks[1].id]
            outgoing = ReadingReconcile.model_validate(merged[0].payload)
            assert len(outgoing.moves) == 2
            assert outgoing.moves[-1].dest_path == str(paths[0])

    asyncio.run(run())


def test_coalesce_isolation(tmp_path):
    """Ignore unsupported paths while preserving other libraries and event protocols.

    Args:
        tmp_path: The isolated filesystem root.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            other = await MediaLib.create(
                name="Other",
                dir=str(tmp_path / "Other"),
                lib_type=LibType.NOVEL,
                priority=2,
            )
            kept = [
                await MediaEvent.create(
                    lib=other,
                    src_path=str(Path(other.dir) / "Work/Book.txt"),
                    event_type="created",
                )
            ]
            for kind in ("ingest", "organize", "metadata", "unknown"):
                kept.append(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=str(Path(lib.dir) / "Work/1.png"),
                        event_type=kind,
                        payload={"unchanged": kind},
                    )
                )
            for path in (
                ".Hidden/1.png",
                "Work/.1.png",
                "Work/1.tmp",
                "Work/a.nfo",
                "1.png",
            ):
                await MediaEvent.create(
                    lib=lib, src_path=str(Path(lib.dir) / path), event_type="created"
                )
            await MediaEvent.create(
                lib=lib, src_path=str(tmp_path / "Outside/1.png"), event_type="deleted"
            )
            assert await coalesce_reading_events(lib.id) == []
            assert set(await MediaEvent.all().values_list("id", flat=True)) == {
                e.id for e in kept
            }
            for event in kept:
                assert (await MediaEvent.get(id=event.id)).payload == event.payload

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, RuntimeError, asyncio.CancelledError])
def test_coalesce_transaction(tmp_path, monkeypatch, failure):
    """Keep late arrivals and roll back task writes on failure or cancellation.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture injecting a late write or interruption.
        failure: The exception after a task write, or None to commit normally.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            path = str(Path(lib.dir) / "Work/Chapter/1.png")
            original = await MediaEvent.create(
                lib=lib, src_path=path, event_type="created"
            )
            save = MediaEvent.save
            late = []

            async def save_task(event, *args, **kwargs):
                await save(event, *args, **kwargs)
                if event.event_type == "reconcile":
                    with pytest.raises(Timeout):
                        async with await library_lock(lib.dir).acquire(timeout=0):
                            pass
                    if failure:
                        raise failure()
                    late.append(
                        await MediaEvent.create(
                            lib=lib, src_path=path, event_type="modified"
                        )
                    )

            monkeypatch.setattr(MediaEvent, "save", save_task)
            notifications = []
            monkeypatch.setattr(
                media_events, "notify_media_events", notifications.append
            )
            if failure:
                with pytest.raises(failure):
                    await coalesce_reading_events(lib.id)
                assert await MediaEvent.all().count() == 1
                assert await MediaEvent.filter(id=original.id).exists()
                assert notifications == []
            else:
                tasks = await coalesce_reading_events(lib.id)
                assert len(tasks) == len(late) == 1
                assert not await MediaEvent.filter(id=original.id).exists()
                assert await MediaEvent.filter(id=late[0].id).exists()
                assert notifications == [lib.id]
            monkeypatch.setattr(MediaEvent, "save", save)
            recovered = await coalesce_reading_events(lib.id)
            assert len(recovered) == 1
            assert await MediaEvent.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"schema_version": 2, "targets": []},
        {
            "schema_version": 1,
            "targets": [],
            "moves": [
                {
                    "event_id": 1,
                    "src_path": src,
                    "dest_path": "/B",
                    "is_directory": True,
                }
                for src in ("/A", "/Conflicting")
            ],
        },
    ],
)
def test_coalesce_invalid_payload(tmp_path, payload):
    """Preserve raw events and all earlier task writes when a saved task is invalid.

    Args:
        tmp_path: The isolated filesystem root.
        payload: The invalid persisted reconcile payload.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            for name in ("A", "B"):
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(Path(lib.dir) / name / "1.png"),
                    event_type="created",
                )
            invalid = await MediaEvent.create(
                lib=lib,
                src_path=str(Path(lib.dir) / "B"),
                event_type="reconcile",
                payload=payload,
            )
            with pytest.raises(ValueError):
                await coalesce_reading_events(lib.id)
            assert await MediaEvent.all().count() == 3
            assert await MediaEvent.filter(event_type="created").count() == 2
            assert (await MediaEvent.get(id=invalid.id)).payload == payload

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.MOVIE, LibType.TV_SHOW])
def test_coalesce_video(tmp_path, lib_type):
    """Reject video libraries without consuming their pending events.

    Args:
        tmp_path: The isolated filesystem root.
        lib_type: The video library type to reject.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path, lib_type)
            original = await MediaEvent.create(
                lib=lib, src_path=lib.dir, event_type="deleted"
            )
            with pytest.raises(ValueError, match="unsupported reading library type"):
                await coalesce_reading_events(lib.id)
            assert await MediaEvent.filter(id=original.id).exists()
            with pytest.raises(DoesNotExist):
                await coalesce_reading_events(lib.id + 1)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["dir", "lib_type", "delete"])
def test_coalesce_library_change(tmp_path, monkeypatch, change):
    """Recheck the library after waiting for its lock.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture injecting a change before lock acquisition.
        change: The library mutation to simulate while waiting.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            original = await MediaEvent.create(
                lib=lib,
                src_path=str(Path(lib.dir) / "Work/1.png"),
                event_type="created",
            )

            @asynccontextmanager
            async def changed(directory):
                if change == "delete":
                    await lib.delete()
                else:
                    await MediaLib.filter(id=lib.id).update(
                        **{
                            change: str(tmp_path / "Changed")
                            if change == "dir"
                            else LibType.NOVEL
                        }
                    )
                async with library_lock(directory):
                    yield

            monkeypatch.setattr(media_events, "library_lock", changed)
            with pytest.raises(DoesNotExist if change == "delete" else ContentError):
                await coalesce_reading_events(lib.id)
            if change != "delete":
                assert await MediaEvent.filter(id=original.id).exists()
            assert not await MediaEvent.filter(event_type="reconcile").exists()

    asyncio.run(run())
