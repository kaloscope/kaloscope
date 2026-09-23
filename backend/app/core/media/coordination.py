import hashlib
from pathlib import Path

from filelock import AsyncFileLock

from app.core.config import KaloscopeConfig


def library_lock(directory: str) -> AsyncFileLock:
    """Create a native file lock for the canonical library path.

    Workers share a library-scoped path in `workspace/temp`, like submission locks.
    Acquisition waits asynchronously so competing writers can finish and release
    their lock without blocking the event loop. Process exit releases the OS lock.

    Args:
        directory: The media library root directory used to identify the lock.

    Returns:
        An independent asynchronous native file lock for the library that must be
        acquired before use.
    """
    key = hashlib.sha256(str(Path(directory).resolve()).encode()).hexdigest()
    return AsyncFileLock(
        Path(KaloscopeConfig.get_workspace("temp")) / f"library_{key}.lock",
        fallback_to_soft=False,
    )
