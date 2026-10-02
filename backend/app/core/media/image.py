"""Build image indexes and read bounded local content."""

import hashlib
import os
import secrets
import shutil
import stat
from pathlib import Path, PureWindowsPath
from typing import Literal, Self

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from app.core.media.handlers.reading import (
    _COVER_NAMES,
    _IMAGE_EXTENSIONS,
    _MAX_PAGES,
    ReadingSource,
    _comic_source,
    _entries,
    _ignored,
    natural_key,
)
from app.core.media.text import _INDEX_BYTES, ContentError, FileSnapshot, _file_state
from app.models.media import MediaFormat

_IMAGE_BYTES = 64 * 1024 * 1024
_ImageMime = Literal["image/jpeg", "image/png", "image/webp", "image/gif"]


class ImageResource(FileSnapshot):
    """Locate one direct image file with a snapshot and an opaque resource ID."""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    relative_path: str = Field(min_length=1, max_length=4096)
    mime_type: _ImageMime
    size: int = Field(gt=0, le=_IMAGE_BYTES)

    @field_validator("relative_path")
    @classmethod
    def check_name(cls, name: str) -> str:
        """Accept only a visible, supported filename directly inside the source.

        Args:
            name: The relative resource filename from an index.

        Returns:
            The unchanged filename.

        Raises:
            ValueError: If the name could escape the directory or is unsupported.
        """
        if (
            any(char in name for char in "/\\\0")
            or PureWindowsPath(name).drive
            or _ignored(name)
            or Path(name).suffix.casefold() not in _IMAGE_EXTENSIONS
        ):
            raise ValueError("invalid image filename")
        return name


class ImageIndex(BaseModel):
    """Keep ordered pages and an optional separate cover without image copies."""

    model_config = FileSnapshot.model_config

    schema_version: Literal[1] = 1
    format: Literal["dir"] = "dir"
    index_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: tuple[ImageResource, ...] = Field(min_length=1, max_length=_MAX_PAGES)
    cover: ImageResource | None = None

    @model_validator(mode="after")
    def check_resources(self) -> Self:
        """Require unique resources, natural page order and separate cover names.

        Returns:
            The validated index.

        Raises:
            ValueError: If identities, ordering or cover ownership are invalid.
        """
        resources = (*self.pages, *((self.cover,) if self.cover else ()))
        if (
            len({entry.id for entry in resources}) != len(resources)
            or len({entry.relative_path for entry in resources}) != len(resources)
            or any(
                Path(entry.relative_path).stem.casefold() in _COVER_NAMES
                for entry in self.pages
            )
            or (
                self.cover is not None
                and Path(self.cover.relative_path).stem.casefold() not in _COVER_NAMES
            )
            or list(self.pages)
            != sorted(self.pages, key=lambda entry: natural_key(entry.relative_path))
        ):
            raise ValueError("invalid image resource list")
        return self

    @property
    def cover_id(self) -> str:
        """Return the separate cover ID or the first body page ID.

        Returns:
            An ID readable through the same resource lookup as body pages.
        """
        return (self.cover or self.pages[0]).id


def _image_type(data: bytes) -> _ImageMime:
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


def _directory_state(path: Path) -> tuple[int, ...]:
    """Inspect a real source container without accepting a linked parent.

    Args:
        path: The source directory within a caller-validated library boundary.

    Returns:
        The directory identity and modification attributes for one operation.

    Raises:
        ContentError: If the container or its direct parent is a symbolic link.
        OSError: If the directory cannot be inspected.
    """
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or path.parent.is_symlink():
        raise ContentError("media_source_unavailable")
    return _file_state(info)


def _read_image(
    path: Path, *, snapshot: FileSnapshot | None = None, full: bool = False
) -> tuple[bytes, os.stat_result]:
    """Read a stable regular file with a fixed bound and no final symlink following.

    Args:
        path: A validated direct image path.
        snapshot: The indexed size and mtime, or None while building an index.
        full: Whether to read the full image; False reads only its signature.

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
    if before.st_size > _IMAGE_BYTES:
        raise ContentError("media_limit_exceeded")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        if _file_state(os.fstat(file.fileno())) != _file_state(before):
            raise ContentError("content_changed")
        data = file.read(_IMAGE_BYTES + 1 if full else 12)
        if len(data) > _IMAGE_BYTES:
            raise ContentError("media_limit_exceeded")
        if _file_state(os.fstat(file.fileno())) != _file_state(before):
            raise ContentError("content_changed")
    try:
        after = path.stat(follow_symlinks=False)
    except FileNotFoundError as error:
        raise ContentError("content_changed") from error
    if _file_state(after) != _file_state(before) or (
        full and len(data) != before.st_size
    ):
        raise ContentError("content_changed")
    return data, before


def build_image_index(source: ReadingSource, cache_dir: Path) -> ImageIndex:
    """Build a staging index from the current direct files of an image directory.

    The caller validates library ownership, runs this synchronous work in a worker
    and revalidates the source before publishing under the library lock. Discovery
    pages are rescanned so an outdated candidate cannot omit newly added pages.

    Args:
        source: An image-directory reading unit discovered by its library handler.
        cache_dir: A nonexistent staging directory whose parent already exists.

    Returns:
        The index also saved as index.json, without copying source images.

    Raises:
        ContentError: If the source is empty, unsupported, unstable or over limits.
        OSError: If files cannot be accessed, or the staging directory exists.
    """
    if source.format != MediaFormat.DIR:
        raise ContentError("unsupported_media_format")
    cache_dir.mkdir()
    try:
        directory_state = _directory_state(source.path)
        files, _ = _entries(source.path)
        try:
            current = _comic_source(source.path, files)
        except ValueError as error:
            raise ContentError(str(error)) from error
        if current is None:
            raise ContentError("empty_content")
        if current.format != MediaFormat.DIR:
            raise ContentError("content_changed")
        covers = sorted(
            (
                path
                for path in files
                if path.suffix.casefold() in _IMAGE_EXTENSIONS
                and path.stem.casefold() in _COVER_NAMES
            ),
            key=lambda path: (
                ("cover", "folder", "poster").index(path.stem.casefold()),
                natural_key(path.name),
            ),
        )
        snapshots = {
            path: _file_state(path.stat(follow_symlinks=False))
            for path in (*current.pages, *covers)
        }
        cover_paths = set(covers)
        pages, cover = [], None
        for path in (*current.pages, *covers):
            if path in cover_paths and cover is not None:
                continue
            try:
                data, info = _read_image(path)
                mime_type = _image_type(data)
            except ContentError as error:
                if path in cover_paths and error.code in {
                    "invalid_image",
                    "media_limit_exceeded",
                }:
                    continue
                raise
            if _file_state(info) != snapshots[path]:
                raise ContentError("content_changed")
            try:
                resource = ImageResource(
                    id=hashlib.sha256(f"image:{path.name}".encode()).hexdigest()[:32],
                    relative_path=path.name,
                    mime_type=mime_type,
                    size=info.st_size,
                    mtime_ns=info.st_mtime_ns,
                )
            except ValidationError as error:
                raise ContentError("unsupported_media_format") from error
            if path in cover_paths:
                cover = resource
            else:
                pages.append(resource)
        try:
            changed = _directory_state(source.path) != directory_state or any(
                _file_state(path.stat(follow_symlinks=False)) != snapshot
                for path, snapshot in snapshots.items()
            )
        except FileNotFoundError as error:
            raise ContentError("content_changed") from error
        if changed:
            raise ContentError("content_changed")
        index = ImageIndex(
            index_version=secrets.token_hex(32), pages=tuple(pages), cover=cover
        )
        data = index.model_dump_json().encode("utf-8")
        if len(data) > _INDEX_BYTES:
            raise ContentError("media_limit_exceeded")
        (cache_dir / "index.json").write_bytes(data)
        return index
    except BaseException:
        shutil.rmtree(cache_dir)
        raise


def load_image_index(cache_dir: Path) -> ImageIndex:
    """Load and validate a bounded image index without scanning source files.

    Args:
        cache_dir: A completed internal cache directory selected by the caller.

    Returns:
        The index with safe relative filenames and unique resource identities.

    Raises:
        ContentError: If the cache is missing, corrupt or from an unsupported schema.
    """
    try:
        with (cache_dir / "index.json").open("rb") as file:
            data = file.read(_INDEX_BYTES + 1)
        if len(data) > _INDEX_BYTES:
            raise ContentError("content_not_ready")
        return ImageIndex.model_validate_json(data)
    except (OSError, ValidationError) as error:
        raise ContentError("content_not_ready") from error


def read_image_resource(
    source_dir: Path, cache_dir: Path, resource_id: str
) -> tuple[bytes, str]:
    """Read one indexed page or cover after checking its current source snapshot.

    Args:
        source_dir: The current directory, with library access checked by the caller.
        cache_dir: The completed cache for the caller-validated content version.
        resource_id: An exact opaque page or cover ID from the index.

    Returns:
        The bounded image bytes and their verified MIME type.

    Raises:
        ContentError: If the ID is unknown, the cache is invalid or the file changed
            or cannot be read safely within the resource limit.
    """
    index = load_image_index(cache_dir)
    resources = (*index.pages, *((index.cover,) if index.cover else ()))
    resource = next((entry for entry in resources if entry.id == resource_id), None)
    if resource is None:
        raise ContentError("not_found")
    try:
        before = _directory_state(source_dir)
        data, _ = _read_image(
            source_dir / resource.relative_path, snapshot=resource, full=True
        )
        if _directory_state(source_dir) != before:
            raise ContentError("content_changed")
        if _image_type(data) != resource.mime_type:
            raise ContentError("content_changed")
        return data, resource.mime_type
    except FileNotFoundError as error:
        raise ContentError("content_changed") from error
    except OSError as error:
        raise ContentError("media_source_unavailable") from error
