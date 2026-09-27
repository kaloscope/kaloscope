import hashlib
from pathlib import Path

from filelock import AsyncFileLock

from app.core.config import KaloscopeConfig


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
