from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import stat
from asyncio import create_task, to_thread
from collections.abc import AsyncGenerator, Generator
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import aiofiles
from pydantic import ValidationError
from sanic import Sanic
from sanic.log import logger
from tortoise.exceptions import DoesNotExist
from tortoise.expressions import Q
from tortoise.transactions import atomic, in_transaction

from app.core.config import KaloscopeConfig
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.media.common import ContentError, file_state
from app.core.media.coordination import (
    library_lock,
    notify_media_events,
    write_in_thread,
)
from app.core.media.metadata import ReadingMetadata, render_metadata
from app.core.media.naming import validate_template
from app.models.flow import FlowTrigger, GraphCategory
from app.models.media import (
    ContentChapter,
    EpubContent,
    ImageContent,
    IndexState,
    LibType,
    MediaContentQuery,
    MediaEvent,
    MediaFormat,
    MediaItem,
    MediaLib,
    MediaLibUpsert,
    MediaMetadata,
    NFOType,
    ReadingMetadataSync,
    TextContent,
)
from app.models.user import (
    HistoryType,
    ImageLocator,
    PermType,
    TextLocator,
    UserHistory,
    UserInfo,
    UserPermission,
    UserRole,
)
from app.services.base import BaseService
from app.services.flow import FlowTriggerService
from app.utils.disk import delete_path, rename_exclusive

if TYPE_CHECKING:
    from app.core.media.cover import CoverImage
    from app.core.media.epub.cache import EpubIndex
    from app.core.media.handlers.base import MediaPathInfo
    from app.core.media.handlers.reading import ReadingSource
    from app.core.media.image import ImageIndex
    from app.core.media.reader import MetadataRead
    from app.core.media.text import TextIndex
    from app.core.media.writer import MetadataWrite

type _SourceStates = dict[Path, tuple[int, ...]]


def _reading_role(item: MediaItem) -> Literal["book", "collection", "chapter"]:
    """Classify a reading item independently of its current chapter count.

    Args:
        item: The reading item whose source ownership has been validated.

    Returns:
        The workflow role shared by automatic and manual scraping.
    """
    if item.parent_id is not None:
        return "chapter"
    return "collection" if item.format is None else "book"


def _source_identity(item: MediaItem) -> tuple:
    """Identify the database attributes that bind an item to its media source.

    Args:
        item: The media item with its library and optional parent loaded.

    Returns:
        The source and parent attributes that must remain current during file I/O.
    """
    parent = item.parent if item.parent_id is not None else None
    return (
        item.lib_id,
        item.lib.dir,
        item.lib.lib_type,
        item.path,
        item.dir,
        item.format,
        item.parent_id,
        parent.lib_id if parent else None,
        parent.path if parent else None,
        parent.format if parent else None,
        parent.parent_id if parent else None,
    )


def _resolve_source(item: MediaItem) -> ReadingSource:
    """Resolve and validate a reading source without requiring files to exist.

    Args:
        item: The reading item with its library and optional parent loaded.

    Returns:
        The source path, format and collection described by this item.

    Raises:
        ContentError: If the library, path, format or parent ownership is invalid.
    """
    from app.core.media.handlers.reading import ReadingSource, is_ignored_name

    if item.lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
        raise ContentError("unsupported_media_format")
    root, path = Path(item.lib.dir), Path(item.path)
    parent = item.parent if item.parent_id is not None else None
    if (
        not root.is_absolute()
        or not path.is_absolute()
        or ".." in root.parts
        or ".." in path.parts
        or path == root
        or not path.is_relative_to(root)
    ):
        raise ContentError("media_source_unavailable")
    source = ReadingSource(path, item.format, Path(parent.path) if parent else None)
    parts = source.directory.relative_to(root).parts
    if (
        not parts
        or any(is_ignored_name(part) for part in path.relative_to(root).parts)
        or item.dir != str(source.directory)
        or len(parts) != (2 if parent else 1)
        or (
            parent is not None
            and (
                item.lib.lib_type != LibType.COMIC
                or parent.lib_id != item.lib_id
                or parent.format is not None
                or parent.parent_id is not None
                or Path(parent.path) != source.directory.parent
            )
        )
        or (
            item.lib.lib_type == LibType.NOVEL
            and item.format not in (MediaFormat.TXT, MediaFormat.EPUB)
        )
        or (
            item.lib.lib_type == LibType.COMIC
            and item.format
            not in (None, MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
        )
        or (parent is not None and item.format is None)
    ):
        raise ContentError("unsupported_layout")
    return source


@contextmanager
def _reading_source(
    item: MediaItem, *, require_candidate: bool = False
) -> Generator[tuple[ReadingSource, _SourceStates, bool]]:
    """Validate reading ownership and guard filesystem stability for one operation.

    Args:
        item: The owned reading item with its library and optional parent loaded.
        require_candidate: Whether discovery must find the source; defaults to False
            so already indexed empty sources can retain their identity and metadata.

    Yields:
        The current source, mutable stability checks and whether its body is missing.

    Raises:
        ContentError: If paths, layout, file access or source stability are invalid.
    """
    # handler registration imports this service through the video handlers
    from app.core.media.handlers.base import get_handler
    from app.core.media.handlers.reading import ReadingMediaHandler

    source = _resolve_source(item)
    root = Path(item.lib.dir)
    parent = item.parent if item.parent_id is not None else None
    parts = source.directory.relative_to(root).parts
    states = {}
    source_missing = False
    try:
        # check all ancestors, but only snapshot directories inside this library
        for directory in (*reversed(source.directory.parents), source.directory):
            info = directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise ContentError("media_source_unavailable")
            if directory.is_relative_to(root):
                states[directory] = file_state(info)
        if source.path != source.directory:
            try:
                info = source.path.stat(follow_symlinks=False)
            except FileNotFoundError:
                source_missing = True
            else:
                if not stat.S_ISREG(info.st_mode):
                    raise ContentError("media_source_unavailable")
                states[source.path] = file_state(info)
        try:
            handler = get_handler(item.lib.lib_type)
            if not isinstance(handler, ReadingMediaHandler):
                raise ContentError("unsupported_media_format")
            scan = handler.scan_sources(str(root), work_path=root / parts[0])
            for scope, error in scan.issues.items():
                if source.directory.is_relative_to(scope):
                    raise ContentError(error)
            if parent is not None and any(
                entry.directory == source.directory.parent and entry.format is not None
                for entry in scan.sources
            ):
                raise ContentError("unsupported_layout")
            current = next(
                (
                    entry
                    for entry in scan.sources
                    if entry.directory == source.directory
                ),
                None,
            )
            if current is not None:
                if (current.path, current.format, current.parent_path) != (
                    source.path,
                    source.format,
                    source.parent_path,
                ):
                    raise ContentError("content_changed")
            elif require_candidate:
                raise ContentError(
                    "media_source_unavailable"
                    if source_missing
                    else "empty_content"
                    if source.format in (None, MediaFormat.DIR)
                    else "unsupported_layout"
                )
            elif source.format not in (None, MediaFormat.DIR) and not source_missing:
                raise ContentError("unsupported_layout")
            # indexed empty image directories and collections can retain their metadata
            yield source, states, source_missing
        finally:
            try:
                changed = any(
                    file_state(location.stat(follow_symlinks=False)) != before
                    for location, before in states.items()
                )
            except FileNotFoundError as error:
                raise ContentError("content_changed") from error
            if changed:
                raise ContentError("content_changed")
            if source_missing:
                try:
                    source.path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise ContentError("content_changed")
    except OSError as error:
        raise ContentError("media_source_unavailable") from error


def _validate_previous_path(
    root: Path, src_path: Path, dest_path: Path, states: _SourceStates
):
    """Check that an observed move no longer resolves through its previous path.

    Args:
        root: The validated absolute library directory.
        src_path: The previous file or directory path within this library.
        dest_path: The validated destination with its state and ancestors captured.
        states: Source guards retained until the enclosing validation finishes.

    Raises:
        ContentError: If the previous path is reused or an ancestor is not a directory.
        OSError: If source inspection fails; the enclosing source guard translates it.
    """
    for directory in (*reversed(src_path.parent.parents), src_path.parent):
        if not directory.is_relative_to(root):
            continue
        try:
            info = directory.stat(follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise ContentError("media_source_unavailable")
        # retain earlier observations when both paths share an ancestor
        states.setdefault(directory, file_state(info))
    try:
        info = src_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    # case-only renames can still resolve through the old spelling
    if (
        str(src_path).casefold() != str(dest_path).casefold()
        or file_state(info) != states[dest_path]
        or states[src_path.parent][:2] != states[dest_path.parent][:2]
    ):
        raise ContentError("content_changed")


def _missing_reading_sources(
    items: list[MediaItem], work_paths: set[Path], *, directories_only: bool = False
) -> _SourceStates | None:
    """Confirm absent sources while retaining the identities of surviving ancestors.

    Args:
        items: The selected items with libraries and parents loaded.
        work_paths: Registered top-level work directories in the same library.
        directories_only: Require absent containers as well as bodies. The default
            False also allows missing files in surviving containers.

    Returns:
        Ancestor snapshots when all required paths are absent, or None if a source
        or required container exists. Empty directories and replacements are retained.

    Raises:
        ContentError: If ownership is invalid, an ancestor changes, a link or invalid
            file type is encountered, the library cannot be listed, or every
            registered work directory has disappeared.
    """
    from app.core.media.handlers.reading import list_source_entries

    states: _SourceStates = {}
    try:
        for item in items:
            source = _resolve_source(item)
            root = Path(item.lib.dir)
            for path in (*reversed(source.path.parents), source.path):
                try:
                    info = path.stat(follow_symlinks=False)
                except FileNotFoundError:
                    if path == root or not path.is_relative_to(root):
                        raise ContentError("media_source_unavailable") from None
                    break
                directory = path != source.path or source.format in (
                    None,
                    MediaFormat.DIR,
                )
                if not (
                    stat.S_ISDIR(info.st_mode)
                    if directory
                    else stat.S_ISREG(info.st_mode)
                ):
                    raise ContentError("media_source_unavailable")
                if path == source.path or (
                    directories_only and path == source.directory
                ):
                    return None
                if path.is_relative_to(root):
                    current = file_state(info)
                    if path in states and states[path] != current:
                        raise ContentError("content_changed")
                    states[path] = current
        root = Path(items[0].lib.dir)
        _, directories = list_source_entries(root)
        # an accessible but empty mount point is not evidence of library deletion
        if not work_paths.intersection(directories):
            raise ContentError("media_source_unavailable")
        if file_state(root.stat(follow_symlinks=False)) != states[root]:
            raise ContentError("content_changed")
    except OSError as error:
        raise ContentError("media_source_unavailable") from error
    return states


def _remove_reading_caches(ids: list[int]):
    """Remove rebuildable caches before deleting their database owners.

    Args:
        ids: The validated reading item IDs whose sources are absent.

    Raises:
        ContentError: If a cache cannot be removed or its directory is a link.
    """
    directory = Path(KaloscopeConfig.get_workspace("temp")) / "media_index"
    try:
        for parent in (directory.parent, directory):
            try:
                info = parent.stat(follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(info.st_mode):
                raise ContentError("content_not_ready")
        for id in ids:
            with suppress(FileNotFoundError):
                shutil.rmtree(directory / str(id))
    except OSError as error:
        raise ContentError("content_not_ready") from error


async def _remove_media_records(items: list[MediaItem]):
    """Remove selected media items and their scoped history inside a transaction.

    Args:
        items: The nonempty, validated same-library deletion scope with parents loaded.
    """
    ids = {item.id for item in items}
    parents = {item.id: item.parent_id for item in items if item.parent_id is not None}
    history_type = {
        LibType.NOVEL: HistoryType.TEXT,
        LibType.COMIC: HistoryType.IMAGE,
    }.get(items[0].lib.lib_type, HistoryType.VIDEO)
    histories = await UserHistory.filter(
        rel_type=history_type, rel_id__in=ids | set(parents.values())
    ).only("id", "rel_id", "locator")
    history_ids = []
    for history in histories:
        chapter_id = (history.locator or {}).get("chapter_item_id")
        if history.rel_id in ids or (
            history_type == HistoryType.IMAGE
            and type(chapter_id) is int
            and parents.get(chapter_id) == history.rel_id
        ):
            history_ids.append(history.id)
    await UserHistory.filter(id__in=history_ids).delete()
    await MediaItem.filter(id__in=ids).delete()


def _media_files(items: list[MediaItem], others: list[MediaItem]):
    """Collect owned files using the library's reading or video rules.

    Args:
        items: The nonempty deletion scope with library and parent relations loaded.
        others: Other library items used to protect shared video companions.

    Returns:
        Directory, body and companion snapshots.

    Raises:
        ContentError: If source ownership or library access cannot be confirmed.
        OSError: If files cannot be inspected.
    """
    from app.core.media.cleanup import reading_files, video_files

    root = Path(items[0].lib.dir)
    if items[0].lib.lib_type in (LibType.NOVEL, LibType.COMIC):
        return reading_files(root, [_resolve_source(item) for item in items])
    return video_files(root, items, others)


def _check_media_deleted(
    items: list[MediaItem], states: _SourceStates, others: list[MediaItem]
):
    """Confirm selected files remain absent and surviving directories are unchanged.

    Args:
        items: The same-library scope whose files were removed.
        states: Directory snapshots after deletion, including cover folders.
        others: Other library items used to protect shared video companions.

    Raises:
        ContentError: If a body, companion or directory changes or cannot be inspected.
    """
    try:
        _, bodies, companions = _media_files(items, others)
        if (
            bodies
            or companions
            or any(
                file_state(path.stat(follow_symlinks=False)) != state
                for path, state in states.items()
            )
        ):
            raise ContentError("content_changed")
    except OSError as error:
        raise ContentError("media_source_unavailable") from error


def _delete_media_files(
    items: list[MediaItem], others: list[MediaItem]
) -> tuple[_SourceStates, list[Path]]:
    """Delete selected bodies before companions without deleting whole directories.

    Args:
        items: The nonempty scope with validated library and parent ownership.
        others: Other library items used to protect shared video companions.

    Returns:
        Surviving directory snapshots and removed files for database and folder cleanup.

    Raises:
        ContentError: If files change or cannot be removed. Earlier completed writes
            are retained so retry can finish the remaining scope.
    """
    removed = []
    try:
        states, bodies, companions = _media_files(items, others)
        for group in (bodies, companions):
            if _media_files(items, others) != (states, bodies, companions):
                raise ContentError("content_changed")
            for path, expected in list(group.items()):
                if (
                    any(
                        file_state(parent.stat(follow_symlinks=False)) != states[parent]
                        for parent in path.parents
                        if parent in states
                    )
                    or file_state(path.stat(follow_symlinks=False)) != expected
                ):
                    raise ContentError("content_changed")
                delete_path(path)
                removed.append(path)
                del group[path]
                states[path.parent] = file_state(
                    path.parent.stat(follow_symlinks=False)
                )
        _check_media_deleted(items, states, others)
        return states, removed
    except OSError as error:
        raise ContentError("media_source_unavailable") from error


def _remove_reading_companions(
    item: MediaItem, work_paths: set[Path], states: _SourceStates
) -> tuple[_SourceStates, list[Path]]:
    """Remove exclusive companions while the registered body remains absent.

    Args:
        item: The missing body with validated database ownership.
        work_paths: Registered work directories used to verify library availability.
        states: Ancestor snapshots captured before cleanup.

    Returns:
        Updated ancestor snapshots and removed file paths for empty directory cleanup.

    Raises:
        ContentError: If ownership, files or library availability change, or a file
            cannot be removed. Already removed files remain safe to skip on retry.
    """
    from app.core.media.cleanup import reading_companions

    source = _resolve_source(item)
    removed: list[Path] = []
    identities = {path: value[:2] for path, value in states.items()}
    try:
        remaining = reading_companions(source)
        for path, state in list(remaining.items()):
            current = _missing_reading_sources([item], work_paths)
            if (
                current is None
                or {path: value[:2] for path, value in current.items()} != identities
            ):
                raise ContentError("content_changed")
            if reading_companions(source) != remaining:
                raise ContentError("content_changed")
            if file_state(path.stat(follow_symlinks=False)) != state:
                raise ContentError("content_changed")
            delete_path(path)
            removed.append(path)
            del remaining[path]
        current = _missing_reading_sources([item], work_paths)
        if (
            current is None
            or {path: value[:2] for path, value in current.items()} != identities
        ):
            raise ContentError("content_changed")
        if reading_companions(source):
            raise ContentError("content_changed")
        return current, removed
    except OSError as error:
        raise ContentError("media_source_unavailable") from error


def _prune_media_directories(
    directory: Path, removed: list[Path], states: _SourceStates
):
    """Best-effort removal of empty companion folders and their owned container.

    Args:
        directory: The former body container strictly below the library root.
        removed: File paths whose parent directories may now be empty.
        states: Surviving ancestor identities captured before database cleanup.
    """
    parents = {directory}
    for path in removed:
        parents.update(
            parent for parent in path.parents if parent.is_relative_to(directory)
        )
    for path in sorted(parents, key=lambda path: len(path.parts), reverse=True):
        try:
            if any(
                file_state(parent.stat(follow_symlinks=False))[:2] != state[:2]
                for parent, state in states.items()
            ):
                return
            if not all(
                stat.S_ISDIR(parent.stat(follow_symlinks=False).st_mode)
                for parent in (*reversed(path.parents), path)
            ):
                return
            path.rmdir()
        except OSError:
            # nonempty or unavailable directories can remain without their old item
            continue


def _validate_reading_source(
    item: MediaItem, *, require_candidate: bool, previous_paths: tuple[Path, ...] = ()
):
    """Check source ownership and discovery in a worker without reading its body.

    Args:
        item: The proposed reading item with its library and optional parent loaded.
        require_candidate: Whether discovery must still find this new source.
        previous_paths: Validated former body paths from observed moves. The default
            empty tuple skips checking that previous paths have disappeared.

    Raises:
        ContentError: If source ownership, discovery or stability is invalid.
    """
    with _reading_source(item, require_candidate=require_candidate) as (
        source,
        states,
        _,
    ):
        for previous_path in previous_paths:
            _validate_previous_path(
                Path(item.lib.dir), previous_path, source.path, states
            )


def _validate_reading_directory_move(
    items: list[MediaItem], previous_paths: tuple[Path, ...], dest_path: Path
):
    """Validate a moved container and retain source guards until checks finish.

    Args:
        items: The nonempty list of proposed items with library and parents loaded.
        previous_paths: Previous work or chapter directories from persisted moves.
        dest_path: The new directory containing the same registered sources.

    Raises:
        ContentError: If sources are invalid, unstable or a former directory is reused.
    """
    with _reading_source(items[0]) as (_, states, _), ExitStack() as stack:
        # ponytail: per-item discovery is quadratic; share scans for large works
        for item in items[1:]:
            stack.enter_context(_reading_source(item))
        for previous_path in previous_paths:
            _validate_previous_path(
                Path(items[0].lib.dir), previous_path, dest_path, states
            )


async def _get_target_parent(
    item: MediaItem, src_directory: Path, dest_directory: Path
) -> MediaItem:
    """Get the registered target collection for a moved comic chapter.

    Args:
        item: The original chapter with library and parent loaded under the lock.
        src_directory: The chapter's previous container in this library.
        dest_directory: The new chapter container in another work of this library.

    Returns:
        The destination collection; its filesystem layout is validated with the chapter.

    Raises:
        ContentError: If either collection is missing or has incompatible ownership.
    """
    parent = item.parent if item.parent_id is not None else None
    destination = await MediaItem.get_or_none(
        lib_id=item.lib_id, path=str(dest_directory.parent)
    )
    if (
        destination is None
        or item.lib.lib_type != LibType.COMIC
        or item.format not in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
    ):
        raise ContentError("unsupported_layout")
    for candidate, directory in (
        (parent, src_directory.parent),
        (destination, dest_directory.parent),
    ):
        if (
            candidate is None
            or candidate.lib_id != item.lib_id
            or candidate.format is not None
            or candidate.parent_id is not None
            or candidate.path != str(directory)
            or candidate.dir != str(directory)
        ):
            raise ContentError("unsupported_layout")
    return destination


def _read_metadata(
    item: MediaItem, *, with_cover: bool
) -> tuple[MetadataRead, CoverImage | None, str | None]:
    """Read OPF or ComicInfo metadata and optional cover bytes in a worker thread.

    Args:
        item: The reading item with its library and optional parent loaded.
        with_cover: Whether to read the selected cover bytes after metadata.

    Returns:
        Fresh metadata, optional cover bytes and an independent missing-source error.

    Raises:
        ContentError: If source ownership, file reading or stability is invalid.
    """
    from app.core.media.cover import read_cover
    from app.core.media.reader import read_metadata

    with _reading_source(item) as (source, _, missing):
        metadata = read_metadata(source)
        if with_cover and missing:
            raise ContentError("media_source_unavailable")
        cover = read_cover(source, metadata) if with_cover else None
        return metadata, cover, "media_source_unavailable" if missing else None


def _publish_metadata(item: MediaItem, data: bytes, *, overwrite: bool) -> bool:
    """Write an external reading sidecar while preserving source ownership checks.

    Args:
        item: The current reading item with library and parent relations loaded.
        data: Fully rendered and validated candidate XML.
        overwrite: Whether this is an explicitly confirmed replacement.

    Returns:
        Whether XML was published rather than an automatic task using local metadata.

    Raises:
        ContentError: If source validation or external metadata publication fails.
    """
    from app.core.media.writer import write_metadata

    with _reading_source(item) as (source, states, missing):
        if missing:
            raise ContentError("media_source_unavailable")
        written = write_metadata(source, data, states, overwrite=overwrite)
        _validate_reading_source(item, require_candidate=False)
        return written


async def _save_summary(
    item: MediaItem, metadata: MetadataRead, source_error: str | None
) -> str | None:
    """Save current reading summaries and metadata state under the library lock.

    Args:
        item: The current item whose ownership has already been verified.
        metadata: The freshly read external and embedded metadata.
        source_error: An independent source error, or None for an available source.

    Returns:
        The error preserving old summaries, or None after a successful summary update.
    """
    external = metadata.external
    error = source_error or next(
        (
            origin.error
            for origin in (external, metadata.embedded)
            if origin and origin.error
        ),
        None,
    )
    sync = ReadingMetadataSync(
        state="error" if error else "ready" if external else "none",
        format=("opf" if item.lib.lib_type == LibType.NOVEL else "comicinfo")
        if external
        else None,
        relative_path=external.path.relative_to(item.dir).as_posix()
        if external and external.error != "ambiguous_metadata"
        else None,
        file_signature=external.signature if external and not error else None,
        error=error,
    )
    fields = ["extra"]
    if not error:
        for name, value in metadata.summary().items():
            setattr(item, name, value)
            fields.append(name)
        item.poster = f"/_api/media/{item.id}/assets/cover"
        fields.append("poster")
    item.extra = {
        **(item.extra or {}),
        "schema_version": 1,
        "metadata_sync": sync.model_dump(),
    }
    await item.save(update_fields=fields)
    return error


def _check_image_pages(source: ReadingSource, index: ImageIndex, states: _SourceStates):
    """Match directory pages to the index and retain their identities during reading.

    Args:
        source: The validated image directory whose direct body pages are inspected.
        index: The published index with ordered filenames and file snapshots.
        states: The source identities rechecked by the enclosing source guard.

    Raises:
        ContentError: If page names, types or snapshots no longer match the index.
        OSError: If page inspection fails; the enclosing source guard translates it.
    """
    from app.core.media.handlers.reading import (
        identify_comic_source,
        list_source_entries,
    )

    files, _ = list_source_entries(source.directory)
    try:
        current = identify_comic_source(source.directory, files)
    except ValueError as error:
        raise ContentError(str(error)) from error
    if (
        current is None
        or current.format != MediaFormat.DIR
        or [path.name for path in current.pages]
        != [page.relative_path for page in index.pages]
    ):
        raise ContentError("content_changed")
    for path, page in zip(current.pages, index.pages, strict=True):
        info = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size != page.size
            or info.st_mtime_ns != page.mtime_ns
        ):
            raise ContentError("content_changed")
        states[path] = file_state(info)


def _index_is_current(item: MediaItem) -> bool:
    """Check whether a ready body index still matches its source without parsing it.

    Compare source sizes and modification times, plus ordered page names for image
    directories. Metadata and covers are read independently and do not invalidate
    the body. Explicit modification events must still force a rebuild.

    Args:
        item: The reading unit with its library and optional parent loaded.

    Returns:
        Whether the current cache and source pass the incremental reuse checks.
    """
    from app.core.media.image import load_image_index
    from app.core.media.text import load_text_index

    if (
        item.index_state != IndexState.READY
        or item.index_version is None
        or re.fullmatch(r"[0-9a-f]{64}", item.index_version) is None
    ):
        return False
    cache = (
        Path(KaloscopeConfig.get_workspace("temp"))
        / "media_index"
        / str(item.id)
        / item.index_version
    )
    try:
        with _reading_source(item) as (source, states, missing):
            if missing:
                return False
            load = (
                load_text_index
                if item.lib.lib_type == LibType.NOVEL
                else load_image_index
            )
            index = load(cache)
            if index.format != item.format or index.index_version != item.index_version:
                return False
            if index.source_snapshot is not None:
                info = source.path.stat(follow_symlinks=False)
                return (
                    info.st_size == index.source_snapshot.size
                    and info.st_mtime_ns == index.source_snapshot.mtime_ns
                )
            _check_image_pages(source, index, states)
            return True
    except ValueError:
        return False


@contextmanager
def _content_index(
    item: MediaItem,
) -> Generator[tuple[ReadingSource, Path, TextIndex | EpubIndex | ImageIndex]]:
    """Guard a reading source and its published cache throughout a content read.

    Args:
        item: The accessible reading unit with a validated ready index version.

    Yields:
        The validated source, cache directory and current text, EPUB or image index.

    Raises:
        ContentError: If the source or cache is unavailable, changed or invalid.
    """
    from app.core.media.image import load_image_index
    from app.core.media.text import load_text_index

    with _reading_source(item) as (source, source_states, missing):
        if missing:
            raise ContentError("media_source_unavailable")
        root = Path(KaloscopeConfig.get_workspace("temp"))
        cache = root / "media_index" / str(item.id) / str(item.index_version)
        files = [cache / "index.json"]
        if item.lib.lib_type == LibType.NOVEL:
            files.append(
                cache
                / ("content.txt" if item.format == MediaFormat.TXT else "content.jsonl")
            )
        try:
            directories = (root, cache.parent.parent, cache.parent, cache)
            states = {}
            for path in (*directories, *files):
                info = path.stat(follow_symlinks=False)
                directory = path in directories
                if not (
                    stat.S_ISDIR(info.st_mode)
                    if directory
                    else stat.S_ISREG(info.st_mode)
                ):
                    raise ContentError("content_not_ready")
                # sibling cache writes do not change the selected version
                states[path] = file_state(info)[:2] if directory else file_state(info)
            index = (
                load_text_index(cache)
                if item.lib.lib_type == LibType.NOVEL
                else load_image_index(cache)
            )
            if index.format != item.format or index.index_version != item.index_version:
                raise ContentError("content_not_ready")
        except OSError as error:
            raise ContentError("content_not_ready") from error
        if index.source_snapshot is not None:
            info = source.path.stat(follow_symlinks=False)
            if (
                info.st_size != index.source_snapshot.size
                or info.st_mtime_ns != index.source_snapshot.mtime_ns
            ):
                raise ContentError("content_changed")
        else:
            _check_image_pages(source, index, source_states)
        yield source, cache, index
        try:
            for path, before in states.items():
                after = file_state(path.stat(follow_symlinks=False))
                if after[: len(before)] != before:
                    raise ContentError("content_changed")
        except OSError as error:
            raise ContentError("content_not_ready") from error


def _read_text_content(
    item: MediaItem, chapter_id: str | None
) -> TextContent | EpubContent:
    """Read a published novel section without exposing source or cache locations.

    Args:
        item: The accessible novel with a validated ready index version.
        chapter_id: The exact section ID, or None to select the first section.

    Returns:
        Public chapter labels, a live local title, and paragraphs or EPUB blocks.

    Raises:
        ContentError: If the source or cache is unavailable, changed or invalid,
            or the chapter is absent from the current index.
    """
    from app.core.media.reader import read_metadata
    from app.core.media.text import EpubIndex, TextIndex, read_text_chapter

    with _content_index(item) as (source, cache, index):
        assert isinstance(index, (TextIndex, EpubIndex))
        selected = chapter_id or index.chapters[0].id
        content = read_text_chapter(cache, selected)
        values = {
            "item_id": item.id,
            "source_item_id": item.id,
            "title": read_metadata(source).data.title or source.path.stem,
            "version": index.index_version,
            "chapter_id": selected,
            "chapters": [
                ContentChapter(id=chapter.id, title=chapter.title, part=chapter.part)
                for chapter in index.chapters
            ],
        }
        if isinstance(content, list):
            return TextContent.model_validate({**values, "text": content})
        data = content.model_dump(mode="json")
        for block in data["blocks"]:
            if block["type"] == "image":
                asset_id = block["asset_id"]
                block["url"] = (
                    f"/_api/media/{item.id}/assets/{asset_id}?v={index.index_version}"
                    if asset_id is not None
                    else None
                )
        return EpubContent.model_validate({**values, **data})


def _read_image_content(
    item: MediaItem, offset: int, limit: int, page_id: str | None = None
) -> ImageContent:
    """Read one comic's indexed page range with a live local title.

    Args:
        item: The accessible comic source with a validated ready index version.
        offset: The zero-based first page; the page count selects the empty last page.
        limit: The validated batch size from 1 to 100.
        page_id: An indexed page to start from; None uses the numeric offset.

    Returns:
        One source's chapter label, version and bounded image URLs in reading order.

    Raises:
        ContentError: If the source or cache changed, is unavailable or invalid,
            or the requested offset or page ID is outside the current index.
    """
    from app.core.media.image import ImageIndex
    from app.core.media.reader import read_metadata

    with _content_index(item) as (source, _, index):
        assert isinstance(index, ImageIndex)
        count = len(index.pages)
        if page_id is not None:
            offset = next(
                (i for i, page in enumerate(index.pages) if page.id == page_id), -1
            )
            if offset < 0:
                raise ContentError("not_found")
        if offset > count:
            raise ContentError("bad_request")
        title = read_metadata(source).data.title or source.directory.name
        chapter_id = f"item:{item.id}"
        end = min(offset + limit, count)
        return ImageContent(
            item_id=item.id,
            source_item_id=item.id,
            title=title,
            format=index.format,
            version=index.index_version,
            chapter_id=chapter_id,
            chapters=[ContentChapter(id=chapter_id, title=title, part=1)],
            images=[
                f"/_api/media/{item.id}/assets/{page.id}?v={index.index_version}"
                for page in index.pages[offset:end]
            ],
            offset=offset,
            image_count=count,
            next_offset=end if end < count else None,
        )


def _read_asset(item: MediaItem, asset_id: str) -> tuple[bytes, str]:
    """Read one indexed EPUB or comic image within the source and cache guards.

    Args:
        item: The accessible reading unit with a validated ready index version.
        asset_id: The opaque image ID from a published block or comic page list.

    Returns:
        Verified raster bytes and their detected MIME type.

    Raises:
        ContentError: If the resource is unknown, unavailable, changed or invalid.
    """
    from app.core.media.image import read_image_resource
    from app.core.media.text import read_text_resource

    with _content_index(item) as (source, cache, _):
        read = (
            read_text_resource
            if item.lib.lib_type == LibType.NOVEL
            else read_image_resource
        )
        return read(source.path, cache, asset_id)


def _check_locator(
    item: MediaItem, locator: TextLocator | ImageLocator, restore: bool = False
) -> TextLocator | ImageLocator | None:
    """Check an anchor or recover its chapter start from the current content index.

    Args:
        item: The accessible reading unit with a validated content version.
        locator: The paragraph, block or page position submitted by the reader.
        restore: Allow recovery of a stale position; defaults to strict validation.

    Returns:
        The valid locator, a current chapter start, or None if its chapter is gone.

    Raises:
        ContentError: If the anchor is unknown or the source or cache is invalid.
    """
    from app.core.media.image import ImageIndex
    from app.core.media.text import read_text_chapter

    with _content_index(item) as (_, cache, index):
        current = locator.version == index.index_version
        if isinstance(locator, ImageLocator):
            if not isinstance(index, ImageIndex):
                raise ContentError("not_found")
            if current and any(page.id == locator.page_id for page in index.pages):
                return locator
            if restore:
                return locator.model_copy(
                    update={
                        "version": index.index_version,
                        "page_id": index.pages[0].id,
                        "offset": 0,
                    }
                )
        else:
            if isinstance(index, ImageIndex):
                raise ContentError("not_found")
            if not any(chapter.id == locator.chapter_id for chapter in index.chapters):
                if restore:
                    return None
                raise ContentError("not_found")
            content = read_text_chapter(cache, locator.chapter_id)
            if isinstance(content, list):
                valid = locator.paragraph is not None and locator.paragraph < len(
                    content
                )
            else:
                valid = any(block.id == locator.block_id for block in content.blocks)
            if current and valid:
                return locator
            if restore:
                return TextLocator(
                    version=index.index_version,
                    chapter_id=locator.chapter_id,
                    paragraph=0 if isinstance(content, list) else None,
                    block_id=None
                    if isinstance(content, list)
                    else content.blocks[0].id,
                )
        raise ContentError("not_found")


def _build_index(
    item: MediaItem, staging: Path
) -> tuple[TextIndex | EpubIndex | ImageIndex, _SourceStates]:
    """Build a content index and cache while retaining source stability checks.

    Args:
        item: The reading item whose source ownership is checked before parsing.
        staging: A new internal cache directory owned by this build.

    Returns:
        The validated index and source identities to recheck before publication.

    Raises:
        ContentError: If the source cannot be indexed or staging cannot be written.
    """
    from app.core.media.handlers.reading import IMAGE_EXTENSIONS, list_source_entries
    from app.core.media.image import build_image_index
    from app.core.media.text import build_text_index

    with _reading_source(item) as (source, states, missing):
        if missing:
            raise ContentError("media_source_unavailable")
        if source.format == MediaFormat.DIR:
            files, _ = list_source_entries(source.directory)
            states.update(
                (path, file_state(path.stat(follow_symlinks=False)))
                for path in files
                if path.suffix.casefold() in IMAGE_EXTENSIONS
            )
        try:
            staging.parent.mkdir(parents=True, exist_ok=True)
            build = (
                build_text_index
                if item.lib.lib_type == LibType.NOVEL
                else build_image_index
            )
            index = build(source, staging)
        except OSError as error:
            raise ContentError("content_not_ready") from error
    return index, states


def _publish_index(item: MediaItem, staging: Path, version: str, states: _SourceStates):
    """Publish an index cache after revalidating the source under the library lock.

    Args:
        item: The current database item with its library and parent loaded.
        staging: The complete private cache directory.
        version: The validated index version used as the final directory name.
        states: Source identities captured during the build.

    Raises:
        ContentError: If ownership or source identities changed, or publication fails.
    """
    with _reading_source(item) as (_, current_states, missing):
        try:
            changed = any(
                file_state(path.stat(follow_symlinks=False)) != before
                for path, before in states.items()
            )
        except FileNotFoundError as error:
            raise ContentError("content_changed") from error
        if missing or changed:
            raise ContentError("content_changed")
        # retain page checks until the exclusive rename finishes
        current_states.update(states)
        try:
            rename_exclusive(staging, staging.parent / version)
        except OSError as error:
            raise ContentError("content_not_ready") from error


class MediaLibService(BaseService[MediaLib], model=MediaLib):
    """The service class for all media library related operations."""

    @classmethod
    @atomic()
    async def update_priorities(cls, ids: list):
        """Update the media library priorities.

        Args:
            ids: The sorted media library IDs.
        """
        libs = await MediaLib.all()
        if set(ids) != set(lib.id for lib in libs):
            raise KaloscopeException(ErrorCode.BAD_REQUEST)
        # avoid duplicate priorities
        priorities = [lib.priority for lib in libs]
        start_priority = 1 if min(priorities) > len(ids) else max(priorities) + 1
        for lib in libs:
            lib.priority = start_priority + ids.index(lib.id)
        await MediaLib.bulk_update(libs, fields=["priority"])

    @classmethod
    @atomic()
    async def upsert(cls, obj: MediaLibUpsert) -> MediaLib:
        """Create or update a media library.

        Preserve startup scanning and rename settings omitted from updates.

        Args:
            obj: The media library data.

        Raises:
            KaloscopeException: If the name or directory already exists.

        Returns:
            The media library instance.
        """

        # check if the name already exists
        filter = ~Q(id=obj.id) if obj.id else Q()
        if await MediaLib.filter(filter & Q(name=obj.name)).count() > 0:
            raise KaloscopeException(ErrorCode.NAME_ALREADY_EXISTS)
        # check if the directory overlaps with existing ones
        if obj.dir:
            dir = Path(obj.dir).resolve()
            dirs: list = await MediaLib.filter(filter).values_list("dir", flat=True)
            for d in dirs:
                existing = Path(d).resolve()
                if dir.is_relative_to(existing) or existing.is_relative_to(dir):
                    raise KaloscopeException(ErrorCode.DUPLICATE_DIRECTORY)

        lib = await MediaLib.get(id=obj.id) if obj.id else None
        lib_type = lib.lib_type if lib is not None else obj.lib_type
        if lib_type in (LibType.NOVEL, LibType.COMIC) and (
            obj.rename_template or obj.danmaku_server
        ):
            raise BadRequestException()
        if lib is not None:
            extra = {}
            if obj.danmaku_ttl is not None:
                extra["danmaku_ttl"] = obj.danmaku_ttl
            if "scan_on_startup" in obj.model_fields_set:
                extra["scan_on_startup"] = obj.scan_on_startup
            if "rename_template" in obj.model_fields_set:
                try:
                    extra["rename_template"] = validate_template(
                        obj.rename_template or "", lib.lib_type
                    )
                except ValueError as exc:
                    raise BadRequestException() from exc
            # update the media library
            await MediaLib.filter(id=lib.id).update(
                name=obj.name,
                language=obj.language or None,
                danmaku_server=obj.danmaku_server,
                **extra,
            )
            lib = await MediaLib.get(id=lib.id)
        else:
            # create the media library
            priorities: list = await MediaLib.all().values_list("priority", flat=True)
            lib = await MediaLib.create(
                lib_type=obj.lib_type,
                dir=obj.dir,
                name=obj.name,
                language=obj.language or None,
                scan_on_startup=obj.scan_on_startup,
                danmaku_server=obj.danmaku_server,
                danmaku_ttl=obj.danmaku_ttl if obj.danmaku_ttl is not None else 24,
                rename_template=obj.rename_template,
                priority=(max(priorities) + 1 if priorities else 1),
            )

        # bind the flow triggers to the media library
        await FlowTriggerService.bind_triggers(
            GraphCategory.INGEST, lib.id, obj.triggers
        )
        if not obj.id:
            # initial discovery must see the library's workflow bindings
            watcher = cls.app_ctx().lib_watcher
            await watcher.add_observer(lib)

        return lib

    @classmethod
    async def delete(cls, id: int):
        """Delete a media library after active writers finish.

        Args:
            id: The media library ID.
        """
        lib = await MediaLib.get(id=id)
        # wait for active filesystem writers before discarding their journals
        async with library_lock(lib.dir), in_transaction("default"):
            await MediaLib.filter(id=id).delete()
            await FlowTrigger.filter(category=GraphCategory.INGEST, rel_id=id).delete()
            await UserPermission.filter(rel_type=PermType.MEDIA_LIB, rel_id=id).delete()
        # release the DB and library locks before waiting for consumer cancellation
        watcher = cls.app_ctx().lib_watcher
        await watcher.remove_observer(lib.dir)


class MediaItemService(BaseService[MediaItem], model=MediaItem):
    """The service class for all media item related operations."""

    HASH_READ_SIZE = 16 * 1024 * 1024  # 16MB

    @classmethod
    async def ingest_reading_work(
        cls,
        lib_id: int,
        work_path: Path,
        *,
        targets: set[Path] | None = None,
        force: bool = False,
        force_targets: set[Path] | None = None,
    ) -> dict[Path, str]:
        """Ingest reading candidates and revisit indexed comic directories serially.

        The library consumer must call this outside the library lock. Each service
        step revalidates ownership and manages its own lock. Indexed image directories
        and collections retain their identity when empty. Missing or ambiguous sources
        still require separate reconciliation; discovery never implies deletion.

        Args:
            lib_id: The novel or comic library containing the work.
            work_path: The absolute work directory directly below the library root.
            targets: Work or direct comic chapter directories to process; None
                processes the whole work. A chapter selection includes its collection.
            force: Whether to rebuild selected bodies unconditionally; defaults to
                False for incremental scans; True rebuilds every selected body.
            force_targets: Containers whose selected bodies must be rebuilt even
                when size and mtime match; None adds no forced containers. Use this
                for mixed body and metadata events, or force=True for the whole scope.

        Returns:
            Selected source or scope paths mapped to discovery, metadata or body
            errors, including registered bodies missing from discovery. Ownership
            conflicts require reconciliation before registering a replacement.
            Body errors take precedence for a source with multiple failures.
            Indexed empty bodies are normal states and do not add an error.

        Raises:
            DoesNotExist: If the library no longer exists.
            ValueError: If the work or selected targets are outside the allowed layout.
            ContentError: If the library type is unsupported or changes while scanning.
        """
        from app.core.media.handlers.base import get_handler
        from app.core.media.handlers.reading import ReadingSource, is_ignored_name

        lib = await MediaLib.get(id=lib_id)
        if lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
            raise ContentError("unsupported_media_format")
        if (targets is not None and not targets) or any(
            target != work_path
            and (
                lib.lib_type != LibType.COMIC
                or target.parent != work_path
                or is_ignored_name(target.name)
            )
            for target in (targets or set()) | (force_targets or set())
        ):
            raise ValueError("targets must select the work or direct comic chapters")
        if (
            targets is not None
            and force_targets
            and any(
                not any(forced.is_relative_to(target) for target in targets)
                for forced in force_targets
            )
        ):
            raise ValueError("forced containers must be within the selected targets")

        def selected(path: Path) -> bool:
            """Include selected containers and their discovery scopes.

            Args:
                path: A source container or discovery issue scope.

            Returns:
                Whether the path intersects the requested targets.
            """
            return targets is None or any(
                path.is_relative_to(target) or target.is_relative_to(path)
                for target in targets
            )

        handler = get_handler(lib.lib_type)
        scan = await to_thread(handler.scan_sources, lib.dir, work_path=work_path)
        current = await MediaLib.get(id=lib_id)
        if (current.dir, current.lib_type) != (lib.dir, lib.lib_type):
            raise ContentError("content_changed")
        issues = {path: error for path, error in scan.issues.items() if selected(path)}
        sources = [source for source in scan.sources if selected(source.directory)]
        known = await MediaItem.filter(
            Q(dir=str(work_path)) | Q(parent__path=str(work_path)), lib_id=lib_id
        ).select_related("parent")
        discovered = {source.directory: source for source in sources}
        conflicts: set[Path] = set()
        for item in known:
            directory = Path(item.dir)
            if (
                item.format is not None
                and targets is not None
                and not any(directory.is_relative_to(target) for target in targets)
            ):
                continue
            source = ReadingSource(
                Path(item.path),
                item.format,
                Path(item.parent.path) if item.parent is not None else None,
            )
            candidate = discovered.get(directory)
            if candidate is not None:
                if (candidate.path, candidate.format, candidate.parent_path) != (
                    source.path,
                    source.format,
                    source.parent_path,
                ):
                    issues[directory] = "content_changed"
                    conflicts.add(directory)
            elif item.format in (None, MediaFormat.DIR):
                # discovery omits empty containers, but indexed ones still need updates
                sources.append(source)
            elif not any(directory.is_relative_to(scope) for scope in issues):
                # a missing registered body still needs deletion or move reconciliation
                issues[source.path] = "media_source_unavailable"
        sources = [source for source in sources if source.directory not in conflicts]
        if not any(source.format is not None for source in sources) and not any(
            item.path == str(work_path) and item.format is None for item in known
        ):
            return issues
        sources.sort(key=lambda source: source.parent_path is not None)
        collection = None
        for source in sources:
            try:
                item = await cls.create_reading(lib_id, source)
            except ContentError as error:
                issues[source.path] = error.code
                continue
            try:
                item = await cls.sync_metadata(item.id)
                if item.extra is not None:
                    sync = ReadingMetadataSync.model_validate(
                        item.extra["metadata_sync"]
                    )
                    if sync.error is not None:
                        issues[source.path] = sync.error
            except DoesNotExist:
                issues[source.path] = "content_changed"
                continue
            except ContentError as error:
                issues[source.path] = error.code
                if item.format is None:
                    continue
            if item.format is None:
                collection = item
                continue
            try:
                await cls.index_content(
                    item.id,
                    force=force
                    or any(
                        source.directory.is_relative_to(target)
                        for target in force_targets or ()
                    ),
                )
            except ContentError as error:
                # empty bodies wait for source changes; keep any metadata error
                if error.code != "empty_content":
                    issues[source.path] = error.code
            except DoesNotExist:
                issues[source.path] = "content_changed"
        if collection is not None:
            try:
                await cls.sync_collection(collection.id)
            except ContentError as error:
                issues[Path(collection.path)] = error.code
            except DoesNotExist:
                issues[Path(collection.path)] = "content_changed"
        return issues

    @classmethod
    async def create_reading(cls, lib_id: int, source: ReadingSource) -> MediaItem:
        """Get or register a discovered reading source without parsing its content.

        Register comic collections before their chapters. Validate current paths
        under the library lock, then insert only the missing pending item. Retain a
        first-ingest event when workflows are bound, in the same transaction. Existing
        items keep their visibility, summaries and index state; ownership changes
        require reconciliation instead of silently overwriting another source.

        Args:
            lib_id: The reading library containing the discovered source.
            source: The candidate from the reading handler, revalidated before saving.

        Returns:
            The existing item or a new pending item with its library and parent loaded.

        Raises:
            DoesNotExist: If the library no longer exists.
            ContentError: If the type, source, parent or existing ownership is invalid.
        """
        original = await MediaLib.get(id=lib_id)
        async with library_lock(original.dir):
            lib = await MediaLib.get(id=lib_id)
            if lib.dir != original.dir:
                raise ContentError("content_changed")
            if lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
                raise ContentError("unsupported_media_format")
            parent = None
            if source.parent_path is not None:
                parent = await MediaItem.get_or_none(
                    lib_id=lib_id, path=str(source.parent_path)
                )
                if parent is None:
                    raise ContentError("unsupported_layout")
            candidate = MediaItem(
                lib=lib,
                parent=parent,
                path=str(source.path),
                dir=str(source.directory),
                name=source.path.name
                if source.format in (None, MediaFormat.DIR)
                else source.path.stem,
                format=source.format,
                index_state=IndexState.PENDING,
            )
            current = await MediaItem.get_or_none(
                lib_id=lib_id, path=candidate.path
            ).select_related("lib", "parent")
            if current is not None and _source_identity(current) != _source_identity(
                candidate
            ):
                raise ContentError("ambiguous_layout")
            await to_thread(
                _validate_reading_source, candidate, require_candidate=current is None
            )
            if current is not None:
                return current
            async with in_transaction():
                await candidate.save(force_create=True)
                if await FlowTriggerService.get_triggers(GraphCategory.INGEST, lib_id):
                    await MediaEvent.create(
                        lib_id=lib_id,
                        event_type="ingest",
                        src_path=candidate.path,
                        payload={"bootparams": [{"item_id": candidate.id}]},
                    )
            notify_media_events(lib_id)
            return candidate

    @classmethod
    async def consume_ingest(cls, id: int) -> bool:
        """Dispatch a reading item's first ingest after it becomes readable.

        Reuse the workflow engine's durable execution and current library bindings.
        Ordinary source changes do not create these events. Failed events stop without
        blocking manual scraping; interruption leaves the event available on restart.

        Args:
            id: The persisted ingest event ID selected by the library consumer.

        Returns:
            True after dispatch or removal of obsolete work; False while waiting for
            the initial index, or when the event is absent or has already failed.

        Raises:
            ContentError: If current ownership or local metadata cannot be read safely.
            ValueError: If the event does not contain one valid reading item ID.
            Exception: If workflow dispatch fails; its failure is persisted first.
        """
        event = await MediaEvent.get_or_none(id=id, event_type="ingest").select_related(
            "lib"
        )
        if event is None or event.lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
            return False
        directory = event.lib.dir
        try:
            async with library_lock(directory):
                event = await MediaEvent.get_or_none(
                    id=id, event_type="ingest"
                ).select_related("lib")
                if event is None:
                    return False
                if event.lib.dir != directory or event.lib.lib_type not in (
                    LibType.NOVEL,
                    LibType.COMIC,
                ):
                    raise ContentError("content_changed")
                pending = (event.payload or {}).get("bootparams")
                if (
                    not isinstance(pending, list)
                    or len(pending) != 1
                    or not isinstance(pending[0], dict)
                    or type(pending[0].get("item_id")) is not int
                    or pending[0]["item_id"] <= 0
                ):
                    if (event.payload or {}).get("error_code") == "invalid_metadata":
                        return False
                    raise ValueError("invalid reading ingest parameters")
                item = await MediaItem.get_or_none(
                    id=pending[0]["item_id"], lib_id=event.lib_id
                ).select_related("lib", "parent")
                if (
                    item is None
                    or not item.visible
                    or (item.parent is not None and not item.parent.visible)
                    or not await FlowTriggerService.get_triggers(
                        GraphCategory.INGEST, event.lib_id
                    )
                ):
                    await event.delete()
                    return True
                if (event.payload or {}).get("error_code"):
                    return False
                # wait for first readability without binding scraping to a cache version
                if item.index_state != IndexState.READY:
                    return False
                metadata, _, source_error = await to_thread(
                    _read_metadata, item, with_cover=False
                )
                current = await MediaItem.get_or_none(id=item.id).select_related(
                    "lib", "parent"
                )
                if current is None or _source_identity(current) != _source_identity(
                    item
                ):
                    raise ContentError("content_changed")
                error = await _save_summary(current, metadata, source_error)
                if metadata.has_local_metadata:
                    await event.delete()
                    return True
                if error:
                    raise ContentError(error)
                fields = metadata.data
                params = {
                    "item_id": item.id,
                    "item_path": item.path,
                    "item_name": item.name,
                    "item_role": _reading_role(item),
                    "lib_type": item.lib.lib_type.value,
                    "title": fields.title or item.name,
                    "series_title": fields.series
                    or (item.parent.title or item.parent.name if item.parent else None),
                    "number": fields.number,
                    "language": fields.language or item.lib.language,
                    "year": fields.year,
                    "nfo_path": None,
                    "nfo_type": None,
                    "nfo_source": None,
                    "season": None,
                    "episode": None,
                    "series_id": None,
                    "page_num": 1,
                    "page_size": 1,
                }
                event.payload = {"bootparams": [params]}
                await event.save(update_fields=["payload"])
            # workflow nodes acquire the same library lock when saving their result
            await FlowTriggerService.fire(
                GraphCategory.INGEST, event.lib_id, bootparams=params
            )
            await event.delete()
            return True
        except Exception as error:
            if event is not None:
                await MediaEvent.filter(id=event.id).update(
                    payload={
                        **(event.payload or {}),
                        "error_code": error.code
                        if isinstance(error, ContentError)
                        else "invalid_metadata"
                        if isinstance(error, ValueError)
                        else "workflow_failed",
                    }
                )
            raise

    @classmethod
    async def move_reading_file(
        cls,
        lib_id: int,
        src_path: Path,
        dest_path: Path,
        *,
        previous_paths: tuple[Path, ...] = (),
    ) -> MediaItem | None:
        """Apply an observed body-file move while retaining its registered identity.

        Call from the serial library consumer before destination ingestion, using a
        persisted filesystem move. Accept renames or moves between work or chapter
        containers. Register a destination collection before moving a chapter across
        works. Files and histories stay untouched. Retain the old cache for cleanup
        and mark the item pending for indexing at its new path.

        Args:
            lib_id: The novel or comic library containing the moved file.
            src_path: The absolute previous body path from the move event.
            dest_path: The absolute new body path at the same depth in this library.
            previous_paths: Other former paths in a continuous move chain, excluding
                src_path. The default empty tuple handles a single move.

        Returns:
            The updated item with its original ID, or None if the previous path is
            not registered, including when this move was already applied.

        Raises:
            DoesNotExist: If the library no longer exists.
            ValueError: If paths are hidden, outside the library or change between
                standalone works and chapters.
            ContentError: If ownership, format, layout or source stability is invalid,
                the previous path is reused, or another item owns the destination.
        """
        from app.core.media.handlers.reading import is_ignored_name

        original = await MediaLib.get(id=lib_id)
        root = Path(original.dir)
        if (
            not root.is_absolute()
            or ".." in root.parts
            or src_path == dest_path
            or any(
                path.parent.parent != dest_path.parent.parent
                and not (
                    original.lib_type == LibType.COMIC
                    and path.parent.parent.parent == root
                    and dest_path.parent.parent.parent == root
                )
                for path in (src_path, *previous_paths)
            )
            or any(
                not path.is_relative_to(root)
                or ".." in path.parts
                or any(is_ignored_name(part) for part in path.relative_to(root).parts)
                for path in (src_path, dest_path, *previous_paths)
            )
        ):
            raise ValueError("move paths must select containers at the same depth")
        async with library_lock(original.dir):
            lib = await MediaLib.get(id=lib_id)
            if (lib.dir, lib.lib_type) != (original.dir, original.lib_type):
                raise ContentError("content_changed")
            if lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
                raise ContentError("unsupported_media_format")
            item = await MediaItem.get_or_none(
                lib_id=lib_id, path=str(src_path)
            ).select_related("lib", "parent")
            if item is None:
                return None
            if item.format not in (
                MediaFormat.TXT,
                MediaFormat.EPUB,
                MediaFormat.CBZ,
                MediaFormat.ZIP,
            ) or any(
                path.suffix.casefold() != f".{item.format}"
                for path in (src_path, dest_path, *previous_paths)
            ):
                raise ContentError("unsupported_media_format")
            if item.dir != str(src_path.parent):
                raise ContentError("unsupported_layout")
            if src_path.parent.parent != dest_path.parent.parent:
                item.parent = await _get_target_parent(
                    item, src_path.parent, dest_path.parent
                )
            if (
                await MediaItem.filter(
                    Q(path__in=[str(path) for path in (dest_path, *previous_paths)])
                    | Q(dir=str(dest_path.parent)),
                    lib_id=lib_id,
                )
                .exclude(id=item.id)
                .exists()
            ):
                raise ContentError("ambiguous_layout")
            item.path = str(dest_path)
            item.dir = str(dest_path.parent)
            item.name = dest_path.stem
            await to_thread(
                _validate_reading_source,
                item,
                require_candidate=True,
                previous_paths=(src_path, *previous_paths),
            )
            item.index_state = IndexState.PENDING
            item.index_error = None
            await item.save(
                update_fields=[
                    "path",
                    "dir",
                    "name",
                    "parent_id",
                    "index_state",
                    "index_error",
                ]
            )
            return item

    @classmethod
    async def move_reading_directory(
        cls,
        lib_id: int,
        src_path: Path,
        dest_path: Path,
        *,
        previous_paths: tuple[Path, ...] = (),
    ) -> list[MediaItem]:
        """Apply an observed work or chapter directory move within the same library.

        Call from the serial library consumer before destination ingestion. Rebase
        selected paths in one transaction after checking source ownership. Register
        the destination collection before moving chapters across works. Preserve
        histories under their original work; update chapter parents, retain summaries
        and previous caches, and mark indexes pending for subsequent ingestion of
        both works. This operation does not rename files on disk.

        Args:
            lib_id: The novel or comic library containing the moved directory.
            src_path: The absolute previous work or comic chapter directory.
            dest_path: The absolute new directory at the same depth in this library.
            previous_paths: Other former directories in a continuous move chain,
                excluding src_path. The default empty tuple handles a single move.

        Returns:
            Updated items with their original IDs and parents loaded, or an empty
            list if no source records remain, including an already applied move.

        Raises:
            DoesNotExist: If the library no longer exists.
            ValueError: If paths are not distinct visible work or chapter containers
                at the same depth in this library.
            ContentError: If ownership, layout or stability is invalid, the source
                directory is reused, or any item already occupies the destination.
        """
        from app.core.media.handlers.reading import is_ignored_name

        original = await MediaLib.get(id=lib_id)
        root = Path(original.dir)
        if (
            not root.is_absolute()
            or ".." in root.parts
            or src_path == dest_path
            or any(
                (path.parent == root) != (dest_path.parent == root)
                for path in (src_path, *previous_paths)
            )
            or any(
                path == root
                or not path.is_relative_to(root)
                or ".." in path.parts
                or (
                    path.parent != root
                    and (
                        original.lib_type != LibType.COMIC or path.parent.parent != root
                    )
                )
                or any(is_ignored_name(part) for part in path.relative_to(root).parts)
                for path in (src_path, dest_path, *previous_paths)
            )
        ):
            raise ValueError(
                "move paths must select visible work or chapter containers"
            )
        async with library_lock(original.dir):
            lib = await MediaLib.get(id=lib_id)
            if (lib.dir, lib.lib_type) != (original.dir, original.lib_type):
                raise ContentError("content_changed")
            if lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
                raise ContentError("unsupported_media_format")
            candidates = await MediaItem.filter(
                Q(path=str(src_path))
                | Q(path__startswith=f"{src_path}/")
                | Q(dir=str(src_path))
                | Q(dir__startswith=f"{src_path}/"),
                lib_id=lib_id,
            ).select_related("lib", "parent")
            # SQLite prefix matching may also return differently cased directories
            items = [
                item
                for item in candidates
                if Path(item.path).is_relative_to(src_path)
                or Path(item.dir).is_relative_to(src_path)
            ]
            if not items:
                return []
            by_id = {item.id: item for item in items}
            if (
                await MediaItem.filter(parent_id__in=by_id)
                .exclude(id__in=by_id)
                .exists()
            ):
                raise ContentError("unsupported_layout")
            destinations = (dest_path, *previous_paths)
            occupied_query = Q()
            for path in destinations:
                occupied_query |= (
                    Q(path=str(path))
                    | Q(path__startswith=f"{path}/")
                    | Q(dir=str(path))
                    | Q(dir__startswith=f"{path}/")
                )
            occupied = await MediaItem.filter(occupied_query, lib_id=lib_id).exclude(
                id__in=by_id
            )
            if any(
                Path(item.path).is_relative_to(path)
                or Path(item.dir).is_relative_to(path)
                for item in occupied
                for path in destinations
            ):
                raise ContentError("ambiguous_layout")
            for item in items:
                if not Path(item.path).is_relative_to(src_path) or not Path(
                    item.dir
                ).is_relative_to(src_path):
                    raise ContentError("unsupported_layout")
                if src_path.parent != dest_path.parent:
                    item.parent = await _get_target_parent(item, src_path, dest_path)
                elif item.parent_id is not None:
                    if item.parent_id in by_id:
                        item.parent = by_id[item.parent_id]
                    elif (
                        item.parent is None or Path(item.parent.path) != src_path.parent
                    ):
                        raise ContentError("unsupported_layout")
                item.path = str(dest_path / Path(item.path).relative_to(src_path))
                item.dir = str(dest_path / Path(item.dir).relative_to(src_path))
                item.name = (
                    Path(item.path).name
                    if item.format in (None, MediaFormat.DIR)
                    else Path(item.path).stem
                )
                item.index_state = IndexState.PENDING
                item.index_error = None
            await to_thread(
                _validate_reading_directory_move,
                items,
                (src_path, *previous_paths),
                dest_path,
            )
            async with in_transaction():
                await MediaItem.bulk_update(
                    items,
                    fields=[
                        "path",
                        "dir",
                        "name",
                        "parent_id",
                        "index_state",
                        "index_error",
                    ],
                )
            return items

    @classmethod
    async def remove_missing_reading_item(
        cls, id: int, *, directory: Path | None = None, source_path: Path | None = None
    ) -> list[int]:
        """Remove an absent reading source, its owned children, caches and histories.

        Call from the serial consumer after stable observations and reliable moves
        have been applied. Remove exclusive companions of missing files first. When the
        whole container disappeared, require that captured directory to remain absent
        so a restored container cannot lose its record. Recheck absence instead of
        treating discovery as deletion. Unrelated and shared files stay untouched.
        Empty image directories and collections are retained; surviving collections
        are summarized by subsequent work ingestion. If every registered work directory
        disappears, retain records until library access can be confirmed again.

        Args:
            id: The registered reading item selected for missing-source reconciliation.
            directory: The captured container that must still belong to this item and
                be absent. None also allows missing bodies in surviving containers.
            source_path: The captured body path that must still belong to this item;
                None uses the item's current path for internal callers.

        Returns:
            Removed item IDs, or an empty list if the item is gone or a source exists.

        Raises:
            ContentError: If ownership, absence or cache cleanup cannot be confirmed.
                Failed cleanup keeps database owners for retry; companions or
                rebuildable caches may already be partially or fully removed.
            asyncio.CancelledError: After any active cache writer stops.
        """
        original = await MediaItem.get_or_none(id=id).select_related("lib", "parent")
        if original is None:
            return []
        if directory is not None and Path(original.dir) != directory:
            raise ContentError("content_changed")
        if source_path is not None and Path(original.path) != source_path:
            raise ContentError("content_changed")
        async with library_lock(original.lib.dir):
            items = await MediaItem.filter(Q(id=id) | Q(parent_id=id)).select_related(
                "lib", "parent"
            )
            item = next((row for row in items if row.id == id), None)
            if item is None:
                return []
            if _source_identity(item) != _source_identity(original):
                raise ContentError("content_changed")
            ids = [row.id for row in items]
            if await MediaItem.filter(parent_id__in=ids).exclude(id__in=ids).exists():
                raise ContentError("unsupported_layout")
            work_paths = {
                Path(directory)
                for (directory,) in await MediaItem.filter(
                    lib_id=item.lib_id, parent_id__isnull=True
                ).values_list("dir")
            }
            states = await to_thread(
                _missing_reading_sources,
                items,
                work_paths,
                directories_only=directory is not None,
            )
            if states is None:
                return []
            removed = []
            if item.format not in (None, MediaFormat.DIR) and Path(item.dir) in states:
                others = (
                    await MediaItem.filter(lib_id=item.lib_id, dir__startswith=item.dir)
                    .exclude(id=id)
                    .only("dir")
                )
                if any(Path(row.dir).is_relative_to(item.dir) for row in others):
                    raise ContentError("content_changed")
                states, removed = await write_in_thread(
                    _remove_reading_companions, item, work_paths, states
                )
            await write_in_thread(_remove_reading_caches, ids)
            async with in_transaction():
                current = await MediaItem.filter(
                    Q(id__in=ids) | Q(parent_id__in=ids)
                ).select_related("lib", "parent")
                if {row.id: _source_identity(row) for row in current} != {
                    row.id: _source_identity(row) for row in items
                }:
                    raise ContentError("content_changed")
                if (
                    await to_thread(
                        _missing_reading_sources,
                        items,
                        work_paths,
                        directories_only=directory is not None,
                    )
                    != states
                ):
                    raise ContentError("content_changed")
                await _remove_media_records(items)
            if Path(item.dir) in states:
                await write_in_thread(
                    _prune_media_directories, Path(item.dir), removed, states
                )
            return ids

    @classmethod
    async def sync_metadata(cls, id: int) -> MediaItem:
        """Synchronize reading list summaries without rebuilding content or writing XML.

        Hold the library lock while reading and saving so a concurrent manual save
        cannot be followed by older list summaries. Recheck ownership after reading.
        Failed item-owned reads preserve summaries; parent metadata stays read-only.

        Args:
            id: The internal reading item ID selected by the library consumer.

        Returns:
            The item with current list summaries or a recorded metadata read error.

        Raises:
            DoesNotExist: If the item was removed before synchronization starts.
            ContentError: If the library type, source boundary or ownership is invalid.
        """
        item = await MediaItem.get(id=id).select_related("lib", "parent")
        if item.lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
            raise ContentError("unsupported_media_format")
        async with library_lock(item.lib.dir):
            metadata, _, source_error = await to_thread(
                _read_metadata, item, with_cover=False
            )
            current = await MediaItem.get_or_none(id=id).select_related("lib", "parent")
            if current is None or _source_identity(item) != _source_identity(current):
                raise ContentError("content_changed")
            await _save_summary(current, metadata, source_error)
            return current

    @classmethod
    async def save_metadata(
        cls, id: int, data: dict, *, overwrite: bool = False
    ) -> bool:
        """Persist and execute an OPF or ComicInfo save for the current reading item.

        Args:
            id: The existing reading item ID, including a comic collection or chapter.
            data: The complete candidate, without paths or fields from old metadata.
            overwrite: False for automatic creation; True after manual confirmation.

        Returns:
            Whether metadata was published, rather than skipped for valid local data.

        Raises:
            ContentError: If the candidate, source, publication or summary is invalid.
        """
        from app.core.media.writer import MetadataWrite

        try:
            candidate = ReadingMetadata.model_validate_json(
                json.dumps(data, allow_nan=False)
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise ContentError("invalid_metadata") from error
        item = await MediaItem.get_or_none(id=id).select_related("lib", "parent")
        if item is None:
            raise ContentError("not_found")
        _resolve_source(item)
        task = MetadataWrite(
            item_id=id, metadata=candidate, overwrite=overwrite, identifier=uuid4()
        )
        # reject unrepresentable candidates before replacing any pending save
        await to_thread(
            render_metadata,
            candidate,
            "opf" if item.lib.lib_type == LibType.NOVEL else "comicinfo",
            identifier=task.identifier,
        )
        async with library_lock(item.lib.dir):
            current = await MediaItem.get_or_none(id=id).select_related("lib", "parent")
            if current is None or _source_identity(item) != _source_identity(current):
                raise ContentError("content_changed")
            async with in_transaction():
                for pending in await MediaEvent.filter(
                    lib_id=current.lib_id, event_type="metadata"
                ):
                    if (pending.payload or {}).get("item_id") == id:
                        if not overwrite:
                            raise ContentError("content_not_ready")
                        await pending.delete()
                event = await MediaEvent.create(
                    lib_id=current.lib_id,
                    event_type="metadata",
                    src_path=current.path,
                    payload=task.model_dump(mode="json"),
                )
            try:
                return await cls._write_metadata(event, current, task)
            finally:
                # an interrupted writer leaves durable work for the library consumer
                notify_media_events(current.lib_id)

    @classmethod
    async def _write_metadata(
        cls, event: MediaEvent, item: MediaItem, task: MetadataWrite
    ) -> bool:
        """Publish a candidate and complete its summary under the library lock.

        Args:
            event: The current save task, loaded again by the caller holding the lock.
            item: The current item in the task's library, with its parent loaded.
            task: The validated candidate and publication state stored in the event.

        Returns:
            Whether this task published metadata instead of using valid local data.

        Raises:
            ContentError: If task validation, file writing or summary reading fails.
        """
        try:
            if task.item_id != item.id or event.lib_id != item.lib_id:
                raise ContentError("content_changed")
            _resolve_source(item)
            written = task.published
            if not task.published:
                data = await to_thread(
                    render_metadata,
                    task.metadata,
                    "opf" if item.lib.lib_type == LibType.NOVEL else "comicinfo",
                    identifier=task.identifier,
                )
                written = await write_in_thread(
                    _publish_metadata, item, data, overwrite=task.overwrite
                )
                if written:
                    task.published = True
                    event.payload = task.model_dump(mode="json")
                    await event.save(update_fields=["payload"])
            # recovery after publication only reads current files, never replays XML
            metadata, _, source_error = await to_thread(
                _read_metadata, item, with_cover=False
            )
            async with in_transaction():
                current = await MediaItem.get_or_none(id=item.id).select_related(
                    "lib", "parent"
                )
                if current is None or _source_identity(current) != _source_identity(
                    item
                ):
                    raise ContentError("content_changed")
                error = await _save_summary(current, metadata, source_error)
                if error:
                    raise ContentError(error)
                await event.delete()
            return written
        except ContentError as error:
            event.payload = {**(event.payload or {}), "error_code": error.code}
            await event.save(update_fields=["payload"])
            raise

    @classmethod
    async def consume_metadata(cls, id: int) -> bool:
        """Resume an interrupted metadata save without replaying a completed file phase.

        Args:
            id: The persisted metadata event ID selected by the library consumer.

        Returns:
            True when the task finishes or its item was removed; False for absent or
            failed tasks. A new explicit save replaces failed work for the same item.

        Raises:
            ContentError: If execution fails; its error is persisted to prevent loops.
        """
        from app.core.media.writer import MetadataWrite

        event = await MediaEvent.get_or_none(
            id=id, event_type="metadata"
        ).select_related("lib")
        if event is None:
            return False
        directory = event.lib.dir
        async with library_lock(directory):
            event = await MediaEvent.get_or_none(
                id=id, event_type="metadata"
            ).select_related("lib")
            if event is None:
                return False
            if event.lib.dir != directory:
                raise ContentError("content_changed")
            try:
                task = MetadataWrite.model_validate_json(json.dumps(event.payload))
            except (ValidationError, TypeError, ValueError):
                if (event.payload or {}).get("error_code") == "invalid_metadata":
                    return False
                event.payload = {
                    **(event.payload or {}),
                    "error_code": "invalid_metadata",
                }
                await event.save(update_fields=["payload"])
                return False
            item = await MediaItem.get_or_none(
                id=task.item_id, lib_id=event.lib_id
            ).select_related("lib", "parent")
            if item is None:
                await event.delete()
                return True
            if task.error_code:
                return False
            await cls._write_metadata(event, item, task)
            return True

    @classmethod
    async def sync_collection(cls, id: int) -> MediaItem:
        """Summarize a comic collection from its current visible chapters.

        The library consumer calls this after registering or reconciling chapters,
        including failed builds, removals and visibility changes. Readiness follows
        published chapter states; source and cache validation stay with the reader.
        Collections have chapter counts but no body index or combined page count.

        Args:
            id: The internal ID of a top-level comic collection.

        Returns:
            The collection with its aggregate state and current chapter count.

        Raises:
            DoesNotExist: If the collection was removed before synchronization.
            ContentError: If the library changes or the item is not a collection.
        """
        original = await MediaItem.get(id=id).select_related("lib")
        async with library_lock(original.lib.dir):
            item = await MediaItem.get(id=id).select_related("lib")
            if item.lib_id != original.lib_id or item.lib.dir != original.lib.dir:
                raise ContentError("content_changed")
            if (
                item.lib.lib_type != LibType.COMIC
                or item.format is not None
                or item.parent_id is not None
            ):
                raise ContentError("unsupported_media_format")
            chapters = (
                await MediaItem.filter(
                    lib_id=item.lib_id,
                    parent_id=id,
                    visible=True,
                    format__in=(MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP),
                )
                .order_by("path", "id")
                .only("id", "index_state", "index_error")
            )
            states = {chapter.index_state for chapter in chapters}
            if IndexState.READY in states:
                state, error = IndexState.READY, None
            elif IndexState.ERROR in states:
                state = IndexState.ERROR
                error = next(
                    (
                        chapter.index_error
                        for chapter in chapters
                        if chapter.index_state == IndexState.ERROR
                        and chapter.index_error is not None
                    ),
                    None,
                )
            elif states - {IndexState.EMPTY}:
                state, error = IndexState.PENDING, None
            else:
                state, error = IndexState.EMPTY, "empty_content"
            item.extra = {
                **(item.extra or {}),
                "schema_version": 1,
                "content": {"chapter_count": len(chapters), "page_count": None},
            }
            item.index_state = state
            item.index_error = error
            await item.save(update_fields=["extra", "index_state", "index_error"])
            if state == IndexState.READY:
                notify_media_events(item.lib_id)
            return item

    @classmethod
    async def index_content(cls, id: int, *, force: bool = True) -> MediaItem:
        """Rebuild and publish content for one persisted reading unit.

        The library's single event consumer must await builds serially and decide
        when rebuilding is required. Ordinary scans can reuse a matching ready
        index; body modification events must force rebuilding even when file sizes
        and modification times match. All paths must enqueue work for the consumer.
        Collections derive their state from children and cannot be indexed as a
        body. Cache directories publish before the database pointer;
        old and orphaned versions are retained for separate cleanup. Cancellation
        leaves pending work safe to retry.

        Args:
            id: The internal reading item ID selected by the library consumer.
            force: Whether to rebuild unconditionally; defaults to True to preserve
                explicit rebuilds. False permits reuse after source and cache checks.

        Returns:
            The item with a reused or rebuilt ready version and content counts.

        Raises:
            DoesNotExist: If the item was removed before indexing starts.
            ContentError: If the source is unsupported, empty, unstable or invalid,
                ownership changes or publication fails.
            asyncio.CancelledError: After active writers stop, leaving pending work.
        """
        from app.core.media.image import ImageIndex

        original = await MediaItem.get(id=id).select_related("lib")
        directory = original.lib.dir
        async with library_lock(directory):
            item = await MediaItem.get(id=id, lib_id=original.lib_id).select_related(
                "lib", "parent"
            )
            if item.lib.dir != directory:
                raise ContentError("content_changed")
            if (
                item.lib.lib_type not in (LibType.NOVEL, LibType.COMIC)
                or item.format is None
            ):
                raise ContentError("unsupported_media_format")
            if not force and await to_thread(_index_is_current, item):
                return item
            item.index_state = IndexState.PENDING
            item.index_error = None
            await item.save(update_fields=["index_state", "index_error"])

        staging = (
            Path(KaloscopeConfig.get_workspace("temp"))
            / "media_index"
            / str(id)
            / f"building_{secrets.token_hex(16)}.tmp"
        )
        try:
            index, states = await write_in_thread(_build_index, item, staging)
            async with library_lock(directory):
                current = await MediaItem.get_or_none(id=id).select_related(
                    "lib", "parent"
                )
                if current is None or _source_identity(item) != _source_identity(
                    current
                ):
                    raise ContentError("content_changed")
                await write_in_thread(
                    _publish_index, current, staging, index.index_version, states
                )
                extra = dict(current.extra or {})
                extra["schema_version"] = 1
                extra["content"] = {
                    "chapter_count": None
                    if isinstance(index, ImageIndex)
                    else len(index.chapters),
                    "page_count": len(index.pages)
                    if isinstance(index, ImageIndex)
                    else None,
                }
                current.extra = extra
                current.size = (
                    index.source_snapshot.size
                    if index.source_snapshot is not None
                    else sum(page.size for page in index.pages)
                    if isinstance(index, ImageIndex)
                    else None
                )
                current.index_version = index.index_version
                current.index_state = IndexState.READY
                current.index_error = None
                async with in_transaction():
                    await current.save(
                        update_fields=[
                            "extra",
                            "size",
                            "index_version",
                            "index_state",
                            "index_error",
                        ]
                    )
                notify_media_events(current.lib_id)
                return current
        except ContentError as error:
            async with library_lock(directory):
                current = await MediaItem.get_or_none(id=id).select_related(
                    "lib", "parent"
                )
                if current is not None and _source_identity(item) == _source_identity(
                    current
                ):
                    current.index_state = (
                        IndexState.EMPTY
                        if error.code == "empty_content"
                        else IndexState.PENDING
                        if error.code
                        in {
                            "content_changed",
                            "media_source_unavailable",
                            "content_not_ready",
                        }
                        else IndexState.ERROR
                    )
                    current.index_error = error.code
                    await current.save(update_fields=["index_state", "index_error"])
            raise
        finally:
            await write_in_thread(shutil.rmtree, staging, ignore_errors=True)

    @classmethod
    async def get_accessible(cls, id: int, user: UserInfo) -> MediaItem:
        """Get a visible item after checking its library and parent access.

        Args:
            id: The requested media item ID.
            user: The authenticated user with permissions loaded by authorize.

        Returns:
            The item with its library and optional parent loaded.

        Raises:
            NotFoundException: If the item or its valid visible parent is missing.
            ForbiddenException: If the user cannot access the library.
        """
        item = await MediaItem.get_or_none(id=id, visible=True).select_related(
            "lib", "parent"
        )
        if item is None:
            raise NotFoundException()
        if user.role != UserRole.ADMIN and (
            user.perms is None or item.lib_id not in user.perms.media_lib_ids
        ):
            raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
        if item.parent_id is not None and (
            item.parent is None
            or not item.parent.visible
            or item.parent.lib_id != item.lib_id
        ):
            raise NotFoundException()
        return item

    @classmethod
    async def _get_metadata(
        cls, item: MediaItem, user: UserInfo, *, with_cover: bool
    ) -> tuple[MediaItem, MetadataRead, CoverImage | None, str | None]:
        """Get reading metadata and an optional cover with access revalidation.

        Retry once if the source or database ownership changes during the read.

        Args:
            item: The initially accessible reading item.
            user: The authenticated user used for access revalidation.
            with_cover: Whether to include the current cover bytes.

        Returns:
            The current item, metadata, optional cover and missing-source error.

        Raises:
            ContentError: If source reading fails or changes repeatedly.
            NotFoundException: If the item becomes hidden or is removed.
            ForbiddenException: If library access is no longer allowed.
        """
        for attempt in range(2):
            try:
                metadata, cover, source_error = await to_thread(
                    _read_metadata, item, with_cover=with_cover
                )
            except ContentError as error:
                if error.code != "content_changed" or attempt:
                    raise
            else:
                current = await cls.get_accessible(item.id, user)
                if _source_identity(current) == _source_identity(item):
                    return current, metadata, cover, source_error
            item = await cls.get_accessible(item.id, user)
        raise ContentError("content_changed")

    @classmethod
    async def get_details(cls, id: int, user: UserInfo) -> dict[str, Any]:
        """Build accessible details from current NFO, OPF or ComicInfo metadata.

        Args:
            id: The media item ID.
            user: The authenticated user with loaded library permissions.

        Returns:
            Details with current metadata and controlled source issues.

        Raises:
            NotFoundException: If the item or its parent is unavailable.
            ForbiddenException: If library access is denied.
        """
        item = await cls.get_accessible(id, user)
        reading = item.lib.lib_type in (LibType.NOVEL, LibType.COMIC)
        metadata = None
        nfo = None
        source_error = None

        # read current metadata before assembling the shared details
        if reading:
            try:
                item, metadata, _, source_error = await cls._get_metadata(
                    item, user, with_cover=False
                )
            except ContentError as error:
                source_error = error.code
                item = await cls.get_accessible(id, user)
        else:
            from app.core.media.shelver import parse_nfo

            nfo = (
                await to_thread(parse_nfo, item.lib.lib_type, item.nfo_path)
                if item.nfo_path
                else None
            )
            await cls.get_accessible(id, user)

        data = await cls.dump(item, exclude={"parent", "children"})
        data["parent"] = (
            await cls.dump(item.parent, exclude={"parent", "children", "lib"})
            if item.parent_id is not None and item.parent is not None
            else None
        )
        children = await MediaItem.filter(
            parent_id=id, lib_id=item.lib_id, visible=True
        )
        data["children"] = await cls.dump_list(
            children, exclude={"parent", "children", "lib"}
        )
        data["lib_type"] = item.lib.lib_type
        data["media_type"] = (
            "text"
            if item.lib.lib_type == LibType.NOVEL
            else "image"
            if reading
            else "video"
        )
        if not reading:
            data["metadata"] = asdict(nfo) if nfo is not None else None
            return data
        data["item_role"] = _reading_role(item)
        path = Path(item.path)
        fields = (
            metadata.data
            if metadata is not None
            else ReadingMetadata(
                title=path.name if item.format in (None, MediaFormat.DIR) else path.stem
            )
        ).model_dump(exclude={"cover"})
        if fields["rating"] is not None:
            fields["rating"] = float(fields["rating"])
        # fixed local URLs resolve current covers without exposing source references
        fields["poster"] = f"/_api/media/{item.id}/assets/cover"
        data["metadata"] = fields
        for key in ("title", "year", "rating", "poster"):
            data[key] = fields[key]
        data["backdrop"] = None
        data["metadata_state"] = (
            metadata.state if metadata is not None and source_error is None else "error"
        )
        data["metadata_issues"] = (
            [
                {
                    "source": name,
                    "error": origin.error,
                    "invalid_fields": origin.parsed.invalid_fields
                    if origin.parsed
                    else (),
                }
                for name, origin in (
                    ("external", metadata.external),
                    ("embedded", metadata.embedded),
                    ("parent", metadata.parent),
                )
                if origin is not None
                and (origin.error or (origin.parsed and origin.parsed.invalid_fields))
            ]
            if metadata is not None
            else []
        )
        if source_error is not None:
            data["metadata_issues"].append(
                {"source": "source", "error": source_error, "invalid_fields": []}
            )
        return data

    @classmethod
    async def get_cover(cls, id: int, user: UserInfo) -> CoverImage | None:
        """Read a current cover for an accessible reading item.

        Args:
            id: The media item ID.
            user: The authenticated user with loaded library permissions.

        Returns:
            Verified cover bytes, or None when a placeholder is needed.

        Raises:
            NotFoundException: If the item is unavailable or belongs to a video library.
            ForbiddenException: If library access is denied.
            ContentError: If source or image reading fails.
        """
        item = await cls.get_accessible(id, user)
        if item.lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
            raise NotFoundException()
        _, _, cover, _ = await cls._get_metadata(item, user, with_cover=True)
        return cover

    @classmethod
    @asynccontextmanager
    async def _content_item(
        cls, id: int, user: UserInfo, version: str | None
    ) -> AsyncGenerator[MediaItem]:
        """Check access and published version before and after unlocked reading.

        Args:
            id: The requested reading item ID.
            user: The authenticated user with loaded library permissions.
            version: The expected published version, or None for the current one.

        Yields:
            An accessible collection or ready reading source for one operation.

        Raises:
            NotFoundException: If the item is hidden, missing or not reading media.
            ForbiddenException: If library access is denied or revoked during reading.
            ContentError: If the format is unsupported, the index is not ready,
                sources change, or the requested chapter or version is unavailable.
        """
        item = await cls.get_accessible(id, user)
        if item.lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
            raise NotFoundException()
        formats = (
            (MediaFormat.TXT, MediaFormat.EPUB)
            if item.lib.lib_type == LibType.NOVEL
            else (None, MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
        )
        if item.format not in formats:
            raise ContentError("unsupported_media_format")
        # collections have no body version; validate the selected chapter instead
        if item.format is not None:
            if (
                item.index_state != IndexState.READY
                or item.index_version is None
                or re.fullmatch(r"[0-9a-f]{64}", item.index_version) is None
            ):
                raise ContentError(
                    "empty_content"
                    if item.index_state == IndexState.EMPTY
                    else "content_not_ready"
                )
            if version is not None and version != item.index_version:
                raise ContentError("content_changed")
        failure = None
        try:
            yield item
        except ContentError as error:
            failure = error
        current = await cls.get_accessible(id, user)
        if (
            user.role != UserRole.ADMIN
            and not await UserPermission.filter(
                user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=current.lib_id
            ).exists()
        ):
            raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
        if _source_identity(current) != _source_identity(item) or (
            item.format is not None
            and (
                current.index_state != IndexState.READY
                or current.index_version != item.index_version
            )
        ):
            raise ContentError("content_changed")
        if failure is not None:
            raise failure

    @classmethod
    async def _get_collection_content(
        cls, item: MediaItem, user: UserInfo, query: MediaContentQuery
    ) -> ImageContent:
        """Get selected chapter content and the available comic chapter list.

        Args:
            item: The accessible collection guarded by the caller during this read.
            user: The authenticated user used to revalidate chapter access.
            query: The optional child selection, expected child version and page range.

        Returns:
            The selected chapter's content with the collection ID and ready chapters.

        Raises:
            NotFoundException: If the selected chapter or collection becomes hidden.
            ForbiddenException: If access to the library is revoked.
            ContentError: If the selection is invalid, no chapter is ready,
                the directory changes, or the selected source cannot be read.
        """
        from app.core.media.handlers.reading import natural_key

        _resolve_source(item)
        children = MediaItem.filter(
            parent_id=item.id,
            lib_id=item.lib_id,
            visible=True,
            format__in=(MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP),
        ).order_by("id")
        fields = (
            "id",
            "dir",
            "path",
            "format",
            "title",
            "index_state",
            "index_version",
        )
        chapters = await children.values(*fields)
        ready = sorted(
            (
                chapter
                for chapter in chapters
                if chapter["index_state"] == IndexState.READY
                and re.fullmatch(r"[0-9a-f]{64}", chapter["index_version"] or "")
            ),
            key=lambda chapter: (natural_key(Path(chapter["dir"]).name), chapter["id"]),
        )
        if query.chapter_id is not None:
            selected = next(
                (
                    chapter
                    for chapter in chapters
                    if query.chapter_id == f"item:{chapter['id']}"
                ),
                None,
            )
            if selected is None:
                raise ContentError("bad_request")
        elif ready:
            selected = ready[0]
        else:
            raise ContentError(
                "empty_content"
                if all(
                    chapter["index_state"] == IndexState.EMPTY for chapter in chapters
                )
                else "content_not_ready"
            )
        async with cls._content_item(selected["id"], user, query.version) as source:
            if source.parent_id != item.id or source.lib_id != item.lib_id:
                raise ContentError("content_changed")
            content = await to_thread(
                _read_image_content, source, query.offset, query.limit, query.page_id
            )
        if await children.values(*fields) != chapters:
            raise ContentError("content_changed")
        return content.model_copy(
            update={
                "item_id": item.id,
                "chapters": [
                    ContentChapter(
                        id=f"item:{chapter['id']}",
                        title=content.title
                        if chapter["id"] == source.id
                        else chapter["title"] or Path(chapter["dir"]).name,
                        part=1,
                    )
                    for chapter in ready
                ],
            }
        )

    @classmethod
    async def get_content(
        cls, id: int, user: UserInfo, query: MediaContentQuery
    ) -> TextContent | EpubContent | ImageContent:
        """Read published content without indexing, metadata writes or history updates.

        Args:
            id: The requested reading source or comic collection ID.
            user: The authenticated user with loaded library permissions.
            query: The optional chapter, expected version and comic pagination.

        Returns:
            TXT paragraphs, EPUB blocks or comic page URLs with chapter labels.

        Raises:
            NotFoundException: If the item is unavailable or belongs to a video library.
            ForbiddenException: If library access is denied.
            ContentError: If the requested content is unavailable or changed.
        """
        async with cls._content_item(id, user, query.version) as item:
            if item.lib.lib_type == LibType.COMIC:
                if query.page_id is not None and "offset" in query.model_fields_set:
                    raise ContentError("bad_request")
                if item.format is None:
                    return await cls._get_collection_content(item, user, query)
                if query.chapter_id not in (None, f"item:{item.id}"):
                    raise ContentError("bad_request")
                return await to_thread(
                    _read_image_content, item, query.offset, query.limit, query.page_id
                )
            if query.model_fields_set & {"offset", "limit", "page_id"} or (
                query.chapter_id is not None and query.chapter_id.startswith("item:")
            ):
                raise ContentError("bad_request")
            return await to_thread(_read_text_content, item, query.chapter_id)

    @classmethod
    async def get_locator(
        cls,
        id: int,
        user: UserInfo,
        locator: TextLocator | ImageLocator,
        *,
        restore: bool = False,
    ) -> TextLocator | ImageLocator | None:
        """Resolve a reading position within its accessible work and chapter.

        Args:
            id: The top-level novel or comic work ID.
            user: The authenticated user with loaded library permissions.
            locator: The position in the expected published content version.
            restore: Recover stale positions at their chapter start; defaults to
                strict validation when recording progress.

        Returns:
            The validated or recovered locator, or None if its chapter is gone.

        Raises:
            NotFoundException: If the work or selected chapter is missing or hidden.
            ForbiddenException: If library access is denied or revoked during reading.
            ContentError: If the type, ownership, version or anchor is invalid.
        """
        version = None if restore else locator.version
        async with cls._content_item(id, user, version) as item:
            expected = (
                LibType.NOVEL if isinstance(locator, TextLocator) else LibType.COMIC
            )
            if item.parent_id is not None or item.lib.lib_type != expected:
                raise ContentError("bad_request")
            if isinstance(locator, ImageLocator) and item.format is None:
                if locator.chapter_item_id is None:
                    raise ContentError("bad_request")
                async with cls._content_item(
                    locator.chapter_item_id, user, version
                ) as chapter:
                    if (
                        chapter.parent_id != item.id
                        or chapter.lib_id != item.lib_id
                        or chapter.format is None
                    ):
                        raise ContentError("bad_request")
                    return await to_thread(_check_locator, chapter, locator, restore)
            else:
                if (
                    isinstance(locator, ImageLocator)
                    and locator.chapter_item_id is not None
                ):
                    raise ContentError("bad_request")
                return await to_thread(_check_locator, item, locator, restore)

    @classmethod
    async def get_asset(
        cls, id: int, user: UserInfo, asset_id: str, version: str
    ) -> tuple[bytes, str]:
        """Read an EPUB or comic image from its index after checking library access.

        Args:
            id: The requested source item ID.
            user: The authenticated user with loaded library permissions.
            asset_id: The opaque image ID selected from the current index.
            version: The required published content version.

        Returns:
            Verified image bytes and their MIME type; no filesystem path is returned.

        Raises:
            NotFoundException: If the item is unavailable or belongs to a video library.
            ForbiddenException: If library access is denied.
            ContentError: If the source, version or resource cannot be read safely.
        """
        async with cls._content_item(id, user, version) as item:
            if re.fullmatch(r"[0-9a-f]{32}", asset_id) is None or item.format in (
                None,
                MediaFormat.TXT,
            ):
                raise ContentError("not_found")
            return await to_thread(_read_asset, item, asset_id)

    @classmethod
    async def delete(cls, id: int, local: bool = False):
        """Delete or hide a media item under its library lock.

        Recover video organization before resolving current paths. Local deletion
        removes owned files individually. Retain the original child scope across
        parent changes and summarize surviving comic collections.

        Args:
            id: The media item ID.
            local: Whether to delete local files; False only hides the selected items.

        Raises:
            DoesNotExist: If the item is missing before a local deletion.
            OrganizePendingError: If pending organization cannot finish safely.
            ContentError: If ownership or file cleanup cannot be confirmed.
        """
        from app.core.media.organizer import recover_organizing

        items = await MediaItem.filter(Q(id=id) | Q(parent_id=id)).select_related("lib")
        item = next((row for row in items if row.id == id), None)
        if item is None:
            if local:
                raise DoesNotExist(MediaItem)
            return
        child_ids = [row.id for row in items if row.id != id]
        reading = item.lib.lib_type in (LibType.NOVEL, LibType.COMIC)
        async with library_lock(item.lib.dir):
            lib = await MediaLib.get(id=item.lib_id)
            if (lib.dir, lib.lib_type) != (item.lib.dir, item.lib.lib_type):
                raise ContentError("content_changed")
            if not reading:
                await recover_organizing(lib)
            items = await MediaItem.filter(
                id__in=[id, *child_ids], lib_id=item.lib_id
            ).select_related("lib", "parent")
            parent_ids = {row.parent_id for row in items if row.parent_id is not None}
            # a surviving parent may have received children outside the requested scope
            if await MediaItem.filter(parent_id=id).exclude(id__in=child_ids).exists():
                items = [row for row in items if row.id != id]
            if local:
                if not reading:
                    ids = {row.id for row in items}
                    for parent in await MediaItem.filter(
                        id__in=parent_ids - ids, lib_id=item.lib_id
                    ).select_related("lib", "parent"):
                        if (
                            not await MediaItem.filter(parent_id=parent.id)
                            .exclude(id__in=ids)
                            .exists()
                        ):
                            items.append(parent)
                await cls._delete_local(items)
            else:
                await MediaItem.filter(id__in=[row.id for row in items]).update(
                    visible=False
                )
                for parent_id in parent_ids:
                    if not await MediaItem.filter(
                        parent_id=parent_id, visible=True
                    ).exists():
                        await MediaItem.filter(id=parent_id, lib_id=item.lib_id).update(
                            visible=False
                        )
        if reading:
            for parent_id in parent_ids:
                with suppress(DoesNotExist):
                    await cls.sync_collection(parent_id)

    @classmethod
    async def _delete_local(cls, items: list[MediaItem]):
        """Delete captured media items while the caller holds the library lock.

        Args:
            items: Current rows from the originally requested scope, with library and
                parent relations loaded; an empty scope is already complete.

        Raises:
            ContentError: If ownership, sources or filesystem cleanup change or fail.
            asyncio.CancelledError: After active filesystem writers stop.
        """
        if not items:
            return
        reading = items[0].lib.lib_type in (LibType.NOVEL, LibType.COMIC)
        ids = {item.id for item in items}
        root = Path(items[0].lib.dir)
        directories = {Path(item.dir) for item in items} - {root}
        others = (
            await MediaItem.filter(lib_id=items[0].lib_id)
            .exclude(id__in=ids)
            .only("dir", "path", "nfo_path")
        )
        if await MediaItem.filter(parent_id__in=ids).exclude(id__in=ids).exists() or (
            reading
            and any(
                any(
                    Path(other.dir).is_relative_to(directory)
                    for directory in directories
                )
                for other in others
            )
        ):
            raise ContentError("content_changed")
        states, removed = await write_in_thread(_delete_media_files, items, others)
        if reading:
            await write_in_thread(_remove_reading_caches, list(ids))
        async with in_transaction():
            current = await MediaItem.filter(
                Q(id__in=ids) | Q(parent_id__in=ids)
            ).select_related("lib", "parent")
            if {row.id: _source_identity(row) for row in current} != {
                row.id: _source_identity(row) for row in items
            }:
                raise ContentError("content_changed")
            await to_thread(_check_media_deleted, items, states, others)
            await _remove_media_records(items)
        directories.update(path.parent for path in removed if path.parent != root)
        for directory in sorted(
            directories, key=lambda path: len(path.parts), reverse=True
        ):
            await write_in_thread(
                _prune_media_directories,
                directory,
                removed,
                {
                    path: state
                    for path, state in states.items()
                    if directory.is_relative_to(path)
                },
            )

    @classmethod
    async def create(
        cls,
        lib_id: int,
        *,
        path_info: MediaPathInfo,
        parent_id: int | None = None,
        default_title: str | None = None,
    ) -> MediaItem:
        """Get or create a media item.

        Args:
            lib_id: The media library ID.
            path_info: The media path info whose item ID is populated.
            parent_id: The parent media item ID, if any.
            default_title: The default title to use if the media item is created.

        Returns:
            The media item instance.
        """
        item_path = path_info.item_path
        item, created = await MediaItem.get_or_create(
            lib_id=lib_id,
            path=item_path,
            defaults={
                "parent_id": parent_id,
                "dir": path_info.item_dir,
                "name": path_info.item_name,
                "title": default_title,
                "year": path_info.year,
                "season": path_info.season,
                "episode": path_info.episode,
                "visible": True,
            },
        )
        path_info.item_id = item.id

        # calculate hash and size for the newly created item
        if created:
            item_id = item.id

            # fill missing file information under the library lock
            async def refresh():
                target = await MediaItem.get_or_none(id=item_id).select_related("lib")
                if target is None:
                    return
                async with library_lock(target.lib.dir):
                    current = await MediaItem.get_or_none(id=item_id)
                    if current is not None and (
                        current.hash is None or current.size is None
                    ):
                        await cls.refresh_hash_and_size(current)

            create_task(refresh())

        return item

    @classmethod
    async def refresh_hash_and_size(cls, item: MediaItem) -> bool:
        """Refresh a media file's hash and size while holding its library lock.

        Args:
            item: The current media item whose library lock the caller holds.

        Returns:
            `True` if a known hash or size changed. `False` if values only needed
            backfilling, are unchanged, or the file is missing or not a regular file.
        """
        if not Path(item.path).is_file():
            return False
        md5 = hashlib.md5()
        try:
            async with aiofiles.open(item.path, "rb") as f:
                md5.update(await f.read(cls.HASH_READ_SIZE))
            size = Path(item.path).stat().st_size
        except FileNotFoundError:
            return False
        digest = md5.hexdigest()
        changed = (item.hash is not None and item.hash != digest) or (
            item.size is not None and item.size != size
        )
        await MediaItem.filter(id=item.id).update(hash=digest, size=size)
        item.hash, item.size = digest, size
        return changed

    @classmethod
    async def resolve_media_hash(cls, item_path: str) -> str:
        """Look up the media file's hash from the database.

        Args:
            item_path: The file path of the media item.

        Returns:
            The media hash if found, otherwise calculate and return the hash.
        """
        try:
            media = await MediaItem.filter(path=item_path).first()
            if media and media.hash:
                return media.hash
        except Exception:
            logger.debug(
                "Failed to look up media hash for '%s'", item_path, exc_info=True
            )

        # fallback to calculating the hash if not found in the database
        path = Path(item_path)
        if not path.is_file():
            raise KaloscopeException(ErrorCode.FILE_NOT_EXISTS)
        md5 = hashlib.md5()
        async with aiofiles.open(path, "rb") as f:
            md5.update(await f.read(cls.HASH_READ_SIZE))
        return md5.hexdigest()

    @classmethod
    async def refresh_episodes(
        cls,
        item: MediaItem,
        meta: MediaMetadata,
        *,
        episode_ids: list[int] | None = None,
    ):
        """Refresh the metadata of the episodes under a season.

        Args:
            item: The season media item.
            meta: The season metadata object.
            episode_ids: The original episode IDs, or `None` to use current children.
        """
        from app.core.media.shelver import gen_nfo, get_nfo_path

        metadata = meta.metadata
        series_id = metadata.get("id")
        nfo_source = metadata.get("site")
        season = metadata.get("season", item.season)
        title = metadata.get("title", item.title)
        year = metadata.get("year", item.year)

        # check if the series_id is the same as the current one
        same_series = series_id and str(series_id) == str(item.unique_id)

        # get the flow engine from the app context
        engine = Sanic.get_app().ctx.flow_engine

        # get the episodes under the season
        episodes = await MediaItem.filter(
            Q(id__in=episode_ids) if episode_ids is not None else Q(parent_id=item.id),
            lib_id=item.lib_id,
        )
        for e in episodes:
            episode = e.episode
            nfo_path = e.nfo_path

            # skip if the season is the same and the NFO file already exists
            if same_series and nfo_path and Path(nfo_path).exists():
                same_season = e.season == season
                if same_season:
                    continue

            # execute the flow to get the metadata for the episode
            results = await engine.execute(
                graph_id=meta.graph_id,
                bootparams={
                    "$manual": True,
                    "series_id": series_id,
                    "nfo_source": nfo_source,
                    "item_path": e.path,
                    "item_name": e.name,
                    "nfo_type": NFOType.EPISODE,
                    "language": item.lib.language,
                    "title": title,
                    "year": year,
                    "season": season,
                    "episode": episode,
                    "page_num": 1,
                    "page_size": 1,
                },
            )

            # generate the NFO file for the episode
            if isinstance(results, list) and len(results) > 0:
                result = results[0]
                if isinstance(result, dict):
                    result["season"] = _s if (_s := result.get("season")) else season
                    result["episode"] = _e if (_e := result.get("episode")) else episode
                    nfo_path = nfo_path or get_nfo_path(e.path)
                    await gen_nfo(
                        NFOType.EPISODE, nfo_path, result, overwrite=True, item_id=e.id
                    )
