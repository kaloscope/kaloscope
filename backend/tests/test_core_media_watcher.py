"""Unit tests for the media library watcher."""

import asyncio
from queue import Queue
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.media import watcher
from app.models.media import LibType, MediaLib


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
