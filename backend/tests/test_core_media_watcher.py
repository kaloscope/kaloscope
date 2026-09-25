"""Unit tests for the media library watcher."""

import asyncio
import mimetypes
from datetime import UTC, datetime
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.constants import NFO_MIME_TYPE
from app.core.media import watcher
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import HistoryType, User, UserHistory, UserRole
from app.services.media import MediaItemService


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
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
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
