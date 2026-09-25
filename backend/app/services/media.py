import hashlib
from asyncio import create_task
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles
from sanic import Sanic
from sanic.log import logger
from tortoise.expressions import Q
from tortoise.transactions import atomic, in_transaction

from app.core.exceptions import BadRequestException, ErrorCode, KaloscopeException
from app.core.media.coordination import library_lock
from app.models.flow import FlowTrigger, GraphCategory
from app.models.media import MediaItem, MediaLib, MediaLibUpsert, MediaMetadata, NFOType
from app.models.user import PermType, UserPermission
from app.services.base import BaseService
from app.services.flow import FlowTriggerService
from app.utils.disk import delete_path

if TYPE_CHECKING:
    from app.core.media.handlers.base import MediaPathInfo


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
            from app.core.media.naming import validate_template

            lib = await MediaLib.get(id=obj.id)
            extra = {}
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
    async def delete(cls, id: int, local: bool = False):
        """Delete a media item.

        Args:
            id: The media item ID.
            local: Whether to delete the local files.
        """
        if local:
            item = await MediaItem.get(id=id)
            path = Path(item.path)
            if path.exists():
                delete_path(path)
            await item.delete()
        else:
            await MediaItem.filter(id=id).update(visible=False)

    @classmethod
    async def create(
        cls,
        lib_id: int,
        *,
        path_info: "MediaPathInfo",
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
            `True` if a previously known hash or size changed, or `False` if only
            missing values were filled, both values are unchanged, or the file
            is unavailable.
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
            episode_ids: The episode IDs to refresh.
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
