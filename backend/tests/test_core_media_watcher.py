"""Unit tests for the media library watcher."""

import asyncio
import hashlib
import mimetypes
from datetime import UTC, datetime
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.constants import NFO_MIME_TYPE
from app.core.media import shelver, watcher
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import HistoryType, User, UserHistory, UserRole
from app.services.flow import FlowTriggerService


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


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
