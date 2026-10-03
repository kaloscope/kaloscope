"""Build image indexes and read bounded local content."""

import hashlib
import secrets
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import Literal, Self
from zipfile import ZipFile, ZipInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.media.archive import normalize_member_path, open_archive, read_member
from app.core.media.common import INDEX_BYTES, ContentError, FileSnapshot, file_state
from app.core.media.handlers.reading import (
    COVER_NAMES,
    IMAGE_EXTENSIONS,
    MAX_PAGES,
    ReadingSource,
    identify_comic_source,
    is_ignored_name,
    list_source_entries,
    natural_key,
)
from app.core.media.raster import (
    IMAGE_BYTES,
    ImageMime,
    image_mime,
    read_image_file,
)
from app.models.media import MediaFormat

_IMAGE_BYTES = IMAGE_BYTES


class ImageResource(BaseModel):
    """Locate one image file or archive member with an opaque resource ID."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    relative_path: str = Field(min_length=1, max_length=4096)
    mime_type: ImageMime
    size: int = Field(gt=0, le=_IMAGE_BYTES)
    mtime_ns: int | None = None
    crc: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)

    @field_validator("relative_path")
    @classmethod
    def check_path(cls, path: str) -> str:
        """Accept only a visible, supported relative image path inside the source.

        Args:
            path: The relative resource filename or member path from an index.

        Returns:
            The unchanged path for exact filesystem or member lookup.

        Raises:
            ValueError: If the path could escape the source or is unsupported.
        """
        normalized = PurePosixPath(normalize_member_path(path))
        if any(is_ignored_name(part) for part in normalized.parts) or (
            normalized.suffix.casefold() not in IMAGE_EXTENSIONS
        ):
            raise ValueError("invalid image filename")
        return path


class ImageIndex(BaseModel):
    """Keep ordered pages and an optional separate cover without image copies."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    format: Literal["dir", "cbz", "zip"] = "dir"
    index_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot: FileSnapshot | None = None
    pages: tuple[ImageResource, ...] = Field(min_length=1, max_length=MAX_PAGES)
    cover: ImageResource | None = None

    @model_validator(mode="after")
    def check_resources(self) -> Self:
        """Require unique resources, natural page order and separate cover names.

        Returns:
            The validated index.

        Raises:
            ValueError: If snapshots, identities, ordering or cover ownership
                are invalid.
        """
        resources = (*self.pages, *((self.cover,) if self.cover else ()))
        archived = self.format != "dir"
        if archived != (self.source_snapshot is not None):
            raise ValueError("invalid image source snapshot")
        for entry in resources:
            if archived:
                valid = entry.crc is not None and entry.mtime_ns is None
            else:
                valid = (
                    entry.crc is None
                    and entry.mtime_ns is not None
                    and "/" not in entry.relative_path
                )
            if not valid:
                raise ValueError("invalid image resource snapshot")
        names = [
            normalize_member_path(entry.relative_path)
            if archived
            else entry.relative_path
            for entry in resources
        ]
        if (
            len({entry.id for entry in resources}) != len(resources)
            or len(set(names)) != len(resources)
            or any(
                Path(entry.relative_path).stem.casefold() in COVER_NAMES
                for entry in self.pages
            )
            or (
                self.cover is not None
                and Path(self.cover.relative_path).stem.casefold() not in COVER_NAMES
            )
            or names[: len(self.pages)]
            != sorted(names[: len(self.pages)], key=natural_key)
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


def _directory_state(path: Path) -> tuple[int, ...]:
    """Inspect a real source container without accepting a linked parent.

    Args:
        path: The source directory within a caller-validated library boundary.

    Returns:
        The directory identity and modification attributes for one operation.

    Raises:
        ContentError: If the container is not a directory or its parent is a symlink.
        OSError: If the directory cannot be inspected.
    """
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or path.parent.is_symlink():
        raise ContentError("media_source_unavailable")
    return file_state(info)


def _build_directory_index(source: ReadingSource) -> ImageIndex:
    """Build an image directory index while checking source stability.

    Args:
        source: The directory reading unit whose current direct files are indexed.

    Returns:
        The complete index without writing cache files or copying images.

    Raises:
        ContentError: If the directory is empty, ambiguous, unstable or over limits.
        OSError: If the source cannot be inspected or read.
    """
    directory_state = _directory_state(source.path)
    files, _ = list_source_entries(source.path)
    try:
        current = identify_comic_source(source.path, files)
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
            if path.suffix.casefold() in IMAGE_EXTENSIONS
            and path.stem.casefold() in COVER_NAMES
        ),
        key=lambda path: (
            COVER_NAMES.index(path.stem.casefold()),
            natural_key(path.name),
        ),
    )
    snapshots = {
        path: file_state(path.stat(follow_symlinks=False))
        for path in (*current.pages, *covers)
    }
    cover_paths = set(covers)
    pages, cover = [], None
    for path in (*current.pages, *covers):
        if path in cover_paths and cover is not None:
            continue
        try:
            data, info = read_image_file(path, limit=_IMAGE_BYTES)
            mime_type = image_mime(data)
        except ContentError as error:
            if path in cover_paths and error.code in {
                "invalid_image",
                "media_limit_exceeded",
            }:
                continue
            raise
        if file_state(info) != snapshots[path]:
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
            file_state(path.stat(follow_symlinks=False)) != snapshot
            for path, snapshot in snapshots.items()
        )
    except FileNotFoundError as error:
        raise ContentError("content_changed") from error
    if changed:
        raise ContentError("content_changed")
    index = ImageIndex(
        index_version=secrets.token_hex(32), pages=tuple(pages), cover=cover
    )
    return index


def list_image_members(archive: ZipFile) -> list[ZipInfo]:
    """List visible comic images in natural order, including named covers.

    Args:
        archive: A stable comic archive already validated by open_archive.

    Returns:
        Image members before named covers are excluded from the body page list.
    """
    return sorted(
        (
            info
            for info in archive.infolist()
            if not info.is_dir()
            and not any(
                is_ignored_name(part) for part in PurePosixPath(info.filename).parts
            )
            and PurePosixPath(info.filename).suffix.casefold() in IMAGE_EXTENSIONS
        ),
        key=lambda info: natural_key(normalize_member_path(info.filename)),
    )


def _build_archive_index(source: ReadingSource) -> ImageIndex:
    """Build a CBZ or ZIP page index without extracting members to disk.

    Args:
        source: The independently contained CBZ or ZIP reading unit.

    Returns:
        Ordered member resources and an archive snapshot, with a fallback cover.

    Raises:
        ContentError: If the source is unsafe, empty, corrupt, unstable or over limits.
        OSError: If the source cannot be inspected or read.
    """
    with open_archive(source.path) as (archive, snapshot):
        members = list_image_members(archive)
        body = [
            info
            for info in members
            if PurePosixPath(info.filename).stem.casefold() not in COVER_NAMES
        ]
        if not body:
            raise ContentError("empty_content")
        if len(body) > MAX_PAGES:
            raise ContentError("media_limit_exceeded")
        covers = sorted(
            (
                info
                for info in members
                if PurePosixPath(info.filename).stem.casefold() in COVER_NAMES
            ),
            key=lambda info: (
                COVER_NAMES.index(PurePosixPath(info.filename).stem.casefold()),
                natural_key(normalize_member_path(info.filename)),
            ),
        )
        pages, cover = [], None
        for info in (*body, *covers):
            is_cover = PurePosixPath(info.filename).stem.casefold() in COVER_NAMES
            if is_cover and cover is not None:
                continue
            try:
                data = read_member(archive, info, _IMAGE_BYTES, prefix_bytes=12)
                mime_type = image_mime(data)
            except ContentError as error:
                if is_cover and error.code in {
                    "invalid_image",
                    "media_limit_exceeded",
                }:
                    continue
                raise
            resource = ImageResource(
                id=hashlib.sha256(
                    f"image:{normalize_member_path(info.filename)}".encode()
                ).hexdigest()[:32],
                relative_path=info.filename,
                mime_type=mime_type,
                size=info.file_size,
                crc=info.CRC,
            )
            if is_cover:
                cover = resource
            else:
                pages.append(resource)
        return ImageIndex(
            format="cbz" if source.format == MediaFormat.CBZ else "zip",
            index_version=secrets.token_hex(32),
            source_snapshot=snapshot,
            pages=tuple(pages),
            cover=cover,
        )


def build_image_index(source: ReadingSource, cache_dir: Path) -> ImageIndex:
    """Build a staging index from an image directory, CBZ or ZIP archive.

    The caller validates library ownership, runs this synchronous work in a worker
    and revalidates the source before publishing under the library lock.

    Args:
        source: The directory or archive reading unit discovered by its handler.
        cache_dir: A nonexistent staging directory whose parent already exists.

    Returns:
        The index also saved as index.json, without copying source images.

    Raises:
        ContentError: If the source is unsafe, empty, unsupported, corrupt,
            unstable or over limits.
        OSError: If files cannot be accessed, or the staging directory exists.
    """
    if source.format not in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP):
        raise ContentError("unsupported_media_format")
    cache_dir.mkdir()
    try:
        index = (
            _build_directory_index(source)
            if source.format == MediaFormat.DIR
            else _build_archive_index(source)
        )
        data = index.model_dump_json().encode("utf-8")
        if len(data) > INDEX_BYTES:
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
            data = file.read(INDEX_BYTES + 1)
        if len(data) > INDEX_BYTES:
            raise ContentError("content_not_ready")
        return ImageIndex.model_validate_json(data)
    except (OSError, ValidationError) as error:
        raise ContentError("content_not_ready") from error


def read_image_resource(
    source_path: Path, cache_dir: Path, resource_id: str
) -> tuple[bytes, str]:
    """Read one indexed page or cover after checking its current source snapshot.

    Args:
        source_path: The current directory or archive after the caller checks access.
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
        if index.format == "dir":
            before = _directory_state(source_path)
            assert resource.mtime_ns is not None
            data, _ = read_image_file(
                source_path / resource.relative_path,
                snapshot=FileSnapshot(size=resource.size, mtime_ns=resource.mtime_ns),
                full=True,
                limit=_IMAGE_BYTES,
            )
            if _directory_state(source_path) != before:
                raise ContentError("content_changed")
        else:
            with open_archive(source_path, index.source_snapshot) as (archive, _):
                try:
                    member = archive.getinfo(resource.relative_path)
                except KeyError as error:
                    raise ContentError("content_changed") from error
                if (member.file_size, member.CRC) != (resource.size, resource.crc):
                    raise ContentError("content_changed")
                data = read_member(archive, member, _IMAGE_BYTES)
        if image_mime(data) != resource.mime_type:
            raise ContentError("content_changed")
        return data, resource.mime_type
    except FileNotFoundError as error:
        raise ContentError("content_changed") from error
    except OSError as error:
        raise ContentError("media_source_unavailable") from error
