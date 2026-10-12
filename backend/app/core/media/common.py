"""Share content errors, source snapshots, limits and file stability checks."""

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# bound serialized indexes without limiting cached body size
INDEX_BYTES = 8 * 1024 * 1024


class ContentError(ValueError):
    """Carry a stable content error without coupling parsers to HTTP."""

    def __init__(self, code: str):
        """Store the error code for the caller's indexing or response handling.

        Args:
            code: The stable content failure code.
        """
        super().__init__(code)
        self.code = code


class FileSnapshot(BaseModel):
    """Keep source size and modification time independently of its path."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    size: int = Field(ge=0)
    mtime_ns: int


def original_file_info(path: str | Path) -> dict[str, Any]:
    """Capture a media source's original location and filesystem attributes.

    Read only filesystem metadata and run this operation in a worker thread.
    Keep the supplied location without resolving symlinks. For symlinks, store
    the raw link target and capture the target's size and modification time.

    Args:
        path: The source file or directory being registered for the first time.

    Returns:
        The absolute path, entry type, regular-file size in bytes, modification
        time in UTC ISO 8601 format and raw symlink target. Unavailable attributes
        remain `None` if filesystem access or timestamp conversion fails.
        Directory sizes remain `None`.
    """
    path = Path(path).absolute()
    original: dict[str, Any] = {
        "path": str(path),
        "type": None,
        "size": None,
        "mtime": None,
        "link_target": None,
    }
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            original["type"] = "symlink"
            original["link_target"] = os.readlink(path)
            info = path.stat()
        elif stat.S_ISDIR(info.st_mode):
            original["type"] = "directory"
        elif stat.S_ISREG(info.st_mode):
            original["type"] = "file"
        if stat.S_ISREG(info.st_mode):
            original["size"] = info.st_size
        original["mtime"] = datetime.fromtimestamp(info.st_mtime, tz=UTC).isoformat()
    except (OSError, ValueError, OverflowError):
        pass
    return original


def file_state(info: os.stat_result) -> tuple[int, ...]:
    """Return file identity and write attributes for stability checks.

    Args:
        info: The filesystem attributes captured during a build or read.

    Returns:
        Identity, size and write timestamps, excluding access time and path.
    """
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
