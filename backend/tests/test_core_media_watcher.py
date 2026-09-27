"""Unit tests for the media library watcher."""

import asyncio
import hashlib
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from filelock import Timeout
from lxml import etree
from tortoise import Tortoise
from watchdog.events import FileCreatedEvent, FileMovedEvent

from app.core.config import KaloscopeConfig
from app.core.constants import NFO_MIME_TYPE
from app.core.flow.context import Context
from app.core.flow.nodes.nfo.episode import EpisodeNode
from app.core.flow.nodes.nfo.movie import MovieNode
from app.core.flow.nodes.nfo.tvshow import TVShowNode
from app.core.media import organizer, shelver, watcher
from app.core.media.coordination import library_lock
from app.core.media.handlers.base import get_handler
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import HistoryType, User, UserHistory, UserRole
from app.services.danmaku import DanmakuService
from app.services.flow import FlowTriggerService


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


def test_pending_startup(tmp_path, monkeypatch):
    tasks = []
    attempts = []
    observer = Mock()
    monkeypatch.setattr(watcher, "Observer", observer)

    async def recover(lib):
        with pytest.raises(Timeout):
            async with await library_lock(lib.dir).acquire(timeout=0):
                pass
        attempts.append(lib.id)
        if lib.name == "Pending":
            raise watcher.OrganizePendingError("destination replaced externally")
        await MediaEvent.filter(lib_id=lib.id, event_type="organize").delete()
        return {}

    monkeypatch.setattr(watcher, "recover_organizing", recover)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            libraries = []
            for priority, name in enumerate(("Pending", "Healthy"), start=1):
                directory = tmp_path / name
                directory.mkdir()
                libraries.append(
                    await MediaLib.create(
                        name=name,
                        dir=str(directory),
                        lib_type=LibType.MOVIE,
                        priority=priority,
                    )
                )
            pending_lib = libraries[0]
            event = await MediaEvent.create(
                lib=pending_lib,
                src_path=str(Path(pending_lib.dir) / "old.mkv"),
                event_type="deleted",
            )
            healthy_event = await MediaEvent.create(
                lib=libraries[1],
                src_path=str(Path(libraries[1].dir) / "new.mkv"),
                event_type="created",
            )
            journals = [
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(Path(lib.dir) / "old.mkv"),
                    event_type="organize",
                    payload={},
                )
                for lib in libraries
            ]
            item = await MediaItem.create(
                lib=pending_lib,
                dir=pending_lib.dir,
                path=event.src_path,
                name="old",
            )
            app = SimpleNamespace(
                loop=asyncio.get_running_loop(),
                add_task=lambda task, **kwargs: tasks.append(task),
            )
            monitor = watcher.LibWatcher(app)
            monitor._watcher_lock = Mock()
            monitor._observing_paths = []
            monitor._scanning_paths = []
            monitor._observers = {}
            await monitor.start()

            assert attempts == [lib.id for lib in libraries]
            assert set(monitor._observing_paths) == {lib.dir for lib in libraries}
            assert observer.call_count == 2
            for lib, pending in zip(libraries, [event, healthy_event], strict=True):
                _, events = monitor._observers[lib.dir]
                queued = events.get_nowait()
                assert queued.id == pending.id
                assert queued.lib.id == lib.id
                assert events.empty()
            assert await MediaEvent.filter(id=journals[0].id).exists()
            assert not await MediaEvent.filter(id=journals[1].id).exists()

            # the pending library stays blocked without deleting its stale rows
            with pytest.raises(watcher.OrganizePendingError):
                await watcher.consume_event(event)
            with pytest.raises(watcher.OrganizePendingError):
                await monitor.scan_directory(pending_lib)
            assert await MediaItem.filter(id=item.id).exists()
            assert await MediaEvent.filter(id=event.id).exists()
        finally:
            for task in tasks:
                task.close()
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("action", [watcher.LibAction.SCAN, watcher.LibAction.REMOVE])
def test_pending_actions(tmp_path, monkeypatch, action):
    pending, healthy, removed = (
        str(tmp_path / name) for name in ("Pending", "Healthy", "Removed")
    )
    monitor = watcher.LibWatcher(None)
    monitor._watcher_actions = {
        pending: action,
        healthy: watcher.LibAction.SCAN,
        removed: watcher.LibAction.REMOVE,
    }
    monitor._observers = {path: (None, Queue()) for path in monitor._watcher_actions}
    rounds = []

    async def handle(path):
        if path == pending and not rounds:
            raise RuntimeError("library action failed")

    async def pause(delay):
        rounds.append((delay, dict(monitor._watcher_actions)))
        if len(rounds) == 2:
            raise asyncio.CancelledError

    scan_action = AsyncMock(side_effect=handle)
    remove_action = AsyncMock(side_effect=handle)
    monkeypatch.setattr(monitor, "scan_directory", scan_action)
    monkeypatch.setattr(monitor, "remove_observer", remove_action)
    monkeypatch.setattr(watcher.asyncio, "sleep", pause)

    asyncio.run(monitor._listener())

    assert rounds == [(5, {pending: action}), (10, {})]
    assert [call.args[0] for call in scan_action.await_args_list] == (
        [pending, healthy, pending] if action == watcher.LibAction.SCAN else [healthy]
    )
    assert [call.args[0] for call in remove_action.await_args_list] == (
        [pending, removed, pending] if action == watcher.LibAction.REMOVE else [removed]
    )


@pytest.mark.parametrize("stage", ["pending", "published", "conflict"])
@pytest.mark.parametrize("by_path", [False, True])
def test_scan_recovery(tmp_path, stage, by_path):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>New</title></movie>")
    destination = tmp_path / "New.mkv"
    target_nfo = destination.with_suffix(".nfo")
    mapping = {str(source): str(destination), str(nfo): str(target_nfo)}

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=lib.dir,
                name=source.stem,
                nfo_path=str(nfo),
                hash=hashlib.md5(b"video").hexdigest(),
                size=len(b"video"),
            )
            journal = await MediaEvent.create(
                lib=lib,
                src_path=str(source),
                event_type="organize",
                payload={
                    "moves": [
                        {
                            "src": old,
                            "dst": new,
                            "identity": organizer._fingerprint(Path(old)),
                        }
                        for old, new in mapping.items()
                    ],
                    "updates": [
                        {
                            "id": item.id,
                            "path": str(destination),
                            "name": destination.stem,
                            "nfo_path": str(target_nfo),
                        }
                    ],
                    "parent": None,
                    "delete_parent": None,
                    "mapping": mapping,
                    "creates": [],
                    "symlinks": [],
                    "nfo_edits": [],
                },
            )
            monitor = watcher.LibWatcher(None)
            events = Queue()
            monitor._observers = {lib.dir: (None, events)}
            monitor._scanning_paths = []
            target = lib.dir if by_path else lib

            if stage == "published":
                for old, new in mapping.items():
                    Path(old).rename(new)
            elif stage == "conflict":
                destination.write_bytes(b"unrelated")
                with pytest.raises(organizer.OrganizePendingError):
                    await monitor.scan_directory(target, validate_request=True)
                await item.refresh_from_db()
                assert item.path == str(source)
                assert item.nfo_path == str(nfo)
                assert source.read_bytes() == b"video"
                assert destination.read_bytes() == b"unrelated"
                assert await MediaEvent.filter(id=journal.id).exists()
                assert await MediaEvent.filter(lib=lib).count() == 1
                assert events.empty()
                assert not monitor.is_scanning(lib.dir)
                destination.unlink()

            await monitor.scan_directory(target, validate_request=True)

            await item.refresh_from_db()
            assert item.path == str(destination)
            assert item.name == destination.stem
            assert item.nfo_path == str(target_nfo)
            assert destination.read_bytes() == b"video"
            assert target_nfo.read_text() == "<movie><title>New</title></movie>"
            assert not source.exists()
            assert not nfo.exists()
            assert await MediaItem.filter(lib=lib).count() == 1
            assert not await MediaEvent.filter(lib=lib).exists()
            assert events.empty()
            assert not monitor.is_scanning(lib.dir)
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_scan_retry(tmp_path, monkeypatch):
    enqueue = AsyncMock(side_effect=[RuntimeError("scan failed"), None])
    monitor = watcher.LibWatcher(None)
    monkeypatch.setattr(monitor, "_enqueue_events", enqueue)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            monitor._scanning_paths = [lib.dir]

            with pytest.raises(RuntimeError, match="scan failed"):
                await monitor.scan_directory(lib)
            assert not monitor.is_scanning(lib.dir)
            await monitor.scan_directory(lib)

            assert not monitor.is_scanning(lib.dir)
            assert enqueue.await_count == 2
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_action_cancellation(tmp_path, monkeypatch):
    pending, removed = (str(tmp_path / name) for name in ("Pending", "Removed"))
    actions = {
        pending: watcher.LibAction.SCAN,
        removed: watcher.LibAction.REMOVE,
    }
    monitor = watcher.LibWatcher(None)
    monitor._watcher_actions = actions.copy()
    monitor._observers = {path: (None, Queue()) for path in actions}
    scan_action = AsyncMock(side_effect=asyncio.CancelledError)
    remove_action = AsyncMock()
    pause = AsyncMock(side_effect=RuntimeError("unexpected retry"))
    monkeypatch.setattr(monitor, "scan_directory", scan_action)
    monkeypatch.setattr(monitor, "remove_observer", remove_action)
    monkeypatch.setattr(watcher.asyncio, "sleep", pause)

    asyncio.run(monitor._listener())

    scan_action.assert_awaited_once_with(pending)
    remove_action.assert_not_awaited()
    pause.assert_not_awaited()
    assert monitor._watcher_actions == actions


@pytest.mark.parametrize("suffix", [".mkv", ".nfo"])
@pytest.mark.parametrize("source_state", ["file", "symlink", "missing"])
@pytest.mark.parametrize("destination_exists", [False, True])
def test_move_source(tmp_path, monkeypatch, suffix, source_state, destination_exists):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    monkeypatch.setattr(
        KaloscopeConfig, "get", lambda: SimpleNamespace(filesystem_trash_mode=False)
    )
    source = tmp_path / "Source.mkv"
    nfo = source.with_suffix(".nfo")
    source.write_bytes(b"replacement video")
    nfo_content = "<movie><title>Replacement</title></movie>"
    nfo.write_text(nfo_content)
    mtime = datetime.fromtimestamp(nfo.stat().st_mtime, tz=UTC)
    event_source = source.with_suffix(suffix)
    if source_state != "file":
        event_source.unlink()
        if source_state == "symlink":
            event_source.symlink_to(tmp_path / "missing-target")
    destination = tmp_path / "Moved.mkv"
    destination_nfo = destination.with_suffix(".nfo")
    if destination_exists:
        destination.write_bytes(b"moved video")
        destination_nfo.write_text("<movie><title>Moved</title></movie>")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                dir=str(tmp_path),
                path=str(source),
                name=source.stem,
                title="Replacement",
                nfo_path=str(nfo),
                nfo_mtime=mtime,
            )
            user = await User.create(
                username="viewer", password="test", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user, rel_type=HistoryType.VIDEO, rel_id=item.id, position=42
            )
            if suffix == ".nfo" and destination_exists:
                await MediaItem.create(
                    lib=lib,
                    dir=str(tmp_path),
                    path=str(destination),
                    name=destination.stem,
                )
            event = MediaEvent(
                lib=lib,
                src_path=str(event_source),
                dest_path=str(destination.with_suffix(suffix)),
                event_type="moved",
            )

            result = await watcher._handle_moved(event)

            current = await MediaItem.get_or_none(id=item.id)
            if source_state == "missing" and suffix == ".mkv":
                assert current is None
                assert not await UserHistory.filter(id=history.id).exists()
                assert not nfo.exists()
            else:
                assert current is not None
                assert current.path == str(source)
                assert current.title == "Replacement"
                assert current.nfo_path == (
                    None if source_state == "missing" else str(nfo)
                )
                assert current.nfo_mtime == (
                    None if source_state == "missing" else mtime
                )
                await history.refresh_from_db()
                assert history.rel_id == item.id
                assert history.position == 42
            if source_state != "missing":
                if source_state == "symlink":
                    assert event_source.is_symlink()
                else:
                    assert event_source.is_file()
                if suffix == ".mkv" or source_state == "file":
                    assert nfo.read_text() == nfo_content
            if destination_exists:
                target = await MediaItem.get(path=str(destination))
                assert destination.read_bytes() == b"moved video"
                if suffix == ".nfo":
                    assert target.nfo_path == str(destination_nfo)
                    assert target.title == "Moved"
                else:
                    assert [info.item_path for info in result] == [str(destination)]
            else:
                assert result is None
                assert not await MediaItem.filter(path=str(destination)).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("backfill_nfo_events", [False, True])
@pytest.mark.parametrize("missing", ["hash", "size", "both", None])
def test_hash_scan(tmp_path, monkeypatch, backfill_nfo_events, missing):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    digest = hashlib.md5(b"video").hexdigest()
    stored_hash = "previous" if missing is None else digest
    stored_size = 99 if missing is None else len(b"video")
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>New</title></movie>")
    cache = tmp_path / "custom.json"
    cache.write_text("[]")
    cached_meta = {"episode_id": 42}

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(tmp_path),
                name=source.stem,
                hash=None if missing in ("hash", "both") else stored_hash,
                size=None if missing in ("size", "both") else stored_size,
                danmaku_path=str(cache),
                danmaku_meta=cached_meta,
            )
            await shelver.update_metadata(lib, nfo)
            monitor = watcher.LibWatcher(None)
            events = Queue()
            monitor._observers = {lib.dir: (None, events)}
            monitor._scanning_paths = []

            await monitor.scan_directory(lib, backfill_nfo_events=backfill_nfo_events)
            while not events.empty():
                await watcher.consume_event(events.get_nowait())

            await item.refresh_from_db()
            assert item.path == str(source)
            assert item.hash == stored_hash
            assert item.size == stored_size
            assert item.danmaku_path == str(cache)
            assert item.danmaku_meta == cached_meta
            assert cache.read_text() == "[]"
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("delivery", ["persisted", "queued", "duplicate"])
@pytest.mark.parametrize("fail_once", [False, True])
def test_event_reload(tmp_path, monkeypatch, delivery, fail_once):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"video")
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>Updated</title></movie>")
    fire = AsyncMock()
    pause = AsyncMock()
    attempts = []
    events = Queue()
    update_metadata = watcher.update_metadata
    consume_event = watcher.consume_event

    async def update(lib, path):
        attempts.append((lib.id, lib.name, str(path)))
        if fail_once and len(attempts) == 1:
            raise OSError("temporary metadata failure")
        return await update_metadata(lib, path)

    async def finish(event):
        await consume_event(event)
        if events.empty():
            raise asyncio.CancelledError

    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr(watcher, "update_metadata", update)
    monkeypatch.setattr(watcher, "consume_event", finish)
    monkeypatch.setattr(watcher.asyncio, "sleep", pause)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Original", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            other = await MediaLib.create(
                name="Other",
                dir=str(tmp_path / "other"),
                lib_type=LibType.MOVIE,
                priority=2,
            )
            item = await MediaItem.create(
                lib=lib, path=str(source), dir=lib.dir, name=source.stem, title="Old"
            )
            pending = await MediaEvent.create(
                lib=lib, src_path=str(nfo), event_type="modified"
            )
            other_event = await MediaEvent.create(
                lib=other, src_path=str(tmp_path / "Other.nfo"), event_type="modified"
            )
            await MediaLib.filter(id=lib.id).update(name="Current")
            if delivery != "persisted":
                events.put(pending)
            if delivery == "duplicate":
                events.put(pending)
            monitor = watcher.LibWatcher(None)

            await asyncio.wait_for(monitor._event_consumer(lib.id, events), 3)

            await item.refresh_from_db()
            assert item.title == "Updated"
            assert item.nfo_path == str(nfo)
            assert not await MediaEvent.filter(lib_id=lib.id).exists()
            assert await MediaEvent.filter(id=other_event.id).exists()
            assert attempts == [(lib.id, "Current", str(nfo))] * (2 if fail_once else 1)
            assert [call.args[0] for call in pause.await_args_list] == (
                [5] if fail_once else [1] if delivery == "duplicate" else []
            )
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("state", ["new", "existing", "legacy"])
@pytest.mark.parametrize(
    ("lib_type", "relative_path"),
    [
        (LibType.MOVIE, "Movie.mkv"),
        (LibType.MOVIE, "Movie/Movie.mkv"),
        (LibType.TV_SHOW, "Show/S01E01.mkv"),
        (LibType.TV_SHOW, "Show/Season 01/S01E01.mkv"),
    ],
)
def test_synchronous_ingest(tmp_path, monkeypatch, state, lib_type, relative_path):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    video = tmp_path / relative_path
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    fired = []
    nodes = {"movie": MovieNode, "tvshow": TVShowNode, "episode": EpisodeNode}

    async def fire(*_args, bootparams):
        item = await MediaItem.get(path=bootparams["item_path"])
        assert bootparams["item_id"] == item.id
        fired.append(item.id)
        if bootparams["nfo_type"] is None:
            return

        nfo_path = Path(bootparams["nfo_path"])
        params = dict(bootparams)
        if state == "legacy":
            params.pop("item_id")
        else:
            params["nfo_path"] = str(tmp_path / "stale" / nfo_path.name)
        context = await Context.create(1, params)
        await nodes[bootparams["nfo_type"]].execute(
            node_data={"response": '[{"title": "Generated"}]', "force_end": False},
            context=context,
        )
        assert etree.parse(nfo_path).getroot().findtext("title") == "Generated"
        assert not (tmp_path / "stale").exists()

    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            if state == "existing":
                await get_handler(lib_type).gen_items(lib, video)
            original_ids = await MediaItem.all().values_list("id", flat=True)
            event = await MediaEvent.create(
                lib=lib, src_path=str(video), event_type="created"
            )

            await asyncio.wait_for(watcher.consume_event(event), timeout=3)

            current_ids = await MediaItem.all().values_list("id", flat=True)
            assert sorted(fired) == sorted(current_ids)
            if original_ids:
                assert sorted(current_ids) == sorted(original_ids)
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["pending", "published", "conflict"])
@pytest.mark.parametrize("event_type", ["deleted", "ingest", "organize"])
def test_event_recovery(tmp_path, monkeypatch, stage, event_type):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>New</title></movie>")
    destination = tmp_path / "New.mkv"
    target_nfo = destination.with_suffix(".nfo")
    mapping = {str(source): str(destination), str(nfo): str(target_nfo)}

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=lib.dir,
                name=source.stem,
                title="Old",
                nfo_path=str(nfo),
                hash=hashlib.md5(b"video").hexdigest(),
                size=len(b"video"),
            )
            user = await User.create(
                username="viewer", password="test", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user, rel_type=HistoryType.VIDEO, rel_id=item.id, position=42
            )
            journal = await MediaEvent.create(
                lib=lib,
                src_path=str(source),
                event_type="organize",
                payload={
                    "moves": [
                        {
                            "src": old,
                            "dst": new,
                            "identity": organizer._fingerprint(Path(old)),
                        }
                        for old, new in mapping.items()
                    ],
                    "updates": [
                        {
                            "id": item.id,
                            "path": str(destination),
                            "name": destination.stem,
                            "title": "New",
                            "nfo_path": str(target_nfo),
                        }
                    ],
                    "parent": None,
                    "delete_parent": None,
                    "mapping": mapping,
                    "creates": [],
                    "symlinks": [],
                    "nfo_edits": [],
                },
            )
            params = {
                "item_id": item.id,
                "item_path": str(source),
                "item_name": source.stem,
                "nfo_path": str(nfo),
                "title": "Old",
            }
            event = journal
            if event_type != "organize":
                event = await MediaEvent.create(
                    lib=lib,
                    src_path=str(source),
                    event_type=event_type,
                    payload={"bootparams": [params]}
                    if event_type == "ingest"
                    else None,
                )

            if stage == "published":
                for old, new in mapping.items():
                    Path(old).rename(new)
            elif stage == "conflict":
                destination.write_bytes(b"unrelated")
                with pytest.raises(organizer.OrganizePendingError):
                    await watcher.consume_event(event)
                await item.refresh_from_db()
                assert item.path == str(source)
                assert item.title == "Old"
                assert source.read_bytes() == b"video"
                assert destination.read_bytes() == b"unrelated"
                assert await MediaEvent.filter(id=journal.id).exists()
                assert await MediaEvent.filter(id=event.id).exists()
                fire.assert_not_awaited()
                destination.unlink()

            await watcher.consume_event(event)
            await watcher.consume_event(event)

            await item.refresh_from_db()
            await history.refresh_from_db()
            assert item.path == str(destination)
            assert item.nfo_path == str(target_nfo)
            assert history.rel_id == item.id
            assert history.position == 42
            assert destination.read_bytes() == b"video"
            assert target_nfo.read_text() == "<movie><title>New</title></movie>"
            assert not source.exists()
            assert not nfo.exists()
            assert await MediaItem.filter(lib=lib).count() == 1
            assert not await MediaEvent.filter(lib=lib).exists()
            if event_type == "ingest":
                fire.assert_awaited_once()
                assert fire.call_args.kwargs["bootparams"] == {
                    **params,
                    "item_path": str(destination),
                    "item_name": destination.stem,
                    "nfo_path": str(target_nfo),
                    "title": "New",
                }
            else:
                fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("event_type", ["created", "modified"])
@pytest.mark.parametrize("change", ["updated", "event_removed", "library_removed"])
def test_event_lock(tmp_path, monkeypatch, event_type, change):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    nfo = video.with_suffix(".nfo")
    nfo.write_text("<movie><title>Updated</title></movie>")
    update_metadata = watcher.update_metadata
    attempts = []

    async def update(lib, path):
        with pytest.raises(Timeout):
            async with await library_lock(lib.dir).acquire(timeout=0):
                pass
        attempts.append(lib.name)
        return await update_metadata(lib, path)

    async def fire(*_args, **_kwargs):
        async with await library_lock(str(tmp_path)).acquire(timeout=1):
            pass

    fire = AsyncMock(side_effect=fire)
    monkeypatch.setattr(watcher, "update_metadata", update)
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        waiting = asyncio.Event()

        def waiting_lock(directory):
            waiting.set()
            return library_lock(directory)

        monkeypatch.setattr(watcher, "library_lock", waiting_lock)
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        request = None
        try:
            lib = await MediaLib.create(
                name="Original", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib, path=str(video), dir=lib.dir, name=video.stem, title="Original"
            )
            event = await MediaEvent.create(
                lib=lib,
                src_path=str(video if event_type == "created" else nfo),
                event_type=event_type,
            )

            async with library_lock(lib.dir):
                request = asyncio.create_task(watcher.consume_event(event))
                await asyncio.wait_for(waiting.wait(), timeout=3)
                assert not request.done()
                assert await MediaEvent.filter(id=event.id).exists()
                assert (await MediaItem.get(id=item.id)).title == "Original"
                assert attempts == []
                fire.assert_not_awaited()
                if change == "updated":
                    await MediaLib.filter(id=lib.id).update(name="Current")
                elif change == "event_removed":
                    await event.delete()
                else:
                    await lib.delete()

            await asyncio.wait_for(request, timeout=3)

            assert not await MediaEvent.filter(id=event.id).exists()
            if change == "updated":
                await item.refresh_from_db()
                assert item.title == "Updated"
                assert item.nfo_path == str(nfo)
                assert attempts == ["Current"]
                assert fire.await_count == (1 if event_type == "created" else 0)
            else:
                assert attempts == []
                fire.assert_not_awaited()
                if change == "event_removed":
                    await item.refresh_from_db()
                    assert item.title == "Original"
                    assert item.nfo_path is None
                else:
                    assert not await MediaItem.filter(id=item.id).exists()
        finally:
            if request is not None:
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("event_type", ["created", "modified"])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_event_retry(tmp_path, monkeypatch, event_type, error):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    nfo = video.with_suffix(".nfo")
    nfo.write_text("<movie><title>Updated</title></movie>")
    update_metadata = watcher.update_metadata
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def interrupted(lib, path):
        await update_metadata(lib, path)
        raise error("metadata update interrupted")

    monkeypatch.setattr(watcher, "update_metadata", interrupted)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib, path=str(video), dir=lib.dir, name=video.stem, title="Original"
            )
            event = await MediaEvent.create(
                lib=lib,
                src_path=str(video if event_type == "created" else nfo),
                event_type=event_type,
            )

            with pytest.raises(error, match="metadata update interrupted"):
                await watcher.consume_event(event)

            async with await library_lock(lib.dir).acquire(timeout=1):
                assert await MediaEvent.filter(id=event.id).exists()
                await item.refresh_from_db()
                assert item.title == "Original"
                assert item.nfo_path is None
            fire.assert_not_awaited()
            monkeypatch.setattr(watcher, "update_metadata", update_metadata)

            await asyncio.wait_for(watcher.consume_event(event), timeout=3)

            await item.refresh_from_db()
            assert item.title == "Updated"
            assert item.nfo_path == str(nfo)
            assert not await MediaEvent.filter(id=event.id).exists()
            assert fire.await_count == (1 if event_type == "created" else 0)
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("explicit_cache", [False, True])
@pytest.mark.parametrize("replacement", ["recreated", "moved", "temporary"])
@pytest.mark.parametrize(
    ("missing", "content", "preserved"),
    [
        (None, b"old video", True),
        (None, b"new video", False),
        (None, b"a longer replacement video", False),
        ("hash", b"old video", True),
        ("size", b"old video", True),
        ("both", b"old video", True),
        ("hash", b"a longer replacement video", False),
        ("size", b"new video", False),
    ],
)
def test_replaced_video(
    tmp_path, monkeypatch, explicit_cache, replacement, missing, content, preserved
):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"old video")
    nfo = source.with_suffix(".nfo")
    nfo_content = "<movie><title>Movie</title></movie>"
    nfo.write_text(nfo_content)
    default_cache = tmp_path / ".Movie.json"
    default_cache.write_text("[]")
    cache = tmp_path / "custom.json" if explicit_cache else default_cache
    cache.write_text("[]")
    cached_meta = {"episode_id": 42}

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(tmp_path),
                name=source.stem,
                hash=(
                    None
                    if missing in {"hash", "both"}
                    else hashlib.md5(source.read_bytes()).hexdigest()
                ),
                size=None if missing in {"size", "both"} else source.stat().st_size,
                nfo_path=str(nfo),
                danmaku_path=str(cache) if explicit_cache else None,
                danmaku_meta=cached_meta,
            )
            user = await User.create(
                username="viewer", password="test", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user, rel_type=HistoryType.VIDEO, rel_id=item.id, position=42
            )
            events = []
            if replacement == "recreated":
                source.unlink()
                events.append(
                    await MediaEvent.create(
                        lib=lib, src_path=str(source), event_type="deleted"
                    )
                )
                source.write_bytes(content)
                events.append(
                    await MediaEvent.create(
                        lib=lib, src_path=str(source), event_type="created"
                    )
                )
            else:
                temporary = tmp_path / (
                    "Replacement.mkv" if replacement == "moved" else ".replacement.tmp"
                )
                temporary.write_bytes(content)
                temporary.replace(source)
                event = watcher.get_handler(lib.lib_type).filter_event(
                    FileMovedEvent(str(temporary), str(source)), base_path=lib.dir
                )
                events.append(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=event.src_path,
                        dest_path=event.dest_path,
                        event_type=event.event_type,
                    )
                )

            for event in events:
                await asyncio.wait_for(watcher.consume_event(event), timeout=3)

            current = await MediaItem.get(id=item.id)
            assert current.path == str(source)
            assert current.hash == hashlib.md5(content).hexdigest()
            assert current.size == len(content)
            assert current.nfo_path == str(nfo)
            assert nfo.read_text() == nfo_content
            await history.refresh_from_db()
            assert history.rel_id == item.id
            assert history.position == 42
            if preserved:
                assert cache.is_file()
                assert default_cache.is_file()
                assert current.danmaku_meta == cached_meta
                assert current.danmaku_path == (str(cache) if explicit_cache else None)
            else:
                assert not cache.exists()
                assert not default_cache.exists()
                assert current.danmaku_path is None
                assert current.danmaku_meta is None
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("missing_parent", [False, True])
def test_missing_nfo(tmp_path, monkeypatch, missing_parent):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    folder = tmp_path / "Series"
    folder.mkdir()
    nfo = folder / "Series.nfo"
    if not missing_parent:
        nfo.write_text("<tvshow><title>Series</title></tvshow>")
    video = folder / "S01E01.mkv"
    video.write_bytes(b"video")
    if missing_parent:
        video.with_suffix(".nfo").write_text(
            "<episodedetails><title>Pilot</title><season>1</season>"
            "<episode>1</episode></episodedetails>"
        )

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Shows", dir=str(tmp_path), lib_type=LibType.TV_SHOW, priority=1
            )
            parent = await MediaItem.create(
                lib=lib,
                path=str(folder),
                dir=str(folder),
                name=folder.name,
                title="Series",
                nfo_path=None if missing_parent else str(nfo),
                season=1,
            )
            child = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(folder),
                name=video.stem,
                nfo_path=str(video.with_suffix(".nfo")) if missing_parent else None,
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(video), event_type="created"
            )
            await watcher.consume_event(event)
            params = [call.kwargs["bootparams"] for call in fire.call_args_list]
            expected_id = parent.id if missing_parent else child.id
            expected_type = "tvshow" if missing_parent else "episode"
            assert any(
                p["item_id"] == expected_id and p["nfo_type"] == expected_type
                for p in params
            )
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_recreated_link(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "Movie.mkv"
    source.symlink_to(tmp_path / "missing.mkv")
    nfo = source.with_suffix(".nfo")
    nfo_content = "<movie><title>Movie</title></movie>"
    nfo.write_text(nfo_content)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=lib.dir,
                name=source.stem,
                hash="original",
                size=10,
                nfo_path=str(nfo),
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(source), event_type="deleted"
            )

            await asyncio.wait_for(watcher.consume_event(event), timeout=3)

            await item.refresh_from_db()
            assert item.path == str(source)
            assert (item.hash, item.size) == ("original", 10)
            assert item.nfo_path == str(nfo)
            assert nfo.read_text() == nfo_content
            assert source.is_symlink()
            assert not source.exists()
            assert not await MediaEvent.filter(id=event.id).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("interrupted_index", [0, 1])
def test_workflow_retry(tmp_path, monkeypatch, restart, error, interrupted_index):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    video = tmp_path / "Series" / "Series S01E01.mkv"
    video.parent.mkdir()
    video.write_bytes(b"video")
    video.parent.joinpath("Series.nfo").write_text(
        "<tvshow><title>Series</title></tvshow>"
    )
    video.with_suffix(".nfo").write_text(
        "<episodedetails><title>Pilot</title><season>1</season>"
        "<episode>1</episode></episodedetails>"
    )
    index = AsyncMock(wraps=watcher._handle_created)
    monkeypatch.setattr(watcher, "_handle_created", index)

    async def run():
        db_url = f"sqlite://{tmp_path / 'media.sqlite3'}"
        await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Shows", dir=str(tmp_path), lib_type=LibType.TV_SHOW, priority=1
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(video), event_type="created"
            )
            fired = []
            interrupted = False

            async def fire(*_args, bootparams):
                nonlocal interrupted
                async with await library_lock(lib.dir).acquire(timeout=1):
                    pending = await MediaEvent.get(id=event.id)
                    assert pending.event_type == "ingest"
                    assert (
                        pending.payload["bootparams"][0]["item_id"]
                        == bootparams["item_id"]
                    )
                if len(fired) == interrupted_index and not interrupted:
                    interrupted = True
                    raise error("workflow interrupted")
                fired.append(bootparams["item_id"])

            monkeypatch.setattr(FlowTriggerService, "fire", fire)

            with pytest.raises(error, match="workflow interrupted"):
                await watcher.consume_event(event)

            items = await MediaItem.filter(lib_id=lib.id).order_by("id")
            ids = [item.id for item in items]
            assert len(ids) == 2
            assert [item.title for item in items] == ["Series", "Pilot"]
            assert all(item.nfo_path for item in items)
            pending = await MediaEvent.get(id=event.id)
            assert pending.event_type == "ingest"
            assert [params["item_id"] for params in pending.payload["bootparams"]] == (
                ids[interrupted_index:]
            )
            assert fired == ids[:interrupted_index]

            if restart:
                await Tortoise.close_connections()
                await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
                lib = await MediaLib.get(id=lib.id)
                monitor = watcher.LibWatcher(None)
                events = await monitor._create_events(lib)
                assert events.qsize() == 1
                pending = events.get_nowait()
            else:
                pending = event

            await watcher.consume_event(pending)

            assert fired == ids
            assert index.await_count == 1
            assert (
                await MediaItem.filter(lib_id=lib.id)
                .order_by("id")
                .values_list("id", flat=True)
                == ids
            )
            assert not await MediaEvent.filter(lib_id=lib.id).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
def test_ingest_persistence(tmp_path, monkeypatch, error):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    nfo = video.with_suffix(".nfo")
    nfo.write_text("<movie><title>Updated</title></movie>")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    save = MediaEvent.save

    async def interrupted(event, *args, **kwargs):
        await save(event, *args, **kwargs)
        if event.event_type == "ingest":
            raise error("journal interrupted")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(video), event_type="created"
            )

            with monkeypatch.context() as patcher:
                patcher.setattr(MediaEvent, "save", interrupted)
                with pytest.raises(error, match="journal interrupted"):
                    await watcher.consume_event(event)

            pending = await MediaEvent.get(id=event.id)
            assert pending.event_type == "created"
            assert pending.payload is None
            assert not await MediaItem.filter(lib_id=lib.id).exists()
            fire.assert_not_awaited()

            await watcher.consume_event(event)

            item = await MediaItem.get(lib_id=lib.id)
            assert item.title == "Updated"
            assert item.nfo_path == str(nfo)
            assert fire.await_count == 1
            assert fire.call_args.kwargs["bootparams"]["item_id"] == item.id
            assert not await MediaEvent.filter(id=event.id).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("lib_type", "nfo"),
    [
        (LibType.MOVIE, "default"),
        (LibType.MOVIE, "recorded"),
        (LibType.MOVIE, "shared"),
        (LibType.MOVIE, "unrequested"),
        (LibType.TV_SHOW, "default"),
        (LibType.TV_SHOW, "recorded"),
    ],
)
def test_ingest_parameters(tmp_path, monkeypatch, lib_type, nfo):
    old = tmp_path / "Old.mkv"
    old.write_bytes(b"unrelated replacement")
    video = tmp_path / "Current" / "Current S02E03.mkv"
    video.parent.mkdir()
    video.write_bytes(b"current video")
    is_show = lib_type == LibType.TV_SHOW
    parent_nfo = video.parent / "Current.nfo" if nfo == "shared" or is_show else None
    recorded_nfo = video.parent / "metadata.nfo" if nfo == "recorded" else None
    for path in (parent_nfo, recorded_nfo):
        if path is not None:
            path.write_text("<metadata/>")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            parent = await MediaItem.create(
                lib=lib,
                path=str(video.parent),
                dir=str(video.parent),
                name=video.parent.name,
                title="Current series",
                year=2024,
                unique_id="42",
                nfo_source="tmdb",
                nfo_path=str(parent_nfo) if parent_nfo else None,
            )
            item = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(video.parent),
                name=video.stem,
                title="Current title",
                year=2026,
                season=2 if is_show else None,
                episode=3 if is_show else None,
                nfo_path=str(recorded_nfo) if recorded_nfo else None,
            )
            params = {
                "item_id": item.id,
                "item_path": str(old),
                "item_name": old.stem,
                "nfo_path": str(old.with_suffix(".nfo"))
                if nfo != "unrequested"
                else None,
                "nfo_type": (
                    None if nfo == "unrequested" else "episode" if is_show else "movie"
                ),
                "language": "en-US",
                "title": "Old title",
                "year": 1990,
                "season": 1 if is_show else None,
                "episode": 1 if is_show else None,
                "series_id": "old-series" if is_show else None,
                "nfo_source": "old-source" if is_show else None,
                "page_num": 1,
                "page_size": 1,
            }
            event = await MediaEvent.create(
                lib=lib,
                src_path=str(old),
                event_type="ingest",
                payload={"bootparams": [params]},
            )

            async def fire(*_args, bootparams):
                async with await library_lock(lib.dir).acquire(timeout=1):
                    pending = await MediaEvent.get(id=event.id)
                    assert pending.payload["bootparams"] == [bootparams]
                    await item.refresh_from_db()
                    assert item.hash == hashlib.md5(b"current video").hexdigest()
                    assert item.size == len(b"current video")

            fire = AsyncMock(side_effect=fire)
            monkeypatch.setattr(FlowTriggerService, "fire", fire)

            await watcher.consume_event(event)

            assert fire.await_count == 1
            result = fire.call_args.kwargs["bootparams"]
            expected_nfo = recorded_nfo or (
                parent_nfo if nfo == "shared" else video.with_suffix(".nfo")
            )
            assert result == {
                **params,
                "item_path": str(video),
                "item_name": video.stem,
                "nfo_path": str(expected_nfo) if nfo != "unrequested" else None,
                "title": "Current series" if is_show else "Current title",
                "year": 2024 if is_show else 2026,
                "season": 2 if is_show else None,
                "episode": 3 if is_show else None,
                "series_id": "42" if is_show else None,
                "nfo_source": "tmdb" if is_show else None,
            }
            assert not await MediaEvent.filter(id=event.id).exists()
            assert old.read_bytes() == b"unrelated replacement"
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("state", ["removed", "other_library", "unbound"])
def test_ingest_scope(tmp_path, monkeypatch, state):
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            other = await MediaLib.create(
                name="Other",
                dir=str(tmp_path / "Other"),
                lib_type=LibType.MOVIE,
                priority=2,
            )
            item = await MediaItem.create(
                lib=other if state == "other_library" else lib,
                path=str(tmp_path / "Movie.mkv"),
                dir=str(tmp_path),
                name="Movie",
            )
            if state in {"removed", "unbound"}:
                await item.delete()
            params = {
                "item_id": None if state == "unbound" else item.id,
                "item_path": item.path,
                "item_name": item.name,
                "nfo_path": None,
            }
            event = await MediaEvent.create(
                lib=lib,
                src_path=item.path,
                event_type="ingest",
                payload={"bootparams": [params]},
            )

            await watcher.consume_event(event)

            if state == "unbound":
                assert fire.await_count == 1
                assert fire.call_args.kwargs["bootparams"] == params
            else:
                fire.assert_not_awaited()
            assert not await MediaEvent.filter(id=event.id).exists()
            if state == "other_library":
                assert await MediaItem.filter(id=item.id, lib=other).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("error", [OSError, asyncio.CancelledError])
def test_ingest_hash_retry(tmp_path, monkeypatch, error):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")
    video.with_suffix(".nfo").write_text("<movie><title>Movie</title></movie>")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        db_url = f"sqlite://{tmp_path / 'media.sqlite3'}"
        await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(video), event_type="created"
            )

            with monkeypatch.context() as patcher:
                patcher.setattr(
                    watcher.MediaItemService,
                    "refresh_hash_and_size",
                    AsyncMock(side_effect=error("hash interrupted")),
                )
                with pytest.raises(error, match="hash interrupted"):
                    await watcher.consume_event(event)

            item = await MediaItem.get(lib_id=lib.id)
            pending = await MediaEvent.get(id=event.id)
            assert pending.event_type == "ingest"
            assert pending.payload["bootparams"][0]["item_id"] == item.id
            assert item.hash is None and item.size is None
            fire.assert_not_awaited()

            await Tortoise.close_connections()
            await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
            await watcher.consume_event(pending)

            await item.refresh_from_db()
            assert item.hash == hashlib.md5(b"video").hexdigest()
            assert item.size == len(b"video")
            assert fire.await_count == 1
            assert fire.call_args.kwargs["bootparams"]["item_id"] == item.id
            assert not await MediaEvent.filter(id=event.id).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_ingest_paths(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    monkeypatch.setattr(watcher, "recover_organizing", AsyncMock(return_value={}))
    old = tmp_path / "old.mkv"
    old.write_bytes(b"video")
    old.with_suffix(".nfo").write_text("<movie><title>New</title></movie>")
    destination = tmp_path / "New.mkv"
    configs = []

    async def organize(lib, ids):
        configs.append(lib.rename_template)
        assert len(ids) == 1
        old.rename(destination)
        old.with_suffix(".nfo").rename(destination.with_suffix(".nfo"))
        await MediaItem.filter(id=ids[0]).update(
            path=str(destination),
            name="New",
            nfo_path=str(destination.with_suffix(".nfo")),
        )
        return {
            str(old): str(destination),
            str(old.with_suffix(".nfo")): str(destination.with_suffix(".nfo")),
        }

    monkeypatch.setattr(watcher, "organize_items", organize)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(old), event_type="created"
            )
            # the queued event contains the old library instance
            await MediaLib.filter(id=lib.id).update(rename_template="{{title}}")
            await watcher.consume_event(event)
            item = await MediaItem.get(lib_id=lib.id)
            original_id = item.id
            assert configs == ["{{title}}"]
            params = fire.call_args.kwargs["bootparams"]
            assert params["item_id"] == item.id
            assert params["item_path"] == str(destination)
            assert params["item_name"] == "New"
            assert params["title"] == "New"
            assert params["nfo_path"] == str(destination.with_suffix(".nfo"))
            for kind, source, target in [
                ("moved", old, destination),
                ("created", destination, None),
                ("deleted", old, None),
            ]:
                event = await MediaEvent.create(
                    lib=lib,
                    src_path=str(source),
                    dest_path=str(target) if target else None,
                    event_type=kind,
                )
                await watcher.consume_event(event)
            assert (await MediaItem.get(lib_id=lib.id)).id == original_id
            assert fire.await_count == 1
            assert await MediaEvent.all().count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("arrival", ["created", "moved"])
@pytest.mark.parametrize("recovery", [False, True])
def test_organization_arrival(tmp_path, monkeypatch, arrival, recovery):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    source = tmp_path / "old.mkv"
    source.write_bytes(b"first video")
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>New</title></movie>")
    destination = tmp_path / "New.mkv"

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            original = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(tmp_path),
                name=source.stem,
                nfo_path=str(nfo),
                hash=hashlib.md5(b"first video").hexdigest(),
                size=len(b"first video"),
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(nfo), event_type="modified"
            )
            if recovery:
                with monkeypatch.context() as patcher:
                    patcher.setattr(
                        organizer,
                        "_write_in_thread",
                        AsyncMock(side_effect=OSError("interrupted organization")),
                    )
                    with pytest.raises(organizer.OrganizePendingError):
                        await watcher.consume_event(event)
            events = Queue()
            handler = watcher.EventHandler(lib, asyncio.get_running_loop(), events)
            write = organizer._write_in_thread
            arrived = False

            async def write_and_arrive(function, *args):
                nonlocal arrived
                result = await write(function, *args)
                if function is organizer._move_files and not arrived:
                    arrived = True
                    await handler._persist(
                        FileMovedEvent(str(source), str(destination))
                    )
                    if arrival == "created":
                        source.write_bytes(b"second video")
                        incoming = FileCreatedEvent(str(source))
                    else:
                        staged = tmp_path / "incoming.mkv"
                        staged.write_bytes(b"second video")
                        staged.rename(source)
                        incoming = FileMovedEvent(str(staged), str(source))
                    await handler._persist(incoming)
                return result

            with monkeypatch.context() as patcher:
                patcher.setattr(organizer, "_write_in_thread", write_and_arrive)
                await watcher.consume_event(event)
            while not events.empty():
                await watcher.consume_event(events.get_nowait())

            await original.refresh_from_db()
            incoming = await MediaItem.get_or_none(lib=lib, path=str(source))
            assert incoming is not None
            assert incoming.id != original.id
            assert incoming.hash == hashlib.md5(b"second video").hexdigest()
            assert incoming.size == len(b"second video")
            assert original.path == str(destination)
            assert original.nfo_path == str(destination.with_suffix(".nfo"))
            assert original.hash == hashlib.md5(b"first video").hexdigest()
            assert source.read_bytes() == b"second video"
            assert destination.read_bytes() == b"first video"
            assert await MediaItem.filter(lib=lib).count() == 2
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_awaited_once()
            params = fire.call_args.kwargs["bootparams"]
            assert params["item_id"] == incoming.id
            assert params["item_path"] == str(source)
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("suffix", [".mkv", ".nfo"])
@pytest.mark.parametrize("destination_removed", [False, True])
def test_reused_move_source(tmp_path, monkeypatch, suffix, destination_removed):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    monkeypatch.setattr(
        KaloscopeConfig,
        "get",
        lambda: SimpleNamespace(filesystem_trash_mode=False),
    )
    source = tmp_path / "old.mkv"
    nfo = source.with_suffix(".nfo")
    source.write_bytes(b"first video")
    nfo.write_text("<movie><title>New</title><year>2026</year></movie>")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}} ({{year}})",
            )
            await watcher.consume_event(
                await MediaEvent.create(
                    lib=lib, src_path=str(source), event_type="created"
                )
            )
            original = await MediaItem.get(lib=lib)
            destination = tmp_path / "New (2026).mkv"
            assert original.path == str(destination)
            assert not source.exists()
            source.write_bytes(b"second video")
            nfo_content = "<movie><title>Second</title></movie>"
            nfo.write_text(nfo_content)
            await watcher.consume_event(
                await MediaEvent.create(
                    lib=lib, src_path=str(source), event_type="created"
                )
            )
            recreated = await MediaItem.get(lib=lib, path=str(source))
            assert recreated.id != original.id
            assert recreated.nfo_mtime is not None
            user = await User.create(
                username="viewer", password="test", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user,
                rel_type=HistoryType.VIDEO,
                rel_id=recreated.id,
                position=42,
            )
            if destination_removed:
                destination.unlink()
                await watcher.consume_event(
                    await MediaEvent.create(
                        lib=lib, src_path=str(destination), event_type="deleted"
                    )
                )
            fire.reset_mock()

            await watcher.consume_event(
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(source.with_suffix(suffix)),
                    dest_path=str(destination.with_suffix(suffix)),
                    event_type="moved",
                )
            )

            current = await MediaItem.get_or_none(id=recreated.id)
            assert current is not None
            assert current.path == str(source)
            assert current.nfo_path == str(nfo)
            assert current.nfo_mtime == recreated.nfo_mtime
            if destination_removed:
                assert not await MediaItem.filter(id=original.id).exists()
                assert not destination.exists()
                assert await MediaItem.filter(lib=lib).count() == 1
            else:
                assert (await MediaItem.get(path=str(destination))).id == original.id
                assert destination.read_bytes() == b"first video"
                assert await MediaItem.filter(lib=lib).count() == 2
            await history.refresh_from_db()
            assert history.rel_id == recreated.id
            assert history.position == 42
            assert source.read_bytes() == b"second video"
            assert nfo.read_text() == nfo_content
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("suffix", [".mkv", ".nfo"])
def test_missing_move_source(tmp_path, monkeypatch, suffix):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr(FlowTriggerService, "fire", AsyncMock())
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    monkeypatch.setattr(
        KaloscopeConfig,
        "get",
        lambda: SimpleNamespace(filesystem_trash_mode=False),
    )
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>Movie</title></movie>")
    destination = tmp_path / "New.mkv"
    destination.write_bytes(b"video")
    destination_nfo = destination.with_suffix(".nfo")
    destination_nfo.write_text("<movie><title>Movie</title></movie>")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            for path in (source, destination):
                await watcher.consume_event(
                    await MediaEvent.create(
                        lib=lib, src_path=str(path), event_type="created"
                    )
                )
            original = await MediaItem.get(lib=lib, path=str(source))
            known = await MediaItem.get(lib=lib, path=str(destination))
            user = await User.create(
                username="viewer", password="test", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user, rel_type=HistoryType.VIDEO, rel_id=original.id, position=42
            )
            source.with_suffix(suffix).replace(destination.with_suffix(suffix))

            await watcher.consume_event(
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(source.with_suffix(suffix)),
                    dest_path=str(destination.with_suffix(suffix)),
                    event_type="moved",
                )
            )

            if suffix == ".mkv":
                assert not await MediaItem.filter(id=original.id).exists()
                assert not await UserHistory.filter(id=history.id).exists()
                assert not nfo.exists()
            else:
                await original.refresh_from_db()
                assert original.nfo_path is None
                assert original.nfo_mtime is None
                assert await UserHistory.filter(id=history.id).exists()
                assert source.is_file()
            current = await MediaItem.get(id=known.id)
            assert current.path == str(destination)
            assert current.nfo_path == str(destination_nfo)
            assert current.nfo_mtime is not None
            assert destination.is_file()
            assert destination_nfo.is_file()
            assert not await MediaEvent.filter(lib=lib).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("explicit_cache", [False, True])
@pytest.mark.parametrize("arrival", ["moved", "created"])
@pytest.mark.parametrize(
    ("missing", "content", "preserved"),
    [
        ("hash", b"old video", True),
        ("size", b"old video", True),
        ("both", b"old video", True),
        ("hash", b"a longer replacement video", False),
        ("size", b"new video", False),
    ],
)
def test_organized_cache(
    tmp_path, monkeypatch, explicit_cache, arrival, missing, content, preserved
):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "old.mkv"
    source.write_bytes(content)
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>New</title></movie>")
    cache = tmp_path / ("custom.json" if explicit_cache else ".old.json")
    cache_content = '[{"text":"Cached comment","start":1000}]'
    cache.write_text(cache_content)
    cached_meta = {"episode_id": 42}
    destination = tmp_path / "New.mkv"
    organized_cache = cache if explicit_cache else tmp_path / ".New.json"

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=lib.dir,
                name=source.stem,
                hash=(
                    hashlib.md5(b"old video").hexdigest() if missing == "size" else None
                ),
                size=len(b"old video") if missing == "hash" else None,
                nfo_path=str(nfo),
                danmaku_path=str(cache) if explicit_cache else None,
                danmaku_meta=cached_meta,
            )
            nfo_event = await MediaEvent.create(
                lib=lib, src_path=str(nfo), event_type="modified"
            )

            await watcher.consume_event(nfo_event)
            assert destination.read_bytes() == content
            assert organized_cache.read_text() == cache_content
            event = await MediaEvent.create(
                lib=lib,
                src_path=str(source if arrival == "moved" else destination),
                dest_path=str(destination) if arrival == "moved" else None,
                event_type=arrival,
            )
            await watcher.consume_event(event)

            await item.refresh_from_db()
            assert item.path == str(destination)
            assert item.hash == hashlib.md5(content).hexdigest()
            assert item.size == len(content)
            if preserved:
                assert organized_cache.read_text() == cache_content
                assert item.danmaku_meta == cached_meta
                assert item.danmaku_path == (str(cache) if explicit_cache else None)
            else:
                assert not organized_cache.exists()
                assert item.danmaku_meta is None
                assert item.danmaku_path is None
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("cache_name", "organized_cache"),
    [(".old.json", ".New.json"), ("custom.json", "custom.json")],
)
def test_reused_cache(tmp_path, monkeypatch, cache_name, organized_cache):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr(FlowTriggerService, "fire", AsyncMock())
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    source = tmp_path / "old.mkv"
    source.write_bytes(b"original movie")
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>New</title></movie>")
    cache = tmp_path / cache_name
    cache.write_text('[{"text":"Original movie comment","start":1000}]')

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=lib.dir,
                name=source.stem,
                nfo_path=str(nfo),
                danmaku_path=str(cache),
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(nfo), event_type="modified"
            )

            await watcher.consume_event(event)
            await item.refresh_from_db()
            source.write_bytes(b"another movie")
            event = await MediaEvent.create(
                lib=lib, src_path=str(source), event_type="created"
            )
            await watcher.consume_event(event)
            replacement = await MediaItem.get(lib=lib, path=str(source))
            original_comments = await DanmakuService.match_danmakus(item.path)
            replacement_comments = await DanmakuService.match_danmakus(replacement.path)

            assert replacement.id != item.id
            assert replacement_comments.comments == []
            assert [comment.text for comment in original_comments.comments] == [
                "Original movie comment"
            ]
            assert item.path == str(tmp_path / "New.mkv")
            assert item.danmaku_path == str(tmp_path / organized_cache)
            assert not (tmp_path / ".old.json").exists()
            assert not await MediaEvent.filter(lib=lib).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_ingest_shared_nfo(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    source.with_suffix(".nfo").write_text(
        "<movie><title>Film</title><year>2026</year></movie>"
    )

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}} ({{year}})/{{title}}",
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(source), event_type="created"
            )

            await watcher.consume_event(event)

            params = fire.call_args.kwargs["bootparams"]
            folder = tmp_path / "Film (2026)"
            assert params["item_path"] == str(folder / "Film.mkv")
            assert params["nfo_path"] == str(folder / "Film (2026).nfo")
            assert Path(params["nfo_path"]).is_file()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("lib_type", "source_name", "template", "target_name"),
    [
        (LibType.MOVIE, "old.mkv", "{{title}}/{{title}}", "Film/Film.mkv"),
        (LibType.MOVIE, "Old/old.mkv", "{{title}}", "Film.mkv"),
        (
            LibType.TV_SHOW,
            "Old/S01E02.mkv",
            "{{show_title}}/Season {{season}}/{{episode_code}} - {{title}}",
            "Series/Season 01/S01E02 - Pilot.mkv",
        ),
        (
            LibType.TV_SHOW,
            "Old/Season 01/S01E02.mkv",
            "{{show_title}}/{{episode_code}} - {{title}}",
            "Series/S01E02 - Pilot.mkv",
        ),
    ],
)
def test_organization_rescan(
    tmp_path, monkeypatch, lib_type, source_name, template, target_name
):
    """Exercise actual scanning, metadata parsing, organization and late events."""
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    source = tmp_path / source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")
    parent_nfo = (
        source.with_suffix(".nfo")
        if source.parent == tmp_path
        else source.parent / f"{source.parent.name}.nfo"
    )
    tag = "movie" if lib_type == LibType.MOVIE else "tvshow"
    title = "Film" if lib_type == LibType.MOVIE else "Series"
    parent_nfo.write_text(
        f"<{tag}><title>{title}</title><year>2026</year>"
        '<uniqueid type="tmdb" default="true">42</uniqueid></' + tag + ">"
    )
    if lib_type == LibType.TV_SHOW:
        source.with_suffix(".nfo").write_text(
            "<episodedetails><title>Pilot</title><season>1</season>"
            "<episode>2</episode></episodedetails>"
        )

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            monitor = watcher.LibWatcher(None)
            events = Queue()
            monitor._observers = {lib.dir: (None, events)}
            monitor._scanning_paths = []

            async def scan():
                await monitor.scan_directory(lib)
                while not events.empty():
                    await watcher.consume_event(events.get_nowait())

            await scan()
            item = await MediaItem.get(lib_id=lib.id, path=str(source))
            original_id = item.id
            fire.reset_mock()
            lib.rename_template = template
            await lib.save(update_fields=["rename_template"])
            await scan()
            await item.refresh_from_db()
            assert item.path == str(source)
            fire.assert_not_awaited()

            event = await MediaEvent.create(
                lib=lib, src_path=str(parent_nfo), event_type="modified"
            )
            await watcher.consume_event(event)
            destination = tmp_path / target_name
            await item.refresh_from_db()
            assert item.path == str(destination)
            assert destination.read_bytes() == b"video"
            # watchdog can deliver these after the journal transaction commits
            for kind, origin, target in [
                ("moved", source, destination),
                ("deleted", source, None),
                ("created", destination, None),
            ]:
                await watcher.consume_event(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=str(origin),
                        dest_path=str(target) if target else None,
                        event_type=kind,
                    )
                )
            await scan()
            await scan()
            assert (await MediaItem.get(path=str(destination))).id == original_id
            assert not await MediaEvent.filter(lib_id=lib.id).exists()
            assert all(
                Path(row.path).exists() for row in await MediaItem.filter(lib_id=lib.id)
            )
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("season_nfo", [False, True])
def test_unindexed_season(tmp_path, monkeypatch, season_nfo):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr(FlowTriggerService, "fire", AsyncMock())
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    source = tmp_path / "Old"
    nested = source / "Season 02"
    nested.mkdir(parents=True)
    (source / "Old.nfo").write_text("<tvshow><title>Series</title></tvshow>")
    (source / "S01E01.mkv").write_bytes(b"first season")
    (nested / "S02E01.mkv").write_bytes(b"second season")
    if season_nfo:
        (nested / "Season 02.nfo").write_text("<tvshow><title>Series</title></tvshow>")
    artwork = source / "art"
    artwork.mkdir()
    (artwork / "poster.jpg").write_bytes(b"poster")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/Season {{season}}/{{episode_code}}",
            )
            monitor = watcher.LibWatcher(None)
            events = Queue()
            monitor._observers = {lib.dir: (None, events)}
            monitor._scanning_paths = []

            async def scan():
                await monitor.scan_directory(lib)
                while not events.empty():
                    await watcher.consume_event(events.get_nowait())

            await scan()

            first = tmp_path / "Series/Season 01/S01E01.mkv"
            second = (
                tmp_path / "Series/Season 02/S02E01.mkv"
                if season_nfo
                else nested / "S02E01.mkv"
            )
            assert first.read_bytes() == b"first season"
            assert second.read_bytes() == b"second season"
            assert set(tmp_path.rglob("*.mkv")) == {first, second}
            assert (first.parent / "art/poster.jpg").read_bytes() == b"poster"
            items = await MediaItem.filter(lib_id=lib.id, parent_id__not_isnull=True)
            ids = {item.path: item.id for item in items}
            assert set(ids) == {str(first), str(second)}

            await scan()

            items = await MediaItem.filter(lib_id=lib.id, parent_id__not_isnull=True)
            assert {item.path: item.id for item in items} == ids
            assert not await MediaEvent.filter(lib_id=lib.id).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_persisted_event(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            source = tmp_path / "Old.mkv"
            source.write_bytes(b"video")
            item = await MediaItem.create(
                lib=lib, path=str(source), dir=lib.dir, name="Old"
            )
            assert await shelver.gen_nfo(
                "movie",
                str(source.with_suffix(".nfo")),
                {"title": "New"},
                item_id=item.id,
                refresh=True,
            )
            consume = watcher.consume_event

            async def finish(event):
                await consume(event)
                raise asyncio.CancelledError

            monkeypatch.setattr(watcher, "consume_event", finish)
            monitor = watcher.LibWatcher(None)
            await asyncio.wait_for(monitor._event_consumer(lib.id, Queue()), 3)

            await item.refresh_from_db()
            assert item.path == str(tmp_path / "New.mkv")
            assert Path(item.path).read_bytes() == b"video"
            assert not source.exists()
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_deferred_events(tmp_path, monkeypatch):
    attempts = []
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            pending = await MediaEvent.create(
                lib=lib,
                src_path=str(tmp_path / "Pending.nfo"),
                event_type="ingest",
                payload={"bootparams": [], "organize_ids": [1]},
            )
            events = Queue()
            events.put(pending)

            async def organize(current_lib, ids):
                attempts.append(ids)
                if ids == [1]:
                    if len(attempts) == 1:
                        await MediaEvent.create(
                            lib=current_lib,
                            src_path=str(tmp_path / "Ready.nfo"),
                            event_type="ingest",
                            payload={"bootparams": [], "organize_ids": [2]},
                        )
                    raise watcher.OrganizeDeferredError("pending transfer")
                return {}

            consume = watcher.consume_event

            async def finish(event):
                await consume(event)
                raise asyncio.CancelledError

            monkeypatch.setattr(watcher, "organize_items", organize)
            monkeypatch.setattr(watcher, "consume_event", finish)
            monitor = watcher.LibWatcher(None)
            await asyncio.wait_for(monitor._event_consumer(lib.id, events), 5)

            await pending.refresh_from_db()
            assert attempts == [[1], [1], [2]]
            assert pending.payload["organize_ids"] == [1]
            assert await MediaEvent.filter(lib=lib).count() == 1
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("interruption", ["organization", "workflow"])
def test_ingest_recovery(tmp_path, monkeypatch, restart, interruption):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    source = tmp_path / "Old" / "S01E01.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    (source.parent / "Old.nfo").write_text("<tvshow><title>Series</title></tvshow>")
    destination = tmp_path / "Series" / source.name
    move_files = organizer._move_files
    interrupted = False
    fired = []

    def move(root, payload):
        nonlocal interrupted
        if interruption == "organization" and not interrupted:
            interrupted = True
            raise OSError("temporary filesystem failure")
        return move_files(root, payload)

    async def fire(*_args, bootparams):
        nonlocal interrupted
        if bootparams["nfo_type"] == "episode":
            if interruption == "workflow" and not interrupted:
                interrupted = True
                raise RuntimeError("temporary workflow failure")
            body = {"title": "Pilot", "season": 1, "episode": 1}
            assert await shelver.gen_nfo(
                bootparams["nfo_type"],
                bootparams["nfo_path"],
                body,
                item_id=bootparams["item_id"],
                refresh=True,
            )
        fired.append(bootparams)

    monkeypatch.setattr(organizer, "_move_files", move)
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/{{episode_code}} - {{title}}",
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(source), event_type="created"
            )
            expected = (
                watcher.OrganizePendingError
                if interruption == "organization"
                else RuntimeError
            )
            with pytest.raises(expected):
                await watcher.consume_event(event)

            if restart:
                monitor = watcher.LibWatcher(None)
                events = await monitor._create_events(lib)
                while not events.empty():
                    await watcher.consume_event(events.get_nowait())
            else:
                await watcher.consume_event(event)

            assert destination.is_file()
            assert destination.with_suffix(".nfo").is_file()
            assert len(fired) == 2
            episode = next(
                params for params in fired if params["nfo_type"] == "episode"
            )
            assert episode["item_path"] == str(destination)
            assert episode["nfo_path"] == str(destination.with_suffix(".nfo"))
            assert episode["title"] == "Series"
            for kind, origin, target in [
                ("moved", source, destination),
                ("created", destination, None),
                ("deleted", source, None),
            ]:
                await watcher.consume_event(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=str(origin),
                        dest_path=str(target) if target else None,
                        event_type=kind,
                    )
                )
            for pending in await MediaEvent.filter(lib=lib):
                await watcher.consume_event(pending)
            organized = destination.with_name("S01E01 - Pilot.mkv")
            assert organized.read_bytes() == b"video"
            assert organized.with_suffix(".nfo").is_file()
            assert len(fired) == 2
            assert await MediaEvent.filter(lib=lib).count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("restart", [False, True])
def test_hash_recovery(tmp_path, monkeypatch, restart):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    source.with_suffix(".nfo").write_text("<movie><title>New</title></movie>")
    destination = tmp_path / "New.mkv"
    move_files = organizer._move_files
    blocked = True

    def move(root, payload):
        move_files(root, payload)
        if blocked:
            raise OSError("interrupted after moving files")

    monkeypatch.setattr(organizer, "_move_files", move)

    async def run():
        nonlocal blocked
        db_url = f"sqlite://{tmp_path / 'media.sqlite3'}"
        hash_started = asyncio.Event()
        hash_finished = asyncio.Event()
        hash_tasks = []

        async def track_hash(coroutine):
            hash_started.set()
            try:
                if restart:
                    await asyncio.Event().wait()
                await coroutine
            finally:
                coroutine.close()
                hash_finished.set()

        def start_hash(coroutine):
            task = asyncio.create_task(track_hash(coroutine))
            hash_tasks.append(task)
            return task

        monkeypatch.setattr("app.services.media.create_task", start_hash)
        await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(source), event_type="created"
            )

            with pytest.raises(watcher.OrganizePendingError):
                await watcher.consume_event(event)
            await asyncio.wait_for(hash_started.wait(), timeout=3)
            if restart:
                for task in hash_tasks:
                    task.cancel()
                await asyncio.gather(*hash_tasks, return_exceptions=True)
                await Tortoise.close_connections()
                await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
                lib = await MediaLib.get(id=lib.id)
            else:
                await asyncio.wait_for(hash_finished.wait(), timeout=3)

            item = await MediaItem.get(lib=lib)
            original_id = item.id
            assert item.hash is None and item.size is None
            assert item.path == str(source)
            assert destination.is_file() and not source.exists()
            with pytest.raises(watcher.OrganizePendingError):
                await watcher.consume_event(event)
            fire.assert_not_awaited()

            blocked = False
            monitor = watcher.LibWatcher(None)
            events = await monitor._create_events(lib)
            while not events.empty():
                await watcher.consume_event(events.get_nowait())

            await item.refresh_from_db()
            assert item.id == original_id
            assert item.path == str(destination)
            assert item.hash == hashlib.md5(b"video").hexdigest()
            assert item.size == len(b"video")
            assert not await MediaEvent.filter(lib=lib).exists()
            assert fire.await_count == 1
        finally:
            for task in hash_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*hash_tasks, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())
