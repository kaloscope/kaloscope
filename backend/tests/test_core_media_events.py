"""Tests for durable reading events, stability, completion and bounded retries."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from itertools import pairwise
from pathlib import Path
from queue import Queue
from unittest.mock import AsyncMock, Mock

import pytest
from filelock import Timeout
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist
from watchdog.events import FileCreatedEvent, FileModifiedEvent

from app.core.config import KaloscopeConfig
from app.core.media import events as media_events
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.events import (
    ReadingReconcile,
    coalesce_reading_events,
    finish_reading_event,
    prepare_reading_event,
)
from app.core.media.handlers.base import get_handler
from app.core.media.handlers.reading import ReadingMediaHandler
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
                "force_targets": [str(work / "A"), str(work / "B")],
                "moves": [],
                "state": "pending",
                "attempts": 0,
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


def test_coalesce_content(tmp_path):
    """Retain forced chapters when metadata expands the task to the whole work.

    Args:
        tmp_path: The isolated filesystem root.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            work = Path(lib.dir) / "Work"
            for path in (
                work / "A/1.png",
                work / "ComicInfo.xml",
                work / "B/cover.png",
            ):
                await MediaEvent.create(
                    lib=lib, src_path=str(path), event_type="modified"
                )
            task = (await coalesce_reading_events(lib.id))[0]
            payload = ReadingReconcile.model_validate(task.payload)
            assert payload.targets == [str(work)]
            assert payload.force_targets == [str(work / "A")]
            await MediaEvent.create(
                lib=lib, src_path=str(work), event_type="modified", is_directory=True
            )
            merged = (await coalesce_reading_events(lib.id))[0]
            assert merged.id == task.id
            assert ReadingReconcile.model_validate(merged.payload).force_targets == [
                str(work / "A")
            ]
            await MediaEvent.create(
                lib=lib, src_path=str(work), event_type="created", is_directory=True
            )
            whole = (await coalesce_reading_events(lib.id))[0]
            assert ReadingReconcile.model_validate(whole.payload).force_targets == [
                str(work)
            ]

    asyncio.run(run())


@pytest.mark.parametrize("legacy", [False, True])
def test_coalesce_legacy(tmp_path, legacy):
    """Preserve metadata-only intent, rebuilding unknown legacy scopes conservatively.

    Args:
        tmp_path: The isolated filesystem root.
        legacy: Whether the saved task predates body-change tracking.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            work = Path(lib.dir) / "Work"
            payload = {"targets": [str(work)], "moves": [], "schema_version": 1}
            if not legacy:
                payload["force_targets"] = []
            original = ReadingReconcile.model_validate(payload)
            assert original.force_targets == ([str(work)] if legacy else [])
            assert original.state == "pending" and original.attempts == 0
            assert original.error_code is None
            task = await MediaEvent.create(
                lib=lib,
                src_path=str(work),
                event_type="reconcile",
                payload=payload,
            )
            await MediaEvent.create(
                lib=lib, src_path=str(work / "ComicInfo.xml"), event_type="modified"
            )
            merged = (await coalesce_reading_events(lib.id))[0]
            assert merged.id == task.id
            assert ReadingReconcile.model_validate(merged.payload).force_targets == (
                [str(work)] if legacy else []
            )

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
        {"targets": [], "state": "running"},
        {"targets": [], "attempts": -1},
        {"targets": [], "attempts": 6},
        {"targets": [], "attempts": True},
        {"targets": [], "error_code": ""},
        {"targets": [], "error_code": "x" * 65},
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


@pytest.fixture
def moment(monkeypatch):
    """Control persisted deadlines without sleeping or changing database timestamps.

    Args:
        monkeypatch: The fixture restoring the task clock after the test.

    Returns:
        A mutable list containing the current epoch timestamp in seconds.
    """
    value = [100.0]
    monkeypatch.setattr(media_events, "time", lambda: value[0])
    return value


async def _pending(
    tmp_path: Path, lib_type: LibType = LibType.NOVEL
) -> tuple[MediaLib, Path, MediaEvent]:
    """Create a body and a persisted task ready for its first observation.

    Args:
        tmp_path: The isolated filesystem root.
        lib_type: The reading type, defaulting to a TXT novel.

    Returns:
        The library, body path and coalesced event.
    """
    lib = await _library(tmp_path, lib_type)
    body = Path(lib.dir) / (
        "Work/Book.txt" if lib_type == LibType.NOVEL else "Work/Chapter/1.png"
    )
    body.parent.mkdir(parents=True)
    body.write_bytes(
        b"Body" if lib_type == LibType.NOVEL else b"\x89PNG\r\n\x1a\nimage"
    )
    await MediaEvent.create(lib=lib, src_path=str(body), event_type="created")
    return lib, body, (await coalesce_reading_events(lib.id))[0]


async def _ready(task: MediaEvent, moment: list[float]) -> MediaEvent:
    """Capture a task after two stable observations across its quiet interval.

    Args:
        task: The new or reset task whose source already exists.
        moment: The controllable task clock to advance by two seconds.

    Returns:
        The prepared task with its library loaded, before any execution begins.
    """
    assert not await prepare_reading_event(task.id)
    moment[0] += 2
    assert await prepare_reading_event(task.id)
    return await MediaEvent.get(id=task.id).select_related("lib")


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_prepare_stable(tmp_path, monkeypatch, moment, lib_type):
    """Resume a persisted delay and admit stable sources without consuming the task.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture counting source observations.
        moment: The controllable task clock.
        lib_type: The reading type passed through the readiness and ingestion steps.
    """

    async def run():
        async with _database():
            lib, _, task = await _pending(tmp_path, lib_type)
            handler = get_handler(lib_type)
            assert isinstance(handler, ReadingMediaHandler)
            observe = Mock(wraps=handler.snapshot_sources)
            monkeypatch.setattr(handler, "snapshot_sources", observe)
            assert not await prepare_reading_event(task.id)
            saved = await MediaEvent.get(id=task.id)
            payload = ReadingReconcile.model_validate(saved.payload)
            assert payload.not_before == 102.0
            assert payload.observed_snapshot is not None
            assert not await prepare_reading_event(task.id)
            moment[0] = 101.9
            assert not await prepare_reading_event(task.id)
            assert observe.call_count == 1
            moment[0] = 102.0
            assert await prepare_reading_event(task.id)
            assert observe.call_count == 2
            assert (await MediaEvent.get(id=task.id)).updated_at == saved.updated_at
            assert await MediaItem.all().count() == 0
            assert (
                await MediaItemService.ingest_reading_work(
                    lib.id,
                    Path(task.src_path),
                    targets={Path(path) for path in payload.targets},
                )
                == {}
            )
            assert await MediaItem.filter(index_state=IndexState.READY).exists()
            assert await MediaEvent.filter(id=task.id).exists()

    asyncio.run(run())


def test_prepare_growth(tmp_path, moment):
    """Delay a growing source while allowing another work to become ready.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            other = Path(lib.dir) / "Other/Book.txt"
            other.parent.mkdir()
            other.write_text("Stable")
            await MediaEvent.create(lib=lib, src_path=str(other), event_type="created")
            second = (await coalesce_reading_events(lib.id))[0]
            assert not await prepare_reading_event(task.id)
            assert not await prepare_reading_event(second.id)
            moment[0] = 102.0
            body.write_text("Body still growing")
            assert not await prepare_reading_event(task.id)
            assert await prepare_reading_event(second.id)
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.not_before == 104.0
            moment[0] = 104.0
            assert await prepare_reading_event(task.id)

    asyncio.run(run())


def test_prepare_new_events(tmp_path, moment):
    """Invalidate an observed task when the same source produces another event.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert not await prepare_reading_event(task.id)
            moment[0] = 101.0
            await MediaEvent.create(lib=lib, src_path=str(body), event_type="modified")
            merged = (await coalesce_reading_events(lib.id))[0]
            assert merged.id == task.id
            payload = ReadingReconcile.model_validate(merged.payload)
            assert payload.not_before is payload.observed_snapshot is None
            assert not await prepare_reading_event(task.id)
            moment[0] = 102.0
            assert not await prepare_reading_event(task.id)
            moment[0] = 103.0
            assert await prepare_reading_event(task.id)

    asyncio.run(run())


def test_prepare_unavailable(tmp_path, monkeypatch, moment):
    """Persist an access-error delay and require fresh observations after recovery.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture injecting an unavailable source.
        moment: The controllable task clock.
    """

    async def run():
        async with _database():
            _, _, task = await _pending(tmp_path)
            assert not await prepare_reading_event(task.id)
            handler = get_handler(LibType.NOVEL)
            observe = handler.snapshot_sources
            denied = Mock(side_effect=ContentError("media_source_unavailable"))
            monkeypatch.setattr(handler, "snapshot_sources", denied)
            moment[0] = 102.0
            with pytest.raises(ContentError, match="media_source_unavailable"):
                await prepare_reading_event(task.id)
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.observed_snapshot is None
            assert payload.not_before == 104.0
            assert payload.state == "deferred" and payload.attempts == 1
            assert payload.error_code == "media_source_unavailable"
            assert not await prepare_reading_event(task.id)
            assert denied.call_count == 1
            moment[0] = 104.0
            with pytest.raises(ContentError, match="media_source_unavailable"):
                await prepare_reading_event(task.id)
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.attempts == 2 and payload.not_before == 109.0
            monkeypatch.setattr(handler, "snapshot_sources", observe)
            moment[0] = 109.0
            assert not await prepare_reading_event(task.id)
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.state == "pending" and payload.attempts == 0
            assert payload.error_code is None and payload.observed_snapshot is not None
            assert payload.not_before == 111.0
            moment[0] = 111.0
            assert await prepare_reading_event(task.id)

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_prepare_backoff(tmp_path, monkeypatch, moment, lib_type):
    """Persist backoff across calls and stop observing after five source failures.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture injecting source failures and counting observations.
        moment: The controllable task clock.
        lib_type: The reading library type whose failed task will be retried.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path, lib_type)
            initial = ReadingReconcile.model_validate(task.payload)
            handler = get_handler(lib_type)
            assert isinstance(handler, ReadingMediaHandler)
            denied = Mock(side_effect=ContentError("media_source_unavailable"))
            deadlines = (102.0, 107.0, 122.0, 152.0, None)
            with monkeypatch.context() as patcher:
                patcher.setattr(handler, "snapshot_sources", denied)
                for attempt, deadline in enumerate(deadlines, 1):
                    with pytest.raises(ContentError, match="media_source_unavailable"):
                        await prepare_reading_event(task.id)
                    saved = await MediaEvent.get(id=task.id)
                    payload = ReadingReconcile.model_validate(saved.payload)
                    assert payload.attempts == attempt
                    assert payload.error_code == "media_source_unavailable"
                    assert payload.observed_snapshot is None
                    assert payload.force_targets == initial.force_targets
                    assert payload.not_before == deadline
                    assert payload.state == (
                        "failed" if deadline is None else "deferred"
                    )
                    assert denied.call_count == attempt
                    moment[0] = deadline - 0.1 if deadline is not None else 1000.0
                    assert not await prepare_reading_event(task.id)
                    # calls reload the saved row without advancing its retry counter
                    assert denied.call_count == attempt
                    assert (await MediaEvent.get(id=task.id)).payload == saved.payload
                    if deadline is not None:
                        moment[0] = deadline
            assert await MediaEvent.all().count() == 1
            assert not await MediaItem.all().exists()
            assert not await coalesce_reading_events(lib.id)
            inspect = Mock(wraps=handler.snapshot_sources)
            monkeypatch.setattr(handler, "snapshot_sources", inspect)
            assert not await prepare_reading_event(task.id)
            assert inspect.call_count == 0
            await MediaEvent.create(lib=lib, src_path=str(body), event_type="modified")
            merged = (await coalesce_reading_events(lib.id))[0]
            assert merged.id == task.id
            payload = ReadingReconcile.model_validate(merged.payload)
            assert payload.state == "pending" and payload.attempts == 0
            assert payload.error_code is payload.not_before is None
            assert payload.force_targets == initial.force_targets
            assert not await prepare_reading_event(task.id)
            moment[0] = 1002.0
            assert await prepare_reading_event(task.id)
            assert inspect.call_count == 2
            assert await MediaEvent.filter(id=task.id).exists()

    asyncio.run(run())


def test_prepare_failed_isolation(tmp_path, monkeypatch, moment):
    """Keep a failed work dormant while another work completes its observations.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture counting filesystem observations.
        moment: The controllable task clock.
    """

    async def run():
        async with _database():
            lib, _, task = await _pending(tmp_path)
            payload = ReadingReconcile.model_validate(task.payload)
            payload.state = "failed"
            payload.attempts = 5
            payload.error_code = "media_source_unavailable"
            task.payload = payload.model_dump(mode="json", exclude_none=True)
            await task.save()
            other = Path(lib.dir) / "Other/Book.txt"
            other.parent.mkdir()
            other.write_text("Body")
            await MediaEvent.create(lib=lib, src_path=str(other), event_type="created")
            second = (await coalesce_reading_events(lib.id))[0]
            handler = get_handler(LibType.NOVEL)
            inspect = Mock(wraps=handler.snapshot_sources)
            monkeypatch.setattr(handler, "snapshot_sources", inspect)
            assert not await prepare_reading_event(task.id)
            assert not await prepare_reading_event(second.id)
            moment[0] = 102.0
            assert not await prepare_reading_event(task.id)
            assert await prepare_reading_event(second.id)
            assert inspect.call_count == 2
            assert (await MediaEvent.get(id=task.id)).payload == task.payload

    asyncio.run(run())


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("finishing", [False, True])
def test_reading_event_interrupted(tmp_path, monkeypatch, moment, failure, finishing):
    """Leave the saved task intact when observation is interrupted unexpectedly.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture injecting the interruption.
        moment: The controllable task clock.
        failure: The exception raised by the worker invocation.
        finishing: Whether interruption occurs during completion or preparation.
    """

    async def run():
        async with _database():
            _, _, task = await _pending(tmp_path)
            if finishing:
                task = await _ready(task, moment)
            worker = media_events.to_thread
            monkeypatch.setattr(
                media_events, "to_thread", AsyncMock(side_effect=failure)
            )
            with pytest.raises(failure):
                if finishing:
                    await finish_reading_event(task)
                else:
                    await prepare_reading_event(task.id)
            assert (await MediaEvent.get(id=task.id)).payload == task.payload
            monkeypatch.setattr(media_events, "to_thread", worker)
            if finishing:
                assert await finish_reading_event(task)
            else:
                assert not await prepare_reading_event(task.id)
                moment[0] = 102.0
                assert await prepare_reading_event(task.id)

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        "merge",
        "delete",
        "path",
        "dir",
        "lib_type",
        "library",
        "revision",
        "dest_path",
        "is_directory",
        "event_type",
    ],
)
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("finishing", [False, True])
def test_reading_event_race(tmp_path, monkeypatch, moment, change, failed, finishing):
    """Discard stale observations after unlocked I/O without overwriting new work.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture changing persisted work during observation.
        moment: The controllable task clock.
        change: The task or library mutation to inject after the source read.
        failed: Whether observation fails after the concurrent change.
        finishing: Whether the observed task is being completed or prepared.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            if finishing:
                task = await _ready(task, moment)
            worker = media_events.to_thread
            after = []

            async def observe(function, *args, **kwargs):
                """Read sources, then mutate state before the task reacquires its lock.

                Args:
                    function: The synchronous source observer.
                    *args: Its positional arguments.
                    **kwargs: Its keyword arguments.

                Returns:
                    The source digest computed before the simulated mutation.

                Raises:
                    ContentError: If the source observation is configured to fail.
                """
                result = await worker(function, *args, **kwargs)
                async with await library_lock(lib.dir).acquire(timeout=0):
                    pass
                if change == "merge":
                    await MediaEvent.create(
                        lib=lib, src_path=str(body), event_type="modified"
                    )
                    await coalesce_reading_events(lib.id)
                elif change == "delete":
                    await task.delete()
                elif change == "path":
                    await MediaEvent.filter(id=task.id).update(
                        src_path=str(Path(lib.dir) / "Other")
                    )
                elif change == "library":
                    await lib.delete()
                elif change == "revision":
                    current = await MediaEvent.get(id=task.id)
                    await current.save(update_fields=["updated_at"])
                elif change in ("dest_path", "is_directory", "event_type"):
                    await MediaEvent.filter(id=task.id).update(
                        **{
                            change: {
                                "dest_path": str(Path(lib.dir) / "Other"),
                                "is_directory": False,
                                "event_type": "metadata",
                            }[change]
                        }
                    )
                else:
                    await MediaLib.filter(id=lib.id).update(
                        **{
                            change: str(tmp_path / "Changed")
                            if change == "dir"
                            else LibType.COMIC
                        }
                    )
                after.extend(await MediaEvent.all().values())
                if failed:
                    raise ContentError("media_source_unavailable")
                return result

            monkeypatch.setattr(media_events, "to_thread", observe)
            if change in ("dir", "lib_type"):
                with pytest.raises(ContentError, match="content_changed"):
                    if finishing:
                        await finish_reading_event(task)
                    else:
                        await prepare_reading_event(task.id)
            else:
                assert not (
                    await finish_reading_event(task)
                    if finishing
                    else await prepare_reading_event(task.id)
                )
            assert await MediaEvent.all().values() == after

    asyncio.run(run())


def test_prepare_boundaries(tmp_path, moment):
    """Preserve wrong event types and invalid scopes instead of inspecting them.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert not await prepare_reading_event(task.id + 1)
            raw = await MediaEvent.create(
                lib=lib, src_path=str(body), event_type="created"
            )
            assert not await prepare_reading_event(raw.id)
            for targets in (
                [],
                [str(Path(lib.dir) / "Other")],
                [str(body.parent / "Child")],
            ):
                task.payload = {"schema_version": 1, "targets": targets, "moves": []}
                await task.save()
                with pytest.raises(ValueError):
                    await prepare_reading_event(task.id)
                assert (await MediaEvent.get(id=task.id)).payload == task.payload
            await MediaLib.filter(id=lib.id).update(lib_type=LibType.MOVIE)
            with pytest.raises(ValueError, match="unsupported reading library type"):
                await prepare_reading_event(task.id)
            assert await MediaEvent.all().count() == 2

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_finish_ingested(tmp_path, moment, lib_type):
    """Acknowledge real ingestion without consuming later raw events.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        lib_type: The reading type passed through ingestion and completion.
    """

    async def run():
        async with _database():
            lib, body, pending = await _pending(tmp_path, lib_type)
            task = await _ready(pending, moment)
            payload = ReadingReconcile.model_validate(task.payload)
            assert not await MediaItemService.ingest_reading_work(
                lib.id,
                Path(task.src_path),
                targets={Path(path) for path in payload.targets},
                force_targets={Path(path) for path in payload.force_targets},
            )
            later = await MediaEvent.create(
                lib=lib, src_path=str(body), event_type="modified"
            )
            other = await MediaEvent.create(
                lib=lib,
                src_path=str(Path(lib.dir) / "Other/Book.txt"),
                event_type="created",
            )
            preserved = await MediaEvent.filter(id__in=[later.id, other.id]).values()
            assert await finish_reading_event(task)
            assert await MediaItem.filter(index_state=IndexState.READY).exists()
            assert await MediaEvent.all().values() == preserved
            assert not await finish_reading_event(task)
            assert await MediaEvent.all().values() == preserved
            assert (await coalesce_reading_events(lib.id))[0].id != task.id

    asyncio.run(run())


@pytest.mark.parametrize("change", ["body", "metadata", "sibling"])
@pytest.mark.parametrize("failed", [False, True])
def test_finish_changed(tmp_path, moment, change, failed):
    """Restart changed scopes without retrying an obsolete execution failure.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        change: The selected source, parent metadata or unselected sibling to change.
        failed: Whether execution also reported a controlled error.
    """

    async def run():
        async with _database():
            lib, body, pending = await _pending(tmp_path, LibType.COMIC)
            sibling = Path(lib.dir) / "Work/Sibling/1.png"
            sibling.parent.mkdir()
            sibling.write_bytes(body.read_bytes())
            task = await _ready(pending, moment)
            path = (
                body
                if change == "body"
                else sibling
                if change == "sibling"
                else Path(task.src_path) / "ComicInfo.xml"
            )
            path.write_text("Changed")
            error = ContentError("invalid_image") if failed else None
            finished = await finish_reading_event(task, error=error)
            if change == "sibling" and not failed:
                assert finished
                return
            assert not finished
            saved = await MediaEvent.get(id=task.id)
            payload = ReadingReconcile.model_validate(saved.payload)
            if change == "sibling":
                assert payload.attempts == 1 and payload.state == "deferred"
                assert payload.error_code == "invalid_image"
            else:
                assert payload.attempts == 0 and payload.state == "pending"
                assert payload.error_code is None
                assert (
                    payload.observed_snapshot
                    != ReadingReconcile.model_validate(task.payload).observed_snapshot
                )
            assert payload.not_before == moment[0] + 2
            assert not await prepare_reading_event(task.id)
            moment[0] += 2
            assert await prepare_reading_event(task.id)

    asyncio.run(run())


def test_finish_retry(tmp_path, monkeypatch, moment):
    """Bound real parsing failures and reset them only after renewed source work.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture checking failed-task observation suppression.
        moment: The controllable task clock.
    """

    async def run():
        async with _database():
            lib, body, pending = await _pending(tmp_path, LibType.COMIC)
            image = body.read_bytes()
            body.write_bytes(b"not an image")
            task = await _ready(pending, moment)
            original = ReadingReconcile.model_validate(task.payload)
            for attempt, delay in enumerate((2, 5, 15, 30, None), 1):
                errors = await MediaItemService.ingest_reading_work(
                    lib.id, Path(task.src_path)
                )
                assert errors
                error = ContentError(next(iter(errors.values())))
                assert not await finish_reading_event(task, error=error)
                saved = await MediaEvent.get(id=task.id).select_related("lib")
                payload = ReadingReconcile.model_validate(saved.payload)
                assert payload.attempts == attempt and payload.error_code == error.code
                assert payload.observed_snapshot == original.observed_snapshot
                assert (
                    payload.targets == original.targets
                    and payload.force_targets == original.force_targets
                )
                assert payload.not_before == (moment[0] + delay if delay else None)
                assert payload.state == ("deferred" if delay else "failed")
                assert not await finish_reading_event(task, error=error)
                assert (await MediaEvent.get(id=task.id)).payload == saved.payload
                if delay:
                    assert not await prepare_reading_event(task.id)
                    moment[0] += delay
                    assert await prepare_reading_event(task.id)
                    task = await MediaEvent.get(id=task.id).select_related("lib")
                    assert (
                        ReadingReconcile.model_validate(task.payload).attempts
                        == attempt
                    )
            handler = get_handler(lib.lib_type)
            assert isinstance(handler, ReadingMediaHandler)
            inspect = Mock(wraps=handler.snapshot_sources)
            monkeypatch.setattr(handler, "snapshot_sources", inspect)
            body.write_bytes(image)
            assert not await prepare_reading_event(task.id)
            assert not await finish_reading_event(saved)
            assert inspect.call_count == 0
            await MediaEvent.create(lib=lib, src_path=str(body), event_type="modified")
            reset = (await coalesce_reading_events(lib.id))[0]
            assert reset.id == task.id
            task = await _ready(reset, moment)
            assert ReadingReconcile.model_validate(task.payload).attempts == 0
            assert not await MediaItemService.ingest_reading_work(
                lib.id, Path(task.src_path)
            )
            assert await finish_reading_event(task)
            assert not await MediaEvent.all().exists()

    asyncio.run(run())


def test_finish_unavailable(tmp_path, monkeypatch, moment):
    """Persist a failed completion recheck and require fresh stable observations.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture injecting a controlled source-read failure.
        moment: The controllable task clock.
    """

    async def run():
        async with _database():
            _, _, pending = await _pending(tmp_path)
            task = await _ready(pending, moment)
            handler = get_handler(LibType.NOVEL)
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    handler,
                    "snapshot_sources",
                    Mock(side_effect=ContentError("media_source_unavailable")),
                )
                with pytest.raises(ContentError, match="media_source_unavailable"):
                    await finish_reading_event(task)
            saved = await MediaEvent.get(id=task.id)
            payload = ReadingReconcile.model_validate(saved.payload)
            assert payload.observed_snapshot is None and payload.attempts == 1
            assert payload.state == "deferred" and payload.not_before == moment[0] + 2
            moment[0] += 2
            assert not await prepare_reading_event(task.id)
            moment[0] += 2
            assert await prepare_reading_event(task.id)
            task = await MediaEvent.get(id=task.id).select_related("lib")
            assert await finish_reading_event(task)

    asyncio.run(run())


@pytest.mark.parametrize("state", ["unobserved", "waiting", "failed", "raw"])
def test_finish_unprepared(tmp_path, monkeypatch, moment, state):
    """Keep tasks that have not met preparation requirements without reading files.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture tracking unexpected source reads.
        moment: The controllable task clock.
        state: The unprepared state or unrelated event type to preserve.
    """

    async def run():
        async with _database():
            _, _, pending = await _pending(tmp_path)
            if state == "waiting":
                assert not await prepare_reading_event(pending.id)
            elif state == "failed":
                payload = ReadingReconcile.model_validate(pending.payload)
                payload.state, payload.attempts = "failed", 5
                pending.payload = payload.model_dump(mode="json", exclude_none=True)
                await pending.save()
            elif state == "raw":
                await MediaEvent.filter(id=pending.id).update(event_type="modified")
            task = await MediaEvent.get(id=pending.id).select_related("lib")
            inspect = Mock(side_effect=AssertionError("unexpected inspection"))
            monkeypatch.setattr(get_handler(LibType.NOVEL), "snapshot_sources", inspect)
            before = await MediaEvent.all().values()
            assert not await finish_reading_event(
                task, error=ContentError("invalid_epub")
            )
            assert await MediaEvent.all().values() == before and inspect.call_count == 0

    asyncio.run(run())


@pytest.mark.parametrize("failed", [False, True])
def test_finish_write_failure(tmp_path, monkeypatch, moment, failed):
    """Retain the original task when acknowledgement or retry persistence fails.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture interrupting database persistence.
        moment: The controllable task clock.
        failed: Whether persisting a retry instead of deleting a completed task.
    """

    async def run():
        async with _database():
            _, _, pending = await _pending(tmp_path)
            task = await _ready(pending, moment)
            before = await MediaEvent.all().values()
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    MediaEvent,
                    "save" if failed else "delete",
                    AsyncMock(side_effect=RuntimeError("write failed")),
                )
                with pytest.raises(RuntimeError, match="write failed"):
                    await finish_reading_event(
                        task, error=ContentError("invalid_epub") if failed else None
                    )
            assert await MediaEvent.all().values() == before
            assert await finish_reading_event(task)

    asyncio.run(run())
