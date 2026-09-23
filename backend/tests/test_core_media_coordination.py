"""Native library locks share the configured workspace across workers."""

import asyncio
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

from app.core.config import KaloscopeConfig
from app.core.media.coordination import library_lock


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    config_path = Path(__file__).resolve().parents[1] / "app/config.toml"
    monkeypatch.setattr(
        KaloscopeConfig, "_config", KaloscopeConfig(config_path, tmp_path)
    )


def test_library_process_exit(tmp_path):
    directory = tmp_path / "media"
    program = """
import asyncio
import os
import sys
from pathlib import Path
from filelock import Timeout
from app.core.config import KaloscopeConfig
from app.core.media.coordination import library_lock

KaloscopeConfig._config = KaloscopeConfig(Path('app/config.toml'), Path(sys.argv[1]))

async def run():
    try:
        async with await library_lock(sys.argv[2]).acquire(timeout=0):
            os._exit(73)
    except Timeout:
        os._exit(74)

asyncio.run(run())
"""
    command = [sys.executable, "-c", program, str(tmp_path), f"{directory}/."]

    async def probe():
        result = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            timeout=10,
        )
        return result.returncode

    async def run():
        lock = library_lock(str(directory))
        key = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
        assert Path(lock.lock_file) == (
            tmp_path / "workspace/temp" / f"library_{key}.lock"
        )
        assert lock.fallback_to_soft is False
        async with lock:
            # synchronous and asynchronous locks use the same native mechanism
            with (
                pytest.raises(Timeout),
                FileLock(lock.lock_file, blocking=False, fallback_to_soft=False),
            ):
                pass
            async with library_lock(str(tmp_path / "another-library")):
                assert await probe() == 74
        assert await probe() == 73
        # abrupt process exit leaves no soft lock that could block future writers
        async with await library_lock(str(directory)).acquire(timeout=1):
            pass

    asyncio.run(run())


def test_library_wait(tmp_path):
    async def run():
        directory = str(tmp_path / "media")
        waiting = asyncio.Event()

        async def acquire():
            waiting.set()
            async with library_lock(directory):
                return True

        task = None
        try:
            async with library_lock(directory):
                task = asyncio.create_task(acquire())
                await asyncio.wait_for(waiting.wait(), timeout=1)
                # the timer can run while the other coroutine waits for the lock
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.1)
            assert await asyncio.wait_for(task, timeout=1)
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
