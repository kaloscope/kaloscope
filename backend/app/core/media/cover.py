"""Select and read bounded local covers without building or changing content indexes."""

import hashlib
import posixpath
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, ZipInfo

from app.core.media.archive import normalize_member_path, open_archive, read_member
from app.core.media.common import ContentError, file_state
from app.core.media.epub.package import locate_epub_package, resolve_epub_reference
from app.core.media.handlers.reading import (
    COVER_NAMES,
    IMAGE_EXTENSIONS,
    ReadingSource,
    is_ignored_name,
    list_source_entries,
    natural_key,
)
from app.core.media.image import list_image_members
from app.core.media.metadata import METADATA_BYTES
from app.core.media.metadata_reader import MetadataRead, MetadataSource
from app.core.media.raster import IMAGE_BYTES, ImageMime, image_mime, read_image_file
from app.models.media import MediaFormat


@dataclass(frozen=True)
class CoverImage:
    """Keep verified image bytes and their internal file or archive ownership.

    Paths and member names are not public URLs or client-supplied resource IDs.
    """

    data: bytes
    mime_type: ImageMime
    path: Path
    member: str | None = None


def _directory_state(path: Path) -> tuple[int, ...]:
    """Inspect a real cover container without following a directory symlink.

    Args:
        path: A container within the caller-validated library boundary.

    Returns:
        The directory identity and modification attributes.

    Raises:
        ContentError: If the path is not a real directory.
        OSError: If the directory cannot be inspected.
    """
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or path.parent.is_symlink():
        raise ContentError("media_source_unavailable")
    return file_state(info)


def _named_covers(names: list[str]) -> list[str]:
    """Order named covers using the same priority as comic content indexes.

    Args:
        names: Visible image filenames or normalized archive member paths.

    Returns:
        Cover, folder and poster candidates with natural filename tie-breaking.
    """
    return sorted(
        (name for name in names if PurePosixPath(name).stem.casefold() in COVER_NAMES),
        key=lambda name: (
            COVER_NAMES.index(PurePosixPath(name).stem.casefold()),
            natural_key(name),
        ),
    )


def _read_local_cover(directory: Path, relative_path: str) -> CoverImage | None:
    """Read one optional image while checking every directory below its container.

    Args:
        directory: The validated work or chapter container.
        relative_path: A resolved relative path within that container.

    Returns:
        A supported cover, or None for missing, invalid or oversized image bytes.

    Raises:
        ContentError: If a symlink, special file or concurrent change is detected.
        OSError: If the source cannot be accessed.
    """
    try:
        normalize_member_path(relative_path)
    except ValueError:
        return None
    parts = PurePosixPath(relative_path).parts
    if any(is_ignored_name(part) for part in parts):
        return None
    states = {directory: _directory_state(directory)}
    parent = directory
    try:
        for part in parts[:-1]:
            parent /= part
            states[parent] = _directory_state(parent)
        path = directory / relative_path
        data, _ = read_image_file(path, full=True, limit=IMAGE_BYTES)
        return CoverImage(data=data, mime_type=image_mime(data), path=path)
    except FileNotFoundError:
        return None
    except ContentError as error:
        if error.code in {"invalid_image", "media_limit_exceeded"}:
            return None
        raise
    finally:
        try:
            changed = any(
                _directory_state(path) != state for path, state in states.items()
            )
        except FileNotFoundError as error:
            raise ContentError("content_changed") from error
        if changed:
            raise ContentError("content_changed")


def _resolve_cover(base: str, href: str) -> str | None:
    """Resolve an OPF cover URL without fetching remote URLs or leaving its root.

    Args:
        base: The external OPF filename or embedded OPF member path.
        href: The unresolved reference retained by the metadata parser.

    Returns:
        A safe relative path, or None for a remote or invalid reference.
    """
    try:
        return resolve_epub_reference(base, href)
    except ContentError:
        return None


def _read_member_cover(
    source: ReadingSource, archive: ZipFile, member: ZipInfo
) -> CoverImage | None:
    """Read one optional archive image, including its complete size and CRC check.

    Args:
        source: The validated EPUB or comic archive source.
        archive: Its stable archive yielded by open_archive.
        member: The selected image member, without trusting its declared MIME.

    Returns:
        Verified image bytes, or None for unsupported or oversized image content.

    Raises:
        ContentError: If member reading fails within the archive boundary.
        OSError: If source bytes cannot be read.
    """
    try:
        data = read_member(archive, member, IMAGE_BYTES)
        return CoverImage(
            data=data,
            mime_type=image_mime(data),
            path=source.path,
            member=member.filename,
        )
    except ContentError as error:
        if error.code in {"invalid_image", "media_limit_exceeded"}:
            return None
        raise


def _check_embedded(archive: ZipFile, origin: MetadataSource) -> None:
    """Require the cover hint to belong to the current embedded metadata bytes.

    Args:
        archive: The currently opened source archive.
        origin: The freshly parsed embedded metadata supplying the cover hint.

    Raises:
        ContentError: If metadata disappeared, changed or exceeds its limit.
        OSError: If source bytes cannot be read.
    """
    try:
        member = archive.getinfo(origin.member or "")
    except KeyError as error:
        raise ContentError("content_changed") from error
    data = read_member(archive, member, METADATA_BYTES)
    if hashlib.sha256(data).hexdigest() != origin.signature:
        raise ContentError("content_changed")


def _read_epub_cover(
    source: ReadingSource, origin: MetadataSource
) -> CoverImage | None:
    """Read a declared EPUB cover independently of its spine or readable body.

    Args:
        source: The validated EPUB source.
        origin: The parsed embedded OPF with a cover reference.

    Returns:
        A supported local image, or None for an unusable or encrypted cover.

    Raises:
        ContentError: If the archive or descriptors are invalid or have changed.
        OSError: If the source cannot be read.
    """
    assert origin.parsed is not None and origin.parsed.data.cover is not None
    href = origin.parsed.data.cover.href
    if href is None:
        return None
    with open_archive(source.path) as (archive, _):
        package_path, members, encrypted = locate_epub_package(archive)
        if normalize_member_path(origin.member or "") != package_path:
            raise ContentError("content_changed")
        _check_embedded(archive, origin)
        path = _resolve_cover(package_path, href)
        member = (
            members.get(path) if path is not None and path not in encrypted else None
        )
        return _read_member_cover(source, archive, member) if member else None


def _read_comic_cover(
    source: ReadingSource, metadata: MetadataRead
) -> CoverImage | None:
    """Select a comic archive cover from markers, named images or the first body page.

    Args:
        source: The validated CBZ or ZIP source.
        metadata: Current external and embedded ComicInfo results for this source.

    Returns:
        The first usable candidate, or None when no supported cover is available.

    Raises:
        ContentError: If the archive is unsafe, corrupt, changing or over limits.
        OSError: If the source cannot be read.
    """
    with open_archive(source.path) as (archive, _):
        members = list_image_members(archive)
        for origin in (metadata.external, metadata.embedded):
            hint = origin.parsed.data.cover if origin and origin.parsed else None
            if hint is not None and hint.page is not None and hint.page < len(members):
                if origin is not None and origin.member is not None:
                    _check_embedded(archive, origin)
                if cover := _read_member_cover(source, archive, members[hint.page]):
                    return cover
        by_name = {normalize_member_path(member.filename): member for member in members}
        for name in _named_covers(list(by_name)):
            if cover := _read_member_cover(source, archive, by_name[name]):
                return cover
        first = next(
            (
                member
                for member in members
                if PurePosixPath(member.filename).stem.casefold() not in COVER_NAMES
            ),
            None,
        )
        return _read_member_cover(source, archive, first) if first else None


def read_cover(source: ReadingSource, metadata: MetadataRead) -> CoverImage | None:
    """Read the current local cover without caches, writes, HTTP or body parsing.

    The caller checks permissions, visibility, layout and all library ancestors,
    supplies fresh metadata for the same source and runs this work in a worker.
    Named external covers win, then external and embedded metadata hints, then
    named archive covers and the first comic body image. Parent covers and child
    traversal are not used. ComicInfo page numbers address all visible images in
    natural order before named covers are excluded from the body index.

    Args:
        source: A reading unit or collection within a caller-validated library.
        metadata: The current read_metadata result for that exact source.

    Returns:
        Verified image bytes and internal ownership, or None for a placeholder.

    Raises:
        ContentError: If ownership, source I/O, stability or archive checks fail.
    """
    if not source.path.is_absolute() or ".." in source.path.parts:
        raise ContentError("media_source_unavailable")
    external, embedded = metadata.external, metadata.embedded
    if (
        external is not None
        and external.parsed is not None
        and (external.path.parent != source.directory or external.member is not None)
    ):
        raise ContentError("media_source_unavailable")
    if (
        embedded is not None
        and embedded.parsed is not None
        and (
            source.format not in (MediaFormat.EPUB, MediaFormat.CBZ, MediaFormat.ZIP)
            or embedded.path != source.path
            or embedded.member is None
        )
    ):
        raise ContentError("media_source_unavailable")
    try:
        before = _directory_state(source.directory)
        try:
            files, _ = list_source_entries(source.directory)
            images = [
                path.name
                for path in files
                if path.suffix.casefold() in IMAGE_EXTENSIONS
            ]
            for name in _named_covers(images):
                if cover := _read_local_cover(source.directory, name):
                    return cover
            if source.format in (MediaFormat.TXT, MediaFormat.EPUB):
                for origin in (metadata.external, metadata.embedded):
                    hint = (
                        origin.parsed.data.cover if origin and origin.parsed else None
                    )
                    if origin is None or hint is None or hint.href is None:
                        continue
                    if origin.member is not None:
                        if cover := _read_epub_cover(source, origin):
                            return cover
                    else:
                        path = _resolve_cover(origin.path.name, hint.href)
                        if path is not None:
                            # preserve filesystem Unicode after URL validation
                            path = posixpath.normpath(unquote(urlsplit(hint.href).path))
                            if cover := _read_local_cover(source.directory, path):
                                return cover
                return None
            if source.format in (MediaFormat.CBZ, MediaFormat.ZIP):
                return _read_comic_cover(source, metadata)
            if source.format == MediaFormat.DIR:
                origin = metadata.external
                hint = origin.parsed.data.cover if origin and origin.parsed else None
                if (
                    hint is not None
                    and hint.page is not None
                    and hint.page < len(images)
                    and (
                        cover := _read_local_cover(source.directory, images[hint.page])
                    )
                ):
                    return cover
                first = next(
                    (
                        name
                        for name in images
                        if Path(name).stem.casefold() not in COVER_NAMES
                    ),
                    None,
                )
                return _read_local_cover(source.directory, first) if first else None
            return None
        finally:
            try:
                changed = _directory_state(source.directory) != before
            except FileNotFoundError as error:
                raise ContentError("content_changed") from error
            if changed:
                raise ContentError("content_changed")
    except OSError as error:
        raise ContentError("media_source_unavailable") from error
