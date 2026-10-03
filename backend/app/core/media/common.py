"""Share content errors, source snapshots, limits and file stability checks."""

import os

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


def file_state(info: os.stat_result) -> tuple[int, ...]:
    """Return file identity and write attributes for stability checks.

    Args:
        info: The filesystem attributes captured during a build or read.

    Returns:
        Identity, size and write timestamps, excluding access time and path.
    """
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
