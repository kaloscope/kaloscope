import asyncio
import hashlib
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from filelock import AsyncFileLock
from sanic import Sanic, SanicException

from app.core.config import KaloscopeConfig


async def write_in_thread[**P, R](
    function: Callable[P, R], *args: P.args, **kwargs: P.kwargs
) -> R:
    """Wait for a filesystem writer before releasing locks or cleaning staging files.

    Args:
        function: The synchronous filesystem operation to run in a worker thread.
        *args: The positional arguments passed to the operation.
        **kwargs: The keyword arguments passed to the operation.

    Returns:
        The result of the filesystem operation.

    Raises:
        asyncio.CancelledError: After the worker stops, even if writing fails.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # wait for the writer before releasing locks or removing its files
        while not task.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        with suppress(asyncio.CancelledError, Exception):
            task.result()
        raise


def notify_media_events(lib_id: int):
    """Notify the library owner after persisted events have committed.

    Standalone callers without an application leave recovery to watcher startup.

    Args:
        lib_id: The library whose pending events need to be reloaded.
    """
    try:
        shared = getattr(Sanic.get_app(), "shared_ctx", None)
    except SanicException:
        return
    changes = getattr(shared, "lib_event_changes", None)
    if changes is not None:
        changes[lib_id] = True


def library_lock(directory: str) -> AsyncFileLock:
    """Create a native file lock for the canonical library path.

    Share a lock path in `workspace/temp` across workers. Wait without blocking
    the event loop; process exit releases the OS lock.

    Args:
        directory: The library root, resolved to identify the shared lock.

    Returns:
        A new, unacquired asynchronous file lock for the library.
    """
    key = hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()
    return AsyncFileLock(
        Path(KaloscopeConfig.get_workspace("temp")) / f"library_{key}.lock",
        fallback_to_soft=False,
    )
