"""Recognize supported raster bytes without decoding pixels."""

from typing import Literal

from app.core.media.common import ContentError

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
