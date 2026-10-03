"""Read bounded raster files and recognize image signatures without decoding pixels."""

import os
import stat
from pathlib import Path
from typing import Literal

from app.core.media.common import ContentError, FileSnapshot, file_state

IMAGE_BYTES = 64 * 1024 * 1024
type ImageMime = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]


def image_mime(data: bytes) -> ImageMime:
    """Recognize supported raster signatures without decoding pixels.

    Args:
        data: At least the available leading 12 bytes of an image.

    Returns:
        The MIME type identified from bytes rather than the filename suffix.

    Raises:
        ContentError: If no supported image signature is present.
    """
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ContentError("invalid_image")


def read_image_file(
    path: Path,
    *,
    snapshot: FileSnapshot | None = None,
    full: bool = False,
    limit: int = IMAGE_BYTES,
) -> tuple[bytes, os.stat_result]:
    """Read a stable regular file with a fixed bound and no final symlink following.

    Args:
        path: An image path after the caller validates its directory boundary.
        snapshot: The indexed size and mtime, or None while building an index.
        full: Whether to read the full image; False reads only its signature.
        limit: The byte limit, defaulting to the shared image budget.

    Returns:
        The bounded bytes and the source attributes before reading.

    Raises:
        ContentError: If the file is unsafe, changes or exceeds the image limit.
        OSError: If the source cannot be accessed.
    """
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ContentError("media_source_unavailable")
    if snapshot and (before.st_size, before.st_mtime_ns) != (
        snapshot.size,
        snapshot.mtime_ns,
    ):
        raise ContentError("content_changed")
    if before.st_size > limit:
        raise ContentError("media_limit_exceeded")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        if file_state(os.fstat(file.fileno())) != file_state(before):
            raise ContentError("content_changed")
        data = file.read(limit + 1 if full else 12)
        if len(data) > limit:
            raise ContentError("media_limit_exceeded")
        if file_state(os.fstat(file.fileno())) != file_state(before):
            raise ContentError("content_changed")
    try:
        after = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise ContentError("content_changed") from error
    if file_state(after) != file_state(before) or (
        full and len(data) != before.st_size
    ):
        raise ContentError("content_changed")
    return data, before
