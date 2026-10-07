"""Tests for durable reading events, stability, completion and bounded retries."""

import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from itertools import pairwise
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from filelock import Timeout
from sanic import Sanic
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

from app.core.config import KaloscopeConfig
from app.core.media import events as media_events
from app.core.media import watcher as media_watcher
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
from app.core.media.watcher import EventHandler, LibWatcher, consume_event
from app.models.media import (
    IndexState,
    LibType,
    MediaEvent,
    MediaFormat,
    MediaItem,
    MediaLib,
)
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


def _monitor(monkeypatch: pytest.MonkeyPatch, lib: MediaLib) -> LibWatcher:
    """Provide a local scan queue without starting a filesystem observer.

    Args:
        monkeypatch: The fixture restoring the isolated watcher state.
        lib: The library whose scan is observed.

    Returns:
        A watcher with an isolated event queue and scan status.
    """
    monitor = LibWatcher(cast(Sanic, SimpleNamespace()))
    monkeypatch.setattr(monitor, "_observers", {lib.dir: (Mock(), Queue())})
    monkeypatch.setitem(monitor.__dict__, "_scanning_paths", [])
    monkeypatch.setitem(monitor.__dict__, "_event_changes", {})
    return monitor


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
@pytest.mark.parametrize("initial", [False, True])
def test_reading_scan(tmp_path, monkeypatch, lib_type, initial):
    """Queue work scopes once while retaining registered empty and missing sources.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture observing worker execution and video isolation.
        lib_type: The reading library type to scan.
        initial: Whether to enter through the initial-scan path.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path, lib_type)
            root = Path(lib.dir)
            for name in ("New", "Empty", ".hidden", "copy.part"):
                (root / name).mkdir(parents=True)
            if lib_type == LibType.NOVEL:
                (root / "New/Book.txt").write_text("Body")
            else:
                for chapter in ("1", "2"):
                    directory = root / "New" / chapter
                    directory.mkdir()
                    for page in range(20):
                        (directory / f"{page}.png").write_bytes(b"image")
            (root / "loose.txt").write_text("Not a work directory")
            outside = tmp_path / "Outside"
            outside.mkdir()
            (root / "Link").symlink_to(outside, target_is_directory=True)
            for name, directory in (
                ("Empty", root / "Empty"),
                ("Missing", root / "Missing"),
                ("Outside", outside),
                ("Escape", root / ".." / "Outside"),
                ("Hidden", root / ".registered"),
            ):
                await MediaItem.create(
                    lib=lib,
                    path=str(directory / "Book.txt")
                    if lib_type == LibType.NOVEL
                    else str(directory),
                    dir=str(directory),
                    name=name,
                    format=MediaFormat.TXT
                    if lib_type == LibType.NOVEL
                    else MediaFormat.DIR,
                    visible=False,
                )
            before = await MediaItem.all().values()
            monitor = _monitor(monkeypatch, lib)
            video = AsyncMock(side_effect=AssertionError("unexpected video scan"))
            monkeypatch.setattr(monitor, "_enqueue_events", video)
            monkeypatch.setattr(media_watcher, "recover_organizing", video)
            enumerate_sources = media_watcher.list_source_entries
            main_thread = threading.get_ident()

            def enumerate_in_worker(directory):
                """Check that filesystem enumeration runs off the event loop.

                Args:
                    directory: The library root being enumerated.

                Returns:
                    Its visible files and directories.
                """
                assert threading.get_ident() != main_thread
                return enumerate_sources(directory)

            monkeypatch.setattr(
                media_watcher, "list_source_entries", enumerate_in_worker
            )
            if initial:
                await monitor._delay_scan(lib)
            else:
                await monitor.scan_directory(lib.dir)
            tasks = await MediaEvent.all().order_by("id")
            assert [task.src_path for task in tasks] == [
                str(root / name) for name in ("Empty", "Missing", "New")
            ]
            for task in tasks:
                assert task.event_type == "reconcile" and task.is_directory
                assert task.payload == ReadingReconcile(
                    targets=[task.src_path], force_targets=[]
                ).model_dump(mode="json", exclude_none=True)
            queue = monitor._observers[lib.dir][1]
            assert [queue.get_nowait().id for _ in tasks] == [task.id for task in tasks]
            assert queue.empty()
            await monitor.scan_directory(lib)
            assert await MediaEvent.all().order_by("id").values_list(
                "id", flat=True
            ) == [task.id for task in tasks]
            assert await MediaItem.all().values() == before
            assert not monitor.is_scanning(lib.dir)
            video.assert_not_awaited()

    asyncio.run(run())


def test_scan_pending(tmp_path, monkeypatch):
    """Merge a scan with pending moves and newly arrived body modifications.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture providing the watcher queue.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            work = Path(lib.dir) / "Work"
            work.mkdir(parents=True)
            moved = await MediaEvent.create(
                lib=lib,
                event_type="moved",
                src_path=str(work / "Old"),
                dest_path=str(work / "A"),
                is_directory=True,
            )
            task = (await coalesce_reading_events(lib.id))[0]
            previous = ReadingReconcile.model_validate(task.payload)
            task.payload = previous.model_copy(
                update={"state": "failed", "attempts": 5, "error_code": "bad_body"}
            ).model_dump(mode="json", exclude_none=True)
            await task.save()
            other_path = str(Path(lib.dir) / "Unselected")
            other = await MediaEvent.create(
                lib=lib,
                event_type="reconcile",
                src_path=other_path,
                is_directory=True,
                payload=ReadingReconcile(
                    targets=[other_path],
                    force_targets=[],
                    state="failed",
                    attempts=5,
                    error_code="bad_body",
                ).model_dump(mode="json", exclude_none=True),
            )
            untouched = await MediaEvent.get(id=other.id).values()
            await MediaEvent.create(
                lib=lib, event_type="modified", src_path=str(work / "B/1.png")
            )
            monitor = _monitor(monkeypatch, lib)
            await monitor.scan_directory(lib)
            current = await MediaEvent.get(id=task.id)
            payload = ReadingReconcile.model_validate(current.payload)
            assert payload.targets == [str(work)]
            assert payload.force_targets == [
                str(work / name) for name in ("A", "B", "Old")
            ]
            assert payload.moves == previous.moves
            assert payload.moves[0].event_id == moved.id
            assert payload.state == "pending" and payload.attempts == 0
            assert payload.error_code is None and payload.observed_snapshot is None
            assert payload.not_before is None
            assert await MediaEvent.all().count() == 2
            assert await MediaEvent.get(id=other.id).values() == untouched
            assert monitor._observers[lib.dir][1].get_nowait().id == task.id

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["missing", "file", "symlink", "denied"])
def test_scan_unavailable(tmp_path, monkeypatch, problem):
    """Retain media and pending tasks when the root cannot be enumerated safely.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture simulating access failure.
        problem: The root access or file-type failure.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            root = Path(lib.dir)
            if problem == "file":
                root.write_bytes(b"not a directory")
            elif problem == "symlink":
                root.symlink_to(tmp_path, target_is_directory=True)
            elif problem == "denied":
                root.mkdir()
                monkeypatch.setattr(
                    media_watcher,
                    "list_source_entries",
                    Mock(side_effect=PermissionError),
                )
            await MediaItem.create(
                lib=lib, path=str(root / "Work"), dir=str(root / "Work"), name="Work"
            )
            await MediaEvent.create(
                lib=lib, src_path=str(root / "Work/1.png"), event_type="modified"
            )
            items = await MediaItem.all().values()
            events = await MediaEvent.all().values()
            monitor = _monitor(monkeypatch, lib)
            with pytest.raises(ContentError, match="media_source_unavailable"):
                await monitor.scan_directory(lib, validate_request=True)
            assert await MediaItem.all().values() == items
            assert await MediaEvent.all().values() == events
            assert monitor._observers[lib.dir][1].empty()
            assert not monitor.is_scanning(lib.dir)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["path", "type", "delete"])
def test_scan_ownership(tmp_path, monkeypatch, change):
    """Reject an enumeration after its library changes or is deleted.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture changing ownership after the worker returns.
        change: The database change concurrent with enumeration.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            (Path(lib.dir) / "Work").mkdir(parents=True)
            monitor = _monitor(monkeypatch, lib)
            to_thread = media_watcher.asyncio.to_thread

            async def changed(function, *args):
                """Change library ownership after filesystem enumeration.

                Args:
                    function: The synchronous worker operation.
                    args: The worker's positional arguments.

                Returns:
                    The original directory listing with now-stale ownership.
                """
                result = await to_thread(function, *args)
                if change == "delete":
                    await MediaLib.filter(id=lib.id).delete()
                else:
                    fields = (
                        {"dir": str(tmp_path / "Other")}
                        if change == "path"
                        else {"lib_type": LibType.MOVIE}
                    )
                    await MediaLib.filter(id=lib.id).update(**fields)
                return result

            monkeypatch.setattr(media_watcher.asyncio, "to_thread", changed)
            with pytest.raises(DoesNotExist if change == "delete" else ContentError):
                await monitor.scan_directory(lib)
            assert not await MediaEvent.all().exists()
            assert monitor._observers[lib.dir][1].empty()

    asyncio.run(run())


@pytest.mark.parametrize("scope", [".", "..", "Work/Chapter", ".hidden", "copy.tmp"])
def test_scan_scope(tmp_path, scope):
    """Reject invalid scan scopes before modifying persisted filesystem events.

    Args:
        tmp_path: The isolated library root.
        scope: The invalid relative scan scope.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            await MediaEvent.create(
                lib=lib,
                src_path=str(Path(lib.dir) / "Work/1.png"),
                event_type="created",
            )
            before = await MediaEvent.all().values()
            with pytest.raises(ValueError, match="scan works"):
                await coalesce_reading_events(
                    lib.id, scan_works={Path(lib.dir) / scope}
                )
            assert await MediaEvent.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_scan_ingest(tmp_path, monkeypatch, lib_type):
    """Pass scan tasks to ingestion and reuse an unchanged published body index.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture providing the watcher queue.
        lib_type: The novel or comic source ingested from the scan task.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path, lib_type)
            work = Path(lib.dir) / "Work"
            work.mkdir(parents=True)
            if lib_type == LibType.NOVEL:
                (work / "Book.txt").write_text("A complete chapter.")
            else:
                (work / "1.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")
            monitor = _monitor(monkeypatch, lib)
            version = None
            for _ in range(2):
                await monitor.scan_directory(lib)
                task = await MediaEvent.get(lib=lib, event_type="reconcile")
                payload = ReadingReconcile.model_validate(task.payload)
                if version is None:
                    assert not await MediaItem.all().exists()
                assert not await MediaItemService.ingest_reading_work(
                    lib.id,
                    Path(task.src_path),
                    targets={Path(path) for path in payload.targets},
                    force_targets={Path(path) for path in payload.force_targets},
                )
                item = await MediaItem.get(lib=lib)
                assert item.index_state == IndexState.READY
                if version is not None:
                    assert item.index_version == version
                version = item.index_version

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
@pytest.mark.parametrize("entry", ["direct", "live", "restart"])
def test_coalesce_burst(tmp_path, monkeypatch, lib_type, entry):
    """Collapse a producer burst and pass its scope to real work ingestion.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture used to observe post-commit notifications.
        lib_type: The reading type whose body and metadata are imported.
        entry: Whether to merge directly, consume live events or recover a queue.
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
            events = Queue()
            handler = EventHandler(lib, asyncio.get_running_loop(), events)
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
            video = AsyncMock(side_effect=AssertionError("unexpected video processing"))
            monkeypatch.setattr(media_watcher, "recover_organizing", video)
            monkeypatch.setattr(media_watcher, "_consume_event", video)
            if entry == "direct":
                tasks = await coalesce_reading_events(lib.id)
            else:
                monitor = _monitor(monkeypatch, lib)
                if entry == "restart":
                    await MediaLib.filter(id=lib.id).update(scan_on_startup=False)
                    events = await monitor._create_events(lib)
                monkeypatch.setitem(monitor.__dict__, "_event_changes", {})

                async def consume(event):
                    """Stop the loop once it reloads the saved reading task.

                    Args:
                        event: The raw event or saved task dequeued by the consumer.

                    Returns:
                        Whether the consumer performed work for this queued event.

                    Raises:
                        asyncio.CancelledError: After the merged task is reached.
                    """
                    processed = await consume_event(event)
                    if event.event_type == "reconcile":
                        raise asyncio.CancelledError
                    return processed

                monkeypatch.setattr(media_watcher, "consume_event", consume)
                # this test isolates event merging from the observation scheduler
                monkeypatch.setattr(
                    media_watcher, "prepare_reading_event", AsyncMock(return_value=True)
                )
                pause = AsyncMock()
                monkeypatch.setattr(media_watcher.asyncio, "sleep", pause)
                await asyncio.wait_for(monitor._event_consumer(lib.id, events), 5)
                pause.assert_awaited_once_with(1)
                tasks = await MediaEvent.filter(lib=lib, event_type="reconcile")
            assert len(tasks) == 1
            task = tasks[0]
            payload = ReadingReconcile.model_validate(task.payload)
            assert task.src_path == str(work)
            assert task.is_directory
            assert task.event_type == "reconcile"
            assert payload.targets == [str(target)]
            assert payload.force_targets == [str(target)]
            assert payload.moves == []
            assert await MediaEvent.all().count() == 1
            assert notifications == [lib.id]
            assert await coalesce_reading_events(lib.id) == []
            assert notifications == [lib.id]

            # a stale queue object must not route a saved task through video handling
            queued = await MediaEvent.get(id=task.id)
            queued.event_type = "created"
            assert not await consume_event(queued)
            assert await MediaEvent.filter(id=task.id).exists()
            video.assert_not_awaited()

            # coalescing alone must not execute or acknowledge unprepared tasks
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


@pytest.mark.parametrize("kind", ["created", "modified", "deleted", "moved"])
def test_consume_reading_scope(tmp_path, kind):
    """Merge live body events into failed tasks without changing other work scopes.

    Args:
        tmp_path: The isolated filesystem root.
        kind: The incoming filesystem event type.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path)
            root = Path(lib.dir)
            pending = []
            for name in ("Work", "Other"):
                pending.append(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=str(root / name),
                        event_type="reconcile",
                        is_directory=True,
                        payload=ReadingReconcile(
                            targets=[str(root / name / "A")],
                            force_targets=[],
                            observed_snapshot="a" * 64,
                            state="failed",
                            attempts=5,
                            error_code="content_changed",
                        ).model_dump(mode="json", exclude_none=True),
                    )
                )
            original = await MediaEvent.create(
                lib=lib,
                src_path=str(root / "Work/A/1.png"),
                dest_path=str(root / "Work/B/1.png") if kind == "moved" else None,
                event_type=kind,
            )
            # dispatch uses persisted event data instead of the queued object's type
            queued = await MediaEvent.get(id=original.id)
            queued.event_type = "reconcile"
            assert await consume_event(queued)
            task = await MediaEvent.get(id=pending[0].id)
            payload = ReadingReconcile.model_validate(task.payload)
            targets = [str(root / "Work/A")]
            if kind == "moved":
                targets.append(str(root / "Work/B"))
                assert len(payload.moves) == 1
                move = payload.moves[0]
                assert (move.event_id, move.src_path, move.dest_path) == (
                    original.id,
                    original.src_path,
                    original.dest_path,
                )
            else:
                assert not payload.moves
            assert payload.targets == payload.force_targets == targets
            assert payload.state == "pending" and payload.attempts == 0
            assert payload.observed_snapshot is None
            assert payload.not_before is None and payload.error_code is None
            assert not await MediaEvent.filter(id=original.id).exists()
            other = await MediaEvent.get(id=pending[1].id)
            assert (other.payload, other.updated_at) == (
                pending[1].payload,
                pending[1].updated_at,
            )
            # replaying an already merged queue entry must not reset the task again
            assert not await consume_event(original)
            current = await MediaEvent.get(id=task.id)
            assert (current.payload, current.updated_at) == (
                task.payload,
                task.updated_at,
            )
            assert not await MediaItem.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind", ["reconcile", "ingest", "organize", "metadata", "unknown"]
)
def test_consume_reading_pending(tmp_path, monkeypatch, kind):
    """Retain pending task protocols without invoking video or resetting retries.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture rejecting processing beyond raw event coalescing.
        kind: The persisted task protocol to retain.
    """

    async def run():
        async with _database():
            lib = await _library(tmp_path, LibType.NOVEL)
            task = await MediaEvent.create(
                lib=lib,
                src_path=str(Path(lib.dir) / "Work"),
                event_type=kind,
                payload=ReadingReconcile(
                    targets=[str(Path(lib.dir) / "Work")],
                    force_targets=[],
                    state="failed",
                    attempts=5,
                ).model_dump(mode="json", exclude_none=True),
            )
            before = await MediaEvent.all().values()
            unexpected = AsyncMock(side_effect=AssertionError("unexpected processing"))
            monkeypatch.setattr(media_watcher, "recover_organizing", unexpected)
            monkeypatch.setattr(media_watcher, "coalesce_reading_events", unexpected)
            monkeypatch.setattr(media_watcher, "_consume_event", unexpected)
            assert not await consume_event(task)
            assert await MediaEvent.all().values() == before
            unexpected.assert_not_awaited()

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
@pytest.mark.parametrize("consumer", [False, True])
def test_coalesce_transaction(tmp_path, monkeypatch, failure, consumer):
    """Keep late arrivals and roll back task writes on failure or cancellation.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture injecting a late write or interruption.
        failure: The exception after a task write, or None to commit normally.
        consumer: Whether to enter through the live watcher consumer.
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
                    if consumer:
                        await consume_event(original)
                    else:
                        await coalesce_reading_events(lib.id)
                assert await MediaEvent.all().count() == 1
                assert await MediaEvent.filter(id=original.id).exists()
                assert notifications == []
            else:
                if consumer:
                    await consume_event(original)
                    tasks = await MediaEvent.filter(lib=lib, event_type="reconcile")
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
    monkeypatch.setattr(media_watcher, "time", lambda: value[0])
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
def test_consume_reading_ready(tmp_path, monkeypatch, moment, lib_type):
    """Ingest bodies and metadata through prepared events while retaining identity.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture rejecting video processing and workflows.
        moment: The controllable task clock.
        lib_type: The reading type passed through the real execution path.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path, lib_type)
            video = AsyncMock(side_effect=AssertionError("unexpected video processing"))
            monkeypatch.setattr(media_watcher, "recover_organizing", video)
            monkeypatch.setattr(media_watcher, "_consume_event", video)
            monkeypatch.setattr(media_watcher.FlowTriggerService, "fire", video)
            assert not await consume_event(task)
            task = await _ready(task, moment)
            assert await consume_event(task)
            assert not await MediaEvent.exists()
            item = await MediaItem.get(lib=lib, format__isnull=False)
            assert item.index_state == IndexState.READY
            first_version = item.index_version
            await MediaItem.filter(id=item.id).update(visible=False)

            if lib_type == LibType.NOVEL:
                body.write_text("Updated body")
                changed = body
            else:
                changed = body.with_name("2.png")
                changed.write_bytes(body.read_bytes())
            raw = await MediaEvent.create(
                lib=lib, src_path=str(changed), event_type="modified"
            )
            assert await consume_event(raw)
            task = await _ready(await MediaEvent.get(event_type="reconcile"), moment)
            assert await consume_event(task)
            await item.refresh_from_db()
            assert item.index_state == IndexState.READY and not item.visible
            assert item.index_version != first_version
            assert await MediaItem.filter(format__isnull=False).count() == 1
            version = item.index_version

            metadata = body.parent / (
                "metadata.opf" if lib_type == LibType.NOVEL else "ComicInfo.xml"
            )
            metadata.write_text(
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Current</dc:title></metadata></package>"
                if lib_type == LibType.NOVEL
                else "<ComicInfo><Title>Current</Title></ComicInfo>"
            )
            raw = await MediaEvent.create(
                lib=lib, src_path=str(metadata), event_type="modified"
            )
            assert await consume_event(raw)
            task = await _ready(await MediaEvent.get(event_type="reconcile"), moment)
            assert await consume_event(task)
            await item.refresh_from_db()
            assert item.title == "Current" and item.index_version == version
            saved = await MediaItem.all().values()
            task = (
                await coalesce_reading_events(lib.id, scan_works={Path(task.src_path)})
            )[0]
            assert await consume_event(await _ready(task, moment))
            assert await MediaItem.all().values() == saved
            assert not await MediaEvent.exists()
            assert not await consume_event(task)
            video.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["move", "raw", "merged", "library", "removed"])
def test_consume_reading_stale(tmp_path, monkeypatch, moment, change):
    """Retain moved or superseded scopes before touching reading items.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture rejecting premature ingestion.
        moment: The controllable task clock.
        change: The pending identity work or concurrent task mutation.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            if change == "move":
                destination = tmp_path / "Moved"
                body.parent.rename(destination)
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(body.parent),
                    dest_path=str(destination),
                    event_type="moved",
                    is_directory=True,
                )
                task = (await coalesce_reading_events(lib.id))[0]
            task = await _ready(task, moment)
            if change in ("raw", "merged"):
                await MediaEvent.create(
                    lib=lib, src_path=str(body), event_type="modified"
                )
                if change == "merged":
                    await coalesce_reading_events(lib.id)
            elif change == "library":
                await MediaLib.filter(id=lib.id).update(dir=str(tmp_path / "Other"))
            elif change == "removed":
                await task.delete()
            ingest = AsyncMock(side_effect=AssertionError("unexpected ingestion"))
            monkeypatch.setattr(MediaItemService, "ingest_reading_work", ingest)
            assert not await consume_event(task)
            ingest.assert_not_awaited()
            assert not await MediaItem.exists()
            if change != "removed":
                saved = await MediaEvent.get(id=task.id)
                payload = ReadingReconcile.model_validate(saved.payload)
                if change in ("raw", "merged"):
                    assert payload.observed_snapshot is None
                else:
                    assert saved.payload == task.payload

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
@pytest.mark.parametrize("asset", ["metadata", "cover"])
def test_consume_reading_asset_move(tmp_path, moment, lib_type, asset):
    """Ingest atomic sidecar replacements without rebuilding unchanged bodies.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        lib_type: The reading library receiving the replacement.
        asset: The external metadata or named cover published by a rename.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path, lib_type)
            assert await consume_event(await _ready(task, moment))
            item = await MediaItem.get(lib=lib, format__isnull=False)
            if asset == "metadata":
                destination = body.parent / (
                    "metadata.opf" if lib_type == LibType.NOVEL else "ComicInfo.xml"
                )
                data = (
                    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    "<dc:title>Replaced</dc:title></metadata></package>"
                    if lib_type == LibType.NOVEL
                    else "<ComicInfo><Title>Replaced</Title></ComicInfo>"
                ).encode()
            else:
                destination = body.parent / "cover.PNG"
                data = b"\x89PNG\r\n\x1a\nimage"
            temporary = destination.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(destination)
            queue = Queue()
            producer = EventHandler(lib, asyncio.get_running_loop(), queue)
            await producer._persist(FileMovedEvent(str(temporary), str(destination)))
            assert await consume_event(queue.get_nowait())
            task = await _ready(await MediaEvent.get(event_type="reconcile"), moment)
            payload = ReadingReconcile.model_validate(task.payload)
            assert len(payload.moves) == 1 and payload.force_targets == []
            assert await consume_event(task)
            current = await MediaItem.get(id=item.id)
            assert current.index_state == IndexState.READY
            assert current.index_version == item.index_version
            assert current.title == ("Replaced" if asset == "metadata" else item.title)
            assert destination.read_bytes() == data
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize("cross_chapter", [False, True])
def test_consume_reading_page_move(tmp_path, moment, cross_chapter):
    """Rebuild page lists after image moves while retaining directory item IDs.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        cross_chapter: Whether the page moves into another chapter in the same work.
    """

    async def run():
        async with _database():
            lib, page, task = await _pending(tmp_path, LibType.COMIC)
            (page.parent / "2.png").write_bytes(page.read_bytes())
            if cross_chapter:
                other = page.parent.with_name("Other")
                other.mkdir()
                (other / "3.png").write_bytes(page.read_bytes())
                task = (
                    await coalesce_reading_events(lib.id, scan_works={other.parent})
                )[0]
                destination = other / "10.PNG"
            else:
                destination = page.with_name("10.PNG")
            assert await consume_event(await _ready(task, moment))
            before = {
                item.id: item.index_version
                for item in await MediaItem.filter(format=MediaFormat.DIR)
            }
            page.rename(destination)
            raw = await MediaEvent.create(
                lib=lib,
                src_path=str(page),
                dest_path=str(destination),
                event_type="moved",
            )
            assert await consume_event(raw)
            task = await _ready(await MediaEvent.get(event_type="reconcile"), moment)
            assert await consume_event(task)
            after = await MediaItem.filter(format=MediaFormat.DIR)
            assert {item.id for item in after} == set(before)
            assert all(item.index_version != before[item.id] for item in after)
            assert all(item.index_state == IndexState.READY for item in after)
            assert destination.is_file() and not page.exists()
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("temporary", ["Book.tmp", ".Book.txt"])
def test_consume_file_publish(tmp_path, moment, existing, temporary):
    """Publish temporary bodies without assigning an existing item a new identity.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        existing: Whether a previously indexed body is being replaced.
        temporary: A temporary or hidden source name used by the external writer.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            first = None
            if existing:
                assert await consume_event(await _ready(task, moment))
                first = await MediaItem.get(lib=lib)
            source = body.with_name(temporary)
            source.write_text("Published body")
            source.replace(body)
            await MediaEvent.create(
                lib=lib, src_path=str(source), dest_path=str(body), event_type="moved"
            )
            task = (await coalesce_reading_events(lib.id))[0]
            assert await consume_event(await _ready(task, moment))
            current = await MediaItem.get(lib=lib)
            assert current.path == str(body) and current.index_state == IndexState.READY
            if first is not None:
                assert current.id == first.id
                assert current.index_version != first.index_version
            assert body.read_text() == "Published body"
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["reused", "occupied", "format", "source_link"])
def test_consume_file_conflict(tmp_path, moment, problem):
    """Persist retry state instead of transferring an ambiguous body identity.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        problem: The source reuse, target ownership, format or symlink conflict.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert await consume_event(await _ready(task, moment))
            destination = body.with_name(
                "Moved.epub" if problem == "format" else "Moved.txt"
            )
            body.rename(destination)
            if problem == "reused":
                body.write_text("Another body")
            elif problem == "occupied":
                await MediaItem.create(
                    lib=lib,
                    path=str(destination),
                    dir=str(body.parent),
                    name="Other",
                    format=MediaFormat.TXT,
                    visible=False,
                )
            elif problem == "source_link":
                body.symlink_to(destination)
            before = await MediaItem.all().values()
            await MediaEvent.create(
                lib=lib,
                src_path=str(body),
                dest_path=str(destination),
                event_type="moved",
            )
            task = (await coalesce_reading_events(lib.id))[0]
            assert await consume_event(await _ready(task, moment))
            assert await MediaItem.all().values() == before
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.state == "deferred" and payload.attempts == 1
            assert payload.not_before == moment[0] + 2
            assert (
                payload.error_code
                == {
                    "reused": "ambiguous_layout",
                    "occupied": "ambiguous_layout",
                    "format": "unsupported_media_format",
                    "source_link": "content_changed",
                }[problem]
            )
            assert len(payload.moves) == 1

    asyncio.run(run())


@pytest.mark.parametrize("order", ["directory_first", "directory_last"])
@pytest.mark.parametrize("reverse", [False, True])
def test_consume_move_chain(tmp_path, moment, order, reverse):
    """Keep linked move facts across work tasks without registering a second item.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        order: Whether a directory move precedes or follows the file move.
        reverse: Whether the last work's task runs before the first work's task.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert await consume_event(await _ready(task, moment))
            before = await MediaItem.all().values()
            middle = Path(lib.dir) / "Middle" / body.name
            destination = Path(lib.dir) / "Last" / body.name
            if order != "directory_last":
                destination.parent.mkdir()
            directory = order == "directory_first"
            if directory:
                body.parent.rename(middle.parent)
            else:
                middle.parent.mkdir()
                body.rename(middle)
            await MediaEvent.create(
                lib=lib,
                src_path=str(body.parent if directory else body),
                dest_path=str(middle.parent if directory else middle),
                event_type="moved",
                is_directory=directory,
            )
            directory = order == "directory_last"
            if directory:
                middle.parent.rename(destination.parent)
            else:
                middle.rename(destination)
            await MediaEvent.create(
                lib=lib,
                src_path=str(middle.parent if directory else middle),
                dest_path=str(destination.parent if directory else destination),
                event_type="moved",
                is_directory=directory,
            )
            tasks = await coalesce_reading_events(lib.id)
            assert len(tasks) == 3
            if reverse:
                tasks.reverse()
            for task in tasks:
                task = await _ready(task, moment)
                assert not await consume_event(task)
                assert (await MediaEvent.get(id=task.id)).payload == task.payload
            assert await MediaItem.all().values() == before
            assert await MediaEvent.all().count() == 3
            assert destination.read_text() == "Body"

    asyncio.run(run())


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("first", [0, 1, 3])
def test_consume_chain_order(tmp_path, moment, directory, first):
    """Retain one identity when any work starts consuming a continuous move chain.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        directory: Whether whole work directories move instead of body files.
        first: The original, intermediate or destination work to consume first.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert await consume_event(await _ready(task, moment))
            original = await MediaItem.get(path=str(body))
            paths = [body.parent if directory else body]
            for name in ("Middle", "Later", "Last"):
                destination = Path(lib.dir) / name
                if not directory:
                    destination.mkdir()
                    destination /= body.name
                paths[-1].rename(destination)
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(paths[-1]),
                    dest_path=str(destination),
                    event_type="moved",
                    is_directory=directory,
                )
                paths.append(destination)
            tasks = await coalesce_reading_events(lib.id)
            assert len(tasks) == 4
            for task in tasks[first:] + tasks[:first]:
                assert await consume_event(await _ready(task, moment))
                assert not await MediaEvent.filter(id=task.id).exists()
            current = await MediaItem.get(id=original.id)
            assert current.path == str(
                paths[-1] / body.name if directory else paths[-1]
            )
            assert current.index_state == IndexState.READY
            assert current.index_version != original.index_version
            assert await MediaItem.all().count() == 1
            assert not await MediaEvent.exists()
            assert Path(current.path).read_text() == "Body"

    asyncio.run(run())


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("shape", ["branch", "cycle", "outside", "depth"])
def test_consume_chain_boundary(tmp_path, moment, directory, shape):
    """Keep ambiguous chains and unsupported intermediate boundaries pending.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        directory: Whether whole work directories move instead of body files.
        shape: A branch, cycle, cross-library leg or intermediate depth change.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert await consume_event(await _ready(task, moment))
            before = await MediaItem.all().values()
            source = body.parent if directory else body
            middle = (
                tmp_path / "Outside" if shape == "outside" else Path(lib.dir) / "Middle"
            )
            if shape == "depth":
                middle /= "Nested"
            middle.parent.mkdir(parents=True, exist_ok=True)
            if not directory:
                middle.mkdir()
                middle /= body.name
            source.rename(middle)
            await MediaEvent.create(
                lib=lib,
                src_path=str(source),
                dest_path=str(middle),
                event_type="moved",
                is_directory=directory,
            )
            previous = middle
            if shape == "branch":
                if directory:
                    source.mkdir()
                (source / body.name if directory else source).write_text("Replacement")
                previous = source
            destination = source if shape == "cycle" else Path(lib.dir) / "Last"
            if not directory and shape != "cycle":
                destination.mkdir()
                destination /= body.name
            previous.rename(destination)
            await MediaEvent.create(
                lib=lib,
                src_path=str(previous),
                dest_path=str(destination),
                event_type="moved",
                is_directory=directory,
            )
            await coalesce_reading_events(lib.id)
            task = await MediaEvent.get(src_path=str(body.parent))
            task = await _ready(task, moment)
            assert not await consume_event(task)
            assert (await MediaEvent.get(id=task.id)).payload == task.payload
            assert await MediaItem.all().values() == before
            assert (destination / body.name if directory else destination).exists()

    asyncio.run(run())


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("problem", ["reused", "owned", "ancestor", "late_owner"])
def test_consume_chain_conflict(tmp_path, monkeypatch, moment, directory, problem):
    """Reject reused or separately owned intermediate paths without rebinding IDs.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture inserting an owner just before the move service.
        moment: The controllable task clock.
        directory: Whether whole work directories move instead of body files.
        problem: A reused path, persisted owner, symlink ancestor or late owner.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert await consume_event(await _ready(task, moment))
            paths = [body.parent if directory else body]
            for name in ("Middle", "Last"):
                destination = Path(lib.dir) / name
                if not directory:
                    destination.mkdir()
                    destination /= body.name
                paths[-1].rename(destination)
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(paths[-1]),
                    dest_path=str(destination),
                    event_type="moved",
                    is_directory=directory,
                )
                paths.append(destination)
            intermediate = paths[1]
            if problem == "reused":
                if directory:
                    intermediate.mkdir()
                else:
                    intermediate.write_text("Another body")
            elif problem == "ancestor":
                link = intermediate if directory else intermediate.parent
                if not directory:
                    link.rmdir()
                link.symlink_to(paths[-1] if directory else paths[-1].parent)
            owner = {
                "lib": lib,
                "path": str(intermediate / body.name if directory else intermediate),
                "dir": str(intermediate if directory else intermediate.parent),
                "name": "Other",
                "format": MediaFormat.TXT,
                "visible": False,
            }
            if problem == "owned":
                await MediaItem.create(**owner)
            before = await MediaItem.all().values()
            if problem == "late_owner":
                method = "move_reading_directory" if directory else "move_reading_file"
                operation = getattr(MediaItemService, method)

                async def occupied(*args, **kwargs):
                    """Add a conflicting owner after the consumer's initial checks.

                    Args:
                        args: Positional move service arguments.
                        kwargs: Keyword move service arguments.

                    Returns:
                        The wrapped service result if ownership validation succeeds.
                    """
                    await MediaItem.create(**owner)
                    return await operation(*args, **kwargs)

                monkeypatch.setattr(MediaItemService, method, occupied)
            await coalesce_reading_events(lib.id)
            task = await MediaEvent.get(
                src_path=str(paths[0] if directory else paths[0].parent)
            )
            assert await consume_event(await _ready(task, moment))
            assert (
                await MediaItem.filter(id__in=[row["id"] for row in before]).values()
                == before
            )
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.state == "deferred" and payload.attempts == 1
            assert payload.error_code in (
                "ambiguous_layout",
                "content_changed",
                "media_source_unavailable",
            )
            assert await MediaEvent.all().count() == 3
            assert (
                paths[-1] / body.name if directory else paths[-1]
            ).read_text() == "Body"

    asyncio.run(run())


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("extended", [False, True])
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
def test_consume_chain_recovery(
    tmp_path, monkeypatch, moment, directory, extended, failure
):
    """Resume a bound identity even when another move arrives after interruption.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture interrupting ingestion after binding the identity.
        moment: The controllable task clock.
        directory: Whether whole work directories move instead of body files.
        extended: Whether the second move happens only after the interruption.
        failure: The cancellation or unexpected error interrupting ingestion.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert await consume_event(await _ready(task, moment))
            original = await MediaItem.get(path=str(body))
            paths = [body.parent if directory else body]
            for index, name in enumerate(("Middle", "Last")):
                destination = Path(lib.dir) / name
                if not directory:
                    destination.mkdir()
                    destination /= body.name
                paths[-1].rename(destination)
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(paths[-1]),
                    dest_path=str(destination),
                    event_type="moved",
                    is_directory=directory,
                )
                paths.append(destination)
                if index == int(not extended):
                    await coalesce_reading_events(lib.id)
                    task = await MediaEvent.get(
                        src_path=str(destination if directory else destination.parent)
                    )
                    prepared = await _ready(task, moment)
                    with monkeypatch.context() as patch:
                        patch.setattr(
                            MediaItemService,
                            "ingest_reading_work",
                            AsyncMock(side_effect=failure),
                        )
                        with pytest.raises(failure):
                            await consume_event(prepared)
                    bound = await MediaItem.get(id=original.id)
                    assert bound.path == str(
                        destination / body.name if directory else destination
                    )
                    assert bound.index_state == IndexState.PENDING
                    assert bound.index_version == original.index_version
                    assert (
                        await MediaEvent.get(id=task.id)
                    ).payload == prepared.payload
            await coalesce_reading_events(lib.id)
            for task in await MediaEvent.all().order_by("-id"):
                if not await prepare_reading_event(task.id):
                    moment[0] += 2
                    assert await prepare_reading_event(task.id)
                assert await consume_event(
                    await MediaEvent.get(id=task.id).select_related("lib")
                )
            current = await MediaItem.get(id=original.id)
            assert current.path == str(
                paths[-1] / body.name if directory else paths[-1]
            )
            assert current.index_state == IndexState.READY
            assert await MediaItem.all().count() == 1
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_consume_directory_publish(tmp_path, moment, lib_type):
    """Ingest an unregistered work published from a temporary directory.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        lib_type: The type of work published by an external directory rename.
    """

    async def run():
        async with _database():
            lib, _, task = await _pending(tmp_path, lib_type)
            destination = Path(task.src_path)
            temporary = destination.with_name(".incoming")
            destination.rename(temporary)
            temporary.rename(destination)
            await MediaEvent.create(
                lib=lib,
                src_path=str(temporary),
                dest_path=str(destination),
                event_type="moved",
                is_directory=True,
            )
            task = (await coalesce_reading_events(lib.id))[0]
            assert await consume_event(await _ready(task, moment))
            assert await MediaItem.filter(lib=lib).count() == (
                1 if lib_type == LibType.NOVEL else 2
            )
            assert not await MediaItem.exclude(index_state=IndexState.READY).exists()
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["reused", "owned", "missing", "link"])
def test_consume_directory_conflict(tmp_path, moment, problem):
    """Retain directory move facts when source or destination ownership is unsafe.

    Args:
        tmp_path: The isolated filesystem root.
        moment: The controllable task clock.
        problem: The reused source, occupied target, missing target or target symlink.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert await consume_event(await _ready(task, moment))
            source, destination = body.parent, body.parent.with_name("Moved")
            source.rename(destination)
            if problem == "reused":
                source.mkdir()
            elif problem == "owned":
                await MediaItem.create(
                    lib=lib,
                    path=str(destination / body.name),
                    dir=str(destination),
                    name="Other",
                    format=MediaFormat.TXT,
                    visible=False,
                )
            elif problem == "missing":
                (destination / body.name).unlink()
                destination.rmdir()
            else:
                outside = tmp_path / "Outside"
                destination.rename(outside)
                destination.symlink_to(outside, target_is_directory=True)
            before = await MediaItem.all().values()
            await MediaEvent.create(
                lib=lib,
                src_path=str(source),
                dest_path=str(destination),
                event_type="moved",
                is_directory=True,
            )
            await coalesce_reading_events(lib.id)
            task = await MediaEvent.get(event_type="reconcile", src_path=str(source))
            assert await consume_event(await _ready(task, moment))
            assert await MediaItem.all().values() == before
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.state == "deferred" and payload.attempts == 1
            assert len(payload.moves) == 1
            assert (
                payload.error_code
                == {
                    "reused": "content_changed",
                    "owned": "ambiguous_layout",
                    "missing": "media_source_unavailable",
                    "link": "media_source_unavailable",
                }[problem]
            )

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["cancel", "error", "root"])
def test_consume_directory_recovery(tmp_path, monkeypatch, moment, failure):
    """Resume a whole collection after binding or old-work verification fails.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture failing ingestion or old-work directory enumeration.
        moment: The controllable task clock.
        failure: Consumer cancellation, unexpected ingestion error or root read failure.
    """

    async def run():
        async with _database():
            lib, _, task = await _pending(tmp_path, LibType.COMIC)
            assert await consume_event(await _ready(task, moment))
            original = {item.id: item.path for item in await MediaItem.all()}
            source, destination = Path(task.src_path), Path(lib.dir) / "Moved"
            source.rename(destination)
            await MediaEvent.create(
                lib=lib,
                src_path=str(source),
                dest_path=str(destination),
                event_type="moved",
                is_directory=True,
            )
            await coalesce_reading_events(lib.id)
            task = await MediaEvent.get(
                event_type="reconcile",
                src_path=str(source if failure == "root" else destination),
            )
            task = await _ready(task, moment)
            with monkeypatch.context() as patch:
                if failure == "root":
                    patch.setattr(
                        media_watcher,
                        "list_source_entries",
                        Mock(side_effect=PermissionError),
                    )
                    assert await consume_event(task)
                else:
                    error = (
                        asyncio.CancelledError if failure == "cancel" else RuntimeError
                    )
                    patch.setattr(
                        MediaItemService,
                        "ingest_reading_work",
                        AsyncMock(side_effect=error),
                    )
                    with pytest.raises(error):
                        await consume_event(task)
            moved = await MediaItem.all()
            assert {item.id for item in moved} == set(original)
            assert all(
                item.path
                == str(destination / Path(original[item.id]).relative_to(source))
                and item.index_state == IndexState.PENDING
                for item in moved
            )
            saved = await MediaEvent.get(id=task.id).select_related("lib")
            if failure == "root":
                payload = ReadingReconcile.model_validate(saved.payload)
                assert payload.state == "deferred" and payload.attempts == 1
                moment[0] += 2
                assert await prepare_reading_event(task.id)
            else:
                assert saved.payload == task.payload
            assert await consume_event(saved)
            remaining = await MediaEvent.get(event_type="reconcile")
            assert await consume_event(await _ready(remaining, moment))
            assert not await MediaEvent.exists()
            assert not await MediaItem.exclude(index_state=IndexState.READY).exists()
            assert await MediaItem.all().count() == len(original)

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_consume_move_interrupted(tmp_path, monkeypatch, moment, cancel):
    """Resume a rebound file after interruption without rebinding or replacing its ID.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture interrupting ingestion after the move has committed.
        moment: The controllable task clock.
        cancel: Whether to simulate consumer cancellation instead of an exception.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            assert await consume_event(await _ready(task, moment))
            item = await MediaItem.get(lib=lib)
            destination = body.with_name("Moved.txt")
            body.rename(destination)
            await MediaEvent.create(
                lib=lib,
                src_path=str(body),
                dest_path=str(destination),
                event_type="moved",
            )
            task = await _ready((await coalesce_reading_events(lib.id))[0], moment)
            failure = asyncio.CancelledError if cancel else RuntimeError
            with monkeypatch.context() as patch:
                patch.setattr(
                    MediaItemService,
                    "ingest_reading_work",
                    AsyncMock(side_effect=failure),
                )
                with pytest.raises(failure):
                    await consume_event(task)
            moved = await MediaItem.get(id=item.id)
            assert (
                moved.path == str(destination)
                and moved.index_state == IndexState.PENDING
            )
            assert moved.index_version == item.index_version
            assert (await MediaEvent.get(id=task.id)).payload == task.payload
            assert await consume_event(task)
            current = await MediaItem.get(id=item.id)
            assert (
                current.path == str(destination)
                and current.index_state == IndexState.READY
            )
            assert await MediaItem.all().count() == 1
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["body", "raw", "merged", "controlled", "cancel", "error"]
)
def test_consume_reading_interrupted(tmp_path, monkeypatch, moment, change):
    """Keep later work and retry interrupted ingestion without duplicating items.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture injecting changes after ingestion and before finish.
        moment: The controllable task clock.
        change: The source change, new event or interruption during execution.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            task = await _ready(task, moment)
            ingest = MediaItemService.ingest_reading_work

            async def interrupted(*args, **kwargs):
                """Interrupt after committing an index but before task acknowledgement.

                Args:
                    *args: The original positional ingestion arguments.
                    **kwargs: The original keyword ingestion arguments.

                Returns:
                    The actual ingestion issues when execution is not interrupted.

                Raises:
                    ContentError: When simulating a controlled execution failure.
                    asyncio.CancelledError: When simulating a stopped consumer.
                    RuntimeError: When simulating an unexpected execution error.
                """
                issues = await ingest(*args, **kwargs)
                if change == "body":
                    body.write_text("Changed during indexing")
                elif change in ("raw", "merged"):
                    await MediaEvent.create(
                        lib=lib, src_path=str(body), event_type="modified"
                    )
                    if change == "merged":
                        await coalesce_reading_events(lib.id)
                elif change == "cancel":
                    raise asyncio.CancelledError
                elif change == "controlled":
                    raise ContentError("content_changed")
                else:
                    raise RuntimeError("interrupted")
                return issues

            with monkeypatch.context() as patch:
                patch.setattr(MediaItemService, "ingest_reading_work", interrupted)
                if change in ("cancel", "error"):
                    with pytest.raises(
                        asyncio.CancelledError if change == "cancel" else RuntimeError
                    ):
                        await consume_event(task)
                else:
                    assert await consume_event(task)
            item = await MediaItem.get(lib=lib)
            assert item.index_state == IndexState.READY
            if change == "raw":
                assert not await MediaEvent.filter(id=task.id).exists()
                assert await MediaEvent.filter(event_type="modified").count() == 1
                task = (await coalesce_reading_events(lib.id))[0]
                task = await _ready(task, moment)
            else:
                saved = await MediaEvent.get(id=task.id).select_related("lib")
                if change in ("body", "controlled"):
                    payload = ReadingReconcile.model_validate(saved.payload)
                    assert payload.not_before == moment[0] + 2
                    assert payload.state == (
                        "deferred" if change == "controlled" else "pending"
                    )
                    assert payload.attempts == int(change == "controlled")
                    moment[0] += 2
                    assert await prepare_reading_event(saved.id)
                    task = saved
                elif change == "merged":
                    task = await _ready(saved, moment)
                else:
                    assert saved.payload == task.payload
            assert await consume_event(task)
            assert not await MediaEvent.exists()
            assert await MediaItem.filter(id=item.id).exists()
            assert await MediaItem.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_reading_execution_retry(tmp_path, monkeypatch, moment, restart, recover):
    """Schedule real parsing retries, allow other work and resume after new input.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture advancing consumer time and recording execution.
        moment: The shared controllable observation and scheduling clock.
        restart: Whether to resume after the first persisted execution failure.
        recover: Whether to replace the failed source and deliver a new event.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path, LibType.COMIC)
            image = body.read_bytes()
            body.write_bytes(b"not an image")
            other = Path(lib.dir) / "Other/1.png"
            other.parent.mkdir()
            other.write_bytes(image)
            await MediaEvent.create(lib=lib, src_path=str(other), event_type="created")
            healthy = (await coalesce_reading_events(lib.id))[0]
            monitor = _monitor(monkeypatch, lib)
            queue = Queue()
            producer = EventHandler(lib, asyncio.get_running_loop(), queue)
            monkeypatch.setattr(
                media_events,
                "notify_media_events",
                lambda id: monitor._event_changes.__setitem__(id, True),
            )
            calls = []
            ingest = MediaItemService.ingest_reading_work

            async def execute(lib_id, work, **kwargs):
                """Record the time each real ingestion attempt starts.

                Args:
                    lib_id: The reading library being processed.
                    work: The selected work directory.
                    **kwargs: The prepared target and forced-body selections.

                Returns:
                    The issues reported by actual metadata and body processing.
                """
                calls.append((work, moment[0]))
                return await ingest(lib_id, work, **kwargs)

            async def pause(seconds):
                """Advance through retries and optionally deliver a corrected source.

                Args:
                    seconds: The consumer's sleep interval.

                Raises:
                    asyncio.CancelledError: After the final retry or recovery.
                """
                assert seconds == 1
                moment[0] += 1
                if recover and moment[0] == 155:
                    body.write_bytes(image)
                    await producer._persist(FileModifiedEvent(str(body)))
                if moment[0] >= 161:
                    raise asyncio.CancelledError

            monkeypatch.setattr(MediaItemService, "ingest_reading_work", execute)
            if restart:
                assert await consume_event(await _ready(task, moment))
                moment[0] = 103.0
            monkeypatch.setattr(media_watcher.asyncio, "sleep", pause)
            await monitor._event_consumer(lib.id, queue)
            assert [now for work, now in calls if work == Path(task.src_path)] == (
                [102.0, 104.0, 109.0, 124.0, 154.0] + ([158.0] if recover else [])
            )
            assert [now for work, now in calls if work == Path(healthy.src_path)] == [
                105.0 if restart else 102.0
            ]
            assert not await MediaEvent.filter(id=healthy.id).exists()
            if recover:
                assert not await MediaEvent.exists()
                assert not await MediaItem.exclude(
                    index_state=IndexState.READY
                ).exists()
            else:
                payload = ReadingReconcile.model_validate(
                    (await MediaEvent.get(id=task.id)).payload
                )
                assert payload.state == "failed" and payload.attempts == 5
                assert payload.error_code == "invalid_image"
                assert payload.not_before is None

    asyncio.run(run())


def test_reading_execution_recheck(tmp_path, monkeypatch, moment):
    """Resume a completion observation failure at its saved deadline.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture making only the first completion check fail.
        moment: The shared controllable observation and scheduling clock.
    """

    async def run():
        async with _database():
            lib, _, task = await _pending(tmp_path)
            monitor = _monitor(monkeypatch, lib)
            handler = get_handler(lib.lib_type)
            assert isinstance(handler, ReadingMediaHandler)
            snapshot = handler.snapshot_sources
            observed = []

            def observe(*args, **kwargs):
                """Fail source verification immediately after the first real ingestion.

                Args:
                    *args: The original observation arguments.
                    **kwargs: The selected work and targets.

                Returns:
                    The actual source snapshot for other observations.

                Raises:
                    ContentError: On the first completion verification.
                """
                observed.append(moment[0])
                if len(observed) == 3:
                    raise ContentError("media_source_unavailable")
                return snapshot(*args, **kwargs)

            async def pause(seconds):
                """Advance the clock without allowing generic error recovery sleeps.

                Args:
                    seconds: The consumer's sleep interval.

                Raises:
                    asyncio.CancelledError: Once recovery has completed.
                """
                assert seconds == 1
                if moment[0] == 102:
                    payload = ReadingReconcile.model_validate(
                        (await MediaEvent.get(id=task.id)).payload
                    )
                    assert payload.state == "deferred" and payload.attempts == 1
                    assert (
                        payload.not_before == 104 and payload.observed_snapshot is None
                    )
                moment[0] += 1
                if moment[0] >= 108:
                    raise asyncio.CancelledError

            monkeypatch.setattr(handler, "snapshot_sources", observe)
            monkeypatch.setattr(media_watcher.asyncio, "sleep", pause)
            await monitor._event_consumer(lib.id, Queue())
            assert observed == [100.0, 102.0, 102.0, 104.0, 106.0, 106.0]
            assert not await MediaEvent.exists()
            assert (await MediaItem.get(lib=lib)).index_state == IndexState.READY

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
@pytest.mark.parametrize("restart", [False, True])
def test_reading_schedule(tmp_path, monkeypatch, moment, lib_type, restart):
    """Observe stable work at its deadline without polling while idle.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture controlling the consumer clock and observations.
        moment: The shared controllable observation and scheduling clock.
        lib_type: The reading library type to schedule.
        restart: Whether the first observation was saved before consumer startup.
    """

    async def run():
        async with _database():
            lib, _, task = await _pending(tmp_path, lib_type)
            if restart:
                assert not await prepare_reading_event(task.id)
                moment[0] = 101.0
            monitor = _monitor(monkeypatch, lib)
            handler = get_handler(lib_type)
            assert isinstance(handler, ReadingMediaHandler)
            snapshot = handler.snapshot_sources
            observed, ready, idle = [], [], []
            reads = Mock(wraps=MediaEvent.get_or_none)
            monkeypatch.setattr(MediaEvent, "get_or_none", reads)

            def observe(*args, **kwargs):
                """Record when the consumer actually inspects the filesystem.

                Args:
                    *args: Positional source observation arguments.
                    **kwargs: Keyword source observation arguments.

                Returns:
                    The current source snapshot.
                """
                observed.append(moment[0])
                return snapshot(*args, **kwargs)

            async def consume(event):
                """Record stable tasks passed on for execution.

                Args:
                    event: The refreshed task after its quiet interval.

                Returns:
                    False to isolate scheduling from ingestion and completion.
                """
                ready.append((event.id, moment[0]))
                return False

            async def pause(seconds):
                """Advance time without adding real waiting to the test.

                Args:
                    seconds: The consumer's idle sleep duration.

                Raises:
                    asyncio.CancelledError: After checking two idle intervals.
                """
                assert seconds == 1
                idle.append((moment[0], reads.call_count))
                moment[0] += 1
                if moment[0] >= 104:
                    raise asyncio.CancelledError

            monkeypatch.setattr(handler, "snapshot_sources", observe)
            monkeypatch.setattr(media_watcher, "consume_event", consume)
            monkeypatch.setattr(media_watcher.asyncio, "sleep", pause)
            await monitor._event_consumer(lib.id, Queue())
            assert observed == ([102.0] if restart else [100.0, 102.0])
            assert ready == [(task.id, 102.0)]
            queries = dict(idle)
            if not restart:
                assert queries[100.0] == queries[101.0]
            assert queries[102.0] == queries[103.0]
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.observed_snapshot is not None
            assert payload.not_before == 102.0 and payload.attempts == 0
            assert not await MediaItem.exists()

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
@pytest.mark.parametrize("restart", [False, True])
def test_reading_schedule_retry(tmp_path, monkeypatch, moment, lib_type, restart):
    """Retry unavailable work on schedule while another work becomes ready.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture injecting source failures and a controllable clock.
        moment: The shared controllable observation and scheduling clock.
        lib_type: The reading library type to schedule.
        restart: Whether two failures were persisted before consumer startup.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path, lib_type)
            other = Path(lib.dir) / "Other" / body.name
            other.parent.mkdir()
            other.write_bytes(body.read_bytes())
            await MediaEvent.create(lib=lib, src_path=str(other), event_type="created")
            healthy = (await coalesce_reading_events(lib.id))[0]
            monitor = _monitor(monkeypatch, lib)
            handler = get_handler(lib_type)
            assert isinstance(handler, ReadingMediaHandler)
            snapshot = handler.snapshot_sources
            failures, ready = [], []

            def observe(base_path, *, work_path, targets):
                """Fail one work without delaying filesystem checks for the other.

                Args:
                    base_path: The library root being observed.
                    work_path: The work selected by this task.
                    targets: Its selected source containers.

                Returns:
                    The current snapshot for the healthy work.

                Raises:
                    ContentError: When observing the unavailable work.
                """
                if work_path == Path(task.src_path):
                    failures.append(moment[0])
                    raise ContentError("media_source_unavailable")
                return snapshot(base_path, work_path=work_path, targets=targets)

            async def consume(event):
                """Record the healthy task reaching the execution boundary.

                Args:
                    event: The stable task selected by the consumer.

                Returns:
                    False to isolate source retries from task execution.
                """
                ready.append((event.id, moment[0]))
                return False

            async def pause(seconds):
                """Advance through every persisted retry deadline.

                Args:
                    seconds: The consumer's idle sleep duration.

                Raises:
                    asyncio.CancelledError: After the failed task has stayed idle.
                """
                assert seconds == 1
                moment[0] += 1
                if moment[0] >= 155:
                    raise asyncio.CancelledError

            monkeypatch.setattr(handler, "snapshot_sources", observe)
            if restart:
                for now in (100.0, 102.0):
                    moment[0] = now
                    with pytest.raises(ContentError, match="media_source_unavailable"):
                        await prepare_reading_event(task.id)
                moment[0] = 103.0
            monkeypatch.setattr(media_watcher, "consume_event", consume)
            monkeypatch.setattr(media_watcher.asyncio, "sleep", pause)
            await monitor._event_consumer(lib.id, Queue())
            assert failures == [100.0, 102.0, 107.0, 122.0, 152.0]
            assert ready == [(healthy.id, 105.0 if restart else 102.0)]
            payload = ReadingReconcile.model_validate(
                (await MediaEvent.get(id=task.id)).payload
            )
            assert payload.state == "failed" and payload.attempts == 5
            assert payload.error_code == "media_source_unavailable"
            assert payload.not_before is None
            assert await MediaEvent.all().count() == 2
            assert not await MediaItem.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["modify", "scan", "delete", "duplicate", "retry", "race"]
)
def test_reading_schedule_update(tmp_path, monkeypatch, moment, change):
    """Replace cached deadlines when persisted tasks change during a wait.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture delivering notifications during an idle interval.
        moment: The shared controllable observation and scheduling clock.
        change: The new input or deletion affecting the waiting task.
    """

    async def run():
        async with _database():
            lib, body, task = await _pending(tmp_path)
            if change == "retry":
                payload = ReadingReconcile.model_validate(task.payload)
                payload.state, payload.attempts = "failed", 5
                payload.error_code = "media_source_unavailable"
                task.payload = payload.model_dump(mode="json", exclude_none=True)
                await task.save()
            monitor = _monitor(monkeypatch, lib)
            events = monitor._observers[lib.dir][1]
            producer = EventHandler(lib, asyncio.get_running_loop(), events)
            monkeypatch.setattr(
                media_events,
                "notify_media_events",
                lambda id: monitor._event_changes.__setitem__(id, True),
            )
            handler = get_handler(lib.lib_type)
            assert isinstance(handler, ReadingMediaHandler)
            snapshot = handler.snapshot_sources
            observed, ready = [], []
            reset = False

            async def prepare(id):
                """Reset a task just after its previous snapshot becomes ready.

                Args:
                    id: The task selected for stable observation.

                Returns:
                    Readiness of the observed version before the concurrent reset.
                """
                nonlocal reset
                result = await prepare_reading_event(id)
                if result and change == "race" and not reset:
                    reset = True
                    await coalesce_reading_events(
                        lib.id, scan_works={Path(task.src_path)}
                    )
                return result

            def observe(*args, **kwargs):
                """Record source reads while the task is reset or removed.

                Args:
                    *args: Positional source observation arguments.
                    **kwargs: Keyword source observation arguments.

                Returns:
                    The current source snapshot.
                """
                observed.append(moment[0])
                return snapshot(*args, **kwargs)

            async def consume(event):
                """Record readiness while also processing new raw events.

                Args:
                    event: The task or raw event dequeued by the consumer.

                Returns:
                    The current consumer result.
                """
                if (
                    event.event_type == "reconcile"
                    and await MediaEvent.filter(id=event.id).exists()
                ):
                    ready.append(moment[0])
                    return False
                return await consume_event(event)

            async def pause(seconds):
                """Deliver one change before the original deadline expires.

                Args:
                    seconds: The consumer's idle or post-processing interval.

                Raises:
                    asyncio.CancelledError: After the updated task becomes ready.
                """
                assert seconds == 1
                moment[0] += 1
                if moment[0] == 101:
                    if change in ("modify", "retry"):
                        body.write_text("Changed body")
                        await producer._persist(FileModifiedEvent(str(body)))
                    elif change == "scan":
                        await coalesce_reading_events(
                            lib.id, scan_works={Path(task.src_path)}
                        )
                    elif change == "delete":
                        await task.delete()
                    elif change == "duplicate":
                        task.event_type = "created"
                        events.put(task)
                if moment[0] >= 106:
                    raise asyncio.CancelledError

            monkeypatch.setattr(handler, "snapshot_sources", observe)
            monkeypatch.setattr(media_watcher, "prepare_reading_event", prepare)
            monkeypatch.setattr(media_watcher, "consume_event", consume)
            monkeypatch.setattr(media_watcher.asyncio, "sleep", pause)
            await monitor._event_consumer(lib.id, events)
            assert (
                ready
                == {
                    "modify": [104.0],
                    "scan": [103.0],
                    "delete": [],
                    "duplicate": [102.0],
                    "retry": [104.0],
                    "race": [104.0],
                }[change]
            )
            assert (
                observed
                == {
                    "modify": [100.0, 102.0, 104.0],
                    "scan": [100.0, 101.0, 103.0],
                    "delete": [100.0],
                    "duplicate": [100.0, 102.0],
                    "retry": [102.0, 104.0],
                    "race": [100.0, 102.0, 102.0, 104.0],
                }[change]
            )
            assert await MediaEvent.all().count() == (0 if change == "delete" else 1)
            assert not await MediaItem.exists()

    asyncio.run(run())


def test_reading_schedule_error(tmp_path, monkeypatch, moment):
    """Use normal error recovery when preparation did not persist a retry.

    Args:
        tmp_path: The isolated library root.
        monkeypatch: The fixture interrupting preparation without changing its task.
        moment: The shared controllable observation and scheduling clock.
    """

    async def run():
        async with _database():
            lib, _, task = await _pending(tmp_path)
            payload = ReadingReconcile.model_validate(task.payload)
            payload.state, payload.attempts = "deferred", 1
            payload.not_before = moment[0]
            payload.error_code = "media_source_unavailable"
            task.payload = payload.model_dump(mode="json", exclude_none=True)
            await task.save()
            before = await MediaEvent.all().values()
            monitor = _monitor(monkeypatch, lib)
            prepare = AsyncMock(
                side_effect=[ContentError("content_changed"), asyncio.CancelledError()]
            )
            pause = AsyncMock()
            monkeypatch.setattr(media_watcher, "prepare_reading_event", prepare)
            monkeypatch.setattr(media_watcher.asyncio, "sleep", pause)
            await monitor._event_consumer(lib.id, Queue())
            assert prepare.await_count == 2
            pause.assert_awaited_once_with(5)
            assert await MediaEvent.all().values() == before

    asyncio.run(run())


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
