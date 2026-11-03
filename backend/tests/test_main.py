"""Unit tests for application lifecycle handling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from app import main


@pytest.mark.parametrize("cancel_first", [False, True])
def test_shutdown_retry(monkeypatch, cancel_first):
    app = SimpleNamespace(ctx=SimpleNamespace())
    cleanup = Mock()
    steps = (
        "close_mcp",
        "shutdown_monitors",
        "save_sessions",
        "close_dl_syncer",
        "close_lib_watcher",
        "close_flow_engine",
        "close_http_client",
        "close_orm",
        "detach_log_monitor",
    )
    for name in steps:
        callback = AsyncMock()
        cleanup.attach_mock(callback, name)
        monkeypatch.setattr(main, name, callback)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def wait_for_release(_app):
            entered.set()
            await release.wait()

        cleanup.close_mcp.side_effect = wait_for_release
        first = asyncio.create_task(main.before_server_stop(app))
        await entered.wait()
        second = asyncio.create_task(main.before_server_stop(app))
        await asyncio.sleep(0)

        if cancel_first:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

        release.set()
        await second
        if not cancel_first:
            await first
        await main.before_server_stop(app)

    asyncio.run(run())

    assert cleanup.mock_calls == [
        getattr(call, name)(*(() if name == "shutdown_monitors" else (app,)))
        for name in steps
    ]
