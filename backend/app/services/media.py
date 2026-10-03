from __future__ import annotations

import hashlib
import stat
from asyncio import create_task, to_thread
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiofiles
from sanic import Sanic
from sanic.log import logger
from tortoise.exceptions import DoesNotExist
from tortoise.expressions import Q
from tortoise.transactions import atomic, in_transaction

from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.media.common import ContentError, file_state
from app.core.media.coordination import library_lock
from app.core.media.metadata import ReadingMetadata
from app.core.media.naming import validate_template
from app.models.flow import FlowTrigger, GraphCategory
from app.models.media import (
    LibType,
    MediaFormat,
    MediaItem,
    MediaLib,
    MediaLibUpsert,
    MediaMetadata,
    NFOType,
)
from app.models.user import PermType, UserInfo, UserPermission, UserRole
from app.services.base import BaseService
from app.services.flow import FlowTriggerService
from app.utils.disk import delete_path

if TYPE_CHECKING:
    from app.core.media.cover import CoverImage
    from app.core.media.handlers.base import MediaPathInfo
    from app.core.media.metadata_reader import MetadataRead


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
        parent.path if parent else None,
        parent.format if parent else None,
        parent.parent_id if parent else None,
    )


def _read_reading(
    item: MediaItem, *, with_cover: bool
) -> tuple[MetadataRead, CoverImage | None, str | None]:
    """Validate current reading ownership and read files in a worker thread.

    Args:
        item: The permission-checked item with its library and parent loaded.
        with_cover: Whether to read the selected cover bytes after metadata.

    Returns:
        Fresh metadata, optional cover bytes and an independent missing-source error.

    Raises:
        ContentError: If paths, layout, file access or source stability are invalid.
    """
    # handler registration imports this service through the video handlers
    from app.core.media.cover import read_cover
    from app.core.media.handlers.base import get_handler
    from app.core.media.handlers.reading import (
        ReadingMediaHandler,
        ReadingSource,
        is_ignored_name,
    )
    from app.core.media.metadata_reader import read_metadata

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
            elif source.format not in (None, MediaFormat.DIR) and not source_missing:
                raise ContentError("unsupported_layout")
            # indexed empty image directories and collections can retain their metadata
            metadata = read_metadata(source)
            if with_cover and source_missing:
                raise ContentError("media_source_unavailable")
            cover = read_cover(source, metadata) if with_cover else None
            return (
                metadata,
                cover,
                "media_source_unavailable" if source_missing else None,
            )
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
