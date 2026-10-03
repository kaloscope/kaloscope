from __future__ import annotations

import hashlib
import secrets
import shutil
import stat
from asyncio import create_task, to_thread
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
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
from app.core.media.coordination import library_lock, write_in_thread
from app.core.media.metadata import ReadingMetadata
from app.core.media.naming import validate_template
from app.models.flow import FlowTrigger, GraphCategory
from app.models.media import (
    IndexState,
    LibType,
    MediaFormat,
    MediaItem,
    MediaLib,
    MediaLibUpsert,
    MediaMetadata,
    NFOType,
    ReadingMetadataSync,
)
from app.models.user import PermType, UserInfo, UserPermission, UserRole
from app.services.base import BaseService
from app.services.flow import FlowTriggerService
from app.utils.disk import delete_path, rename_exclusive

if TYPE_CHECKING:
    from app.core.media.cover import CoverImage
    from app.core.media.epub.cache import EpubIndex
    from app.core.media.handlers.base import MediaPathInfo
    from app.core.media.handlers.reading import ReadingSource
    from app.core.media.image import ImageIndex
    from app.core.media.metadata_reader import MetadataRead
    from app.core.media.text import TextIndex

type _SourceStates = dict[Path, tuple[int, ...]]


def _reading_identity(item: MediaItem) -> tuple:
    """Identify the current database ownership used by a reading request.

    Args:
        item: An accessible item with its library and optional parent loaded.

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
    from app.core.media.handlers.reading import (
        ReadingMediaHandler,
        ReadingSource,
        is_ignored_name,
    )

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
            lib_type = item.lib.lib_type
            if lib_type not in (LibType.NOVEL, LibType.COMIC):
                raise ContentError("unsupported_media_format")
            handler = get_handler(lib_type)
            if not isinstance(handler, ReadingMediaHandler):
                raise ContentError("unsupported_media_format")
            scan = handler.scan_sources(str(root), work_path=root / parts[0])
            for scope, error in scan.issues.items():
                if source.directory.is_relative_to(scope):
                    raise ContentError(error)
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


def _validate_reading_source(item: MediaItem, *, require_candidate: bool):
    """Check source ownership and discovery in a worker without reading its body.

    Args:
        item: The proposed reading item with its library and optional parent loaded.
        require_candidate: Whether discovery must still find this new source.

    Raises:
        ContentError: If source ownership, discovery or stability is invalid.
    """
    with _reading_source(item, require_candidate=require_candidate):
        pass


def _read_reading(
    item: MediaItem, *, with_cover: bool
) -> tuple[MetadataRead, CoverImage | None, str | None]:
    """Read current metadata and optional cover bytes in a worker thread.

    Args:
        item: The permission-checked item with its library and parent loaded.
        with_cover: Whether to read the selected cover bytes after metadata.

    Returns:
        Fresh metadata, optional cover bytes and an independent missing-source error.

    Raises:
        ContentError: If source ownership, file reading or stability is invalid.
    """
    from app.core.media.cover import read_cover
    from app.core.media.metadata_reader import read_metadata

    with _reading_source(item) as (source, _, missing):
        metadata = read_metadata(source)
        if with_cover and missing:
            raise ContentError("media_source_unavailable")
        cover = read_cover(source, metadata) if with_cover else None
        return metadata, cover, "media_source_unavailable" if missing else None


def _build_content(
    item: MediaItem, staging: Path
) -> tuple[TextIndex | EpubIndex | ImageIndex, _SourceStates]:
    """Build a private content cache while retaining publication stability checks.

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


def _publish_content(
    item: MediaItem, staging: Path, version: str, states: _SourceStates
):
    """Publish a completed cache after revalidating the source under the library lock.

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

        if obj.id:
            lib = await MediaLib.get(id=obj.id)
            extra = {}
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
            await MediaLib.filter(id=obj.id).update(
                name=obj.name,
                language=obj.language or None,
                danmaku_server=obj.danmaku_server,
                danmaku_ttl=obj.danmaku_ttl,
                **extra,
            )
            lib = await MediaLib.get(id=obj.id)
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
                danmaku_ttl=obj.danmaku_ttl,
                rename_template=obj.rename_template,
                priority=(max(priorities) + 1 if priorities else 1),
            )
            # add the observer
            watcher = cls.app_ctx().lib_watcher
            await watcher.add_observer(lib)

        # bind the flow triggers to the media library
        await FlowTriggerService.bind_triggers(
            GraphCategory.INGEST, lib.id, obj.triggers
        )

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
    async def create_reading(cls, lib_id: int, source: ReadingSource) -> MediaItem:
        """Get or register a discovered reading source without parsing its content.

        Register comic collections before their chapters. Validate current paths
        under the library lock, then insert only the missing pending item. Existing
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
            if current is not None and _reading_identity(current) != _reading_identity(
                candidate
            ):
                raise ContentError("ambiguous_layout")
            await to_thread(
                _validate_reading_source, candidate, require_candidate=current is None
            )
            if current is not None:
                return current
            await candidate.save(force_create=True)
            return candidate

    @classmethod
    async def sync_metadata(cls, id: int) -> MediaItem:
        """Synchronize reading list summaries without rebuilding content or writing XML.

        The library's single event consumer must await synchronization serially.
        Read files outside the library lock, then recheck ownership before saving.
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
        metadata, _, source_error = await to_thread(
            _read_reading, item, with_cover=False
        )
        external = metadata.external
        error = source_error or next(
            (
                origin.error
                for origin in (external, metadata.embedded)
                if origin is not None and origin.error is not None
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
        async with library_lock(item.lib.dir):
            current = await MediaItem.get_or_none(id=id).select_related("lib", "parent")
            if current is None or _reading_identity(item) != _reading_identity(current):
                raise ContentError("content_changed")
            fields = ["extra"]
            if not error:
                for name, value in metadata.summary().items():
                    setattr(current, name, value)
                    fields.append(name)
                current.poster = f"/_api/media/{id}/assets/cover"
                fields.append("poster")
            current.extra = {
                **(current.extra or {}),
                "schema_version": 1,
                "metadata_sync": sync.model_dump(),
            }
            await current.save(update_fields=fields)
            return current

    @classmethod
    async def index_content(cls, id: int) -> MediaItem:
        """Rebuild and publish content for one persisted reading unit.

        The library's single event consumer must await builds serially and decide
        when rebuilding is required. Scan, watch and retry paths must enqueue work
        for that consumer. Collections derive their state from children and cannot
        be indexed as a body. Cache directories publish before the database pointer;
        old and orphaned versions are retained for separate cleanup. Cancellation
        leaves pending work safe to retry.

        Args:
            id: The internal reading item ID selected by the library consumer.

        Returns:
            The item with a ready version, source size and bounded content counts.

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
            index, states = await write_in_thread(_build_content, item, staging)
            async with library_lock(directory):
                current = await MediaItem.get_or_none(id=id).select_related(
                    "lib", "parent"
                )
                if current is None or _reading_identity(item) != _reading_identity(
                    current
                ):
                    raise ContentError("content_changed")
                await write_in_thread(
                    _publish_content, current, staging, index.index_version, states
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
                return current
        except ContentError as error:
            async with library_lock(directory):
                current = await MediaItem.get_or_none(id=id).select_related(
                    "lib", "parent"
                )
                if current is not None and _reading_identity(item) == _reading_identity(
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
    async def _read_current(
        cls, item: MediaItem, user: UserInfo, *, with_cover: bool
    ) -> tuple[MediaItem, MetadataRead, CoverImage | None, str | None]:
        """Read current metadata with one retry for source or ownership changes.

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
                    _read_reading, item, with_cover=with_cover
                )
            except ContentError as error:
                if error.code != "content_changed" or attempt:
                    raise
            else:
                current = await cls.get_accessible(item.id, user)
                if _reading_identity(current) == _reading_identity(item):
                    return current, metadata, cover, source_error
            item = await cls.get_accessible(item.id, user)
        raise ContentError("content_changed")

    @classmethod
    async def get_details(cls, id: int, user: UserInfo) -> dict[str, Any]:
        """Build accessible details, reading current OPF or ComicInfo for reading media.

        Args:
            id: The media item ID.
            user: The authenticated user with loaded library permissions.

        Returns:
            Details with current reading metadata and controlled source issues.

        Raises:
            NotFoundException: If the item or its parent is unavailable.
            ForbiddenException: If library access is denied.
        """
        item = await cls.get_accessible(id, user)
        reading = item.lib.lib_type in (LibType.NOVEL, LibType.COMIC)
        metadata = None
        source_error = None
        if reading:
            try:
                item, metadata, _, source_error = await cls._read_current(
                    item, user, with_cover=False
                )
            except ContentError as error:
                source_error = error.code
                item = await cls.get_accessible(id, user)
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
            return data
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
        _, _, cover, _ = await cls._read_current(item, user, with_cover=True)
        return cover

    @classmethod
    async def delete(cls, id: int, local: bool = False):
        """Delete or hide a media item under its library lock.

        Recover pending organization before resolving current paths. Retain original
        children across parent changes and clean up empty parents.

        Args:
            id: The media item ID.
            local: Whether to delete the local files.

        Raises:
            DoesNotExist: If the item is missing before a local deletion.
            OrganizePendingError: If pending organization cannot finish safely.
        """
        from app.core.media.organizer import recover_organizing

        items = await MediaItem.filter(Q(id=id) | Q(parent_id=id)).select_related("lib")
        item = next((row for row in items if row.id == id), None)
        if item is None:
            if local:
                raise DoesNotExist(MediaItem)
            return
        child_ids = [row.id for row in items if row.id != id]
        async with library_lock(item.lib.dir):
            await recover_organizing(item.lib)
            items = await MediaItem.filter(id__in=[id, *child_ids], lib_id=item.lib_id)
            parent_ids = {row.parent_id for row in items if row.parent_id is not None}
            # a surviving parent may have received children outside the requested scope
            if await MediaItem.filter(parent_id=id).exclude(id__in=child_ids).exists():
                items = [row for row in items if row.id != id]
            if local:
                if any(row.id == id for row in items):
                    items = [row for row in items if row.parent_id != id]
                for current in items:
                    path = Path(current.path)
                    if path.exists():
                        delete_path(path)
                    await current.delete()
                for parent_id in parent_ids:
                    if not await MediaItem.filter(parent_id=parent_id).exists():
                        await MediaItem.filter(
                            id=parent_id, lib_id=item.lib_id
                        ).delete()
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
