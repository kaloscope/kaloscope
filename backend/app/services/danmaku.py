import contextlib
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode

import aiofiles
import httpx
from pydantic import BaseModel, field_validator
from sanic import Sanic
from sanic.log import logger

from app.core.constants import ENCODING
from app.models.media import Language, LibType, MediaItem, MediaResource
from app.utils import json

# the display mode of the danmaku
type Mode = Literal["scroll", "top", "bottom"]


class Danmaku(BaseModel):
    """The data model for a single danmaku."""

    # unique id
    id: str | None = None
    # comment text
    text: str
    # display mode
    mode: Mode | None = None
    # color in hex format
    color: str | None = None
    # start time in milliseconds
    start: int | None = None


class DanmakuMeta(BaseModel):
    """The metadata for a danmaku collection."""

    anime_id: str
    anime_title: str | None = None
    episode_id: str
    episode_title: str | None = None
    type: str
    type_description: str | None = None

    @field_validator("anime_id", "episode_id", mode="before")
    @classmethod
    def normalize_id(cls, value: object) -> str:
        return "" if value is None else str(value)


class DanmakuWrapper(BaseModel):
    """The wrapper for danmakus with additional metadata."""

    metadata: DanmakuMeta | None = None
    comments: list[Danmaku]


class EpisodeQuery(MediaResource):
    """The query model for searching episodes from the danmaku server."""

    title: str


class EpisodeConfirm(MediaResource):
    """The model for confirming the danmaku match for a media resource."""

    metadata: DanmakuMeta


class DanmakuService:
    """The service class for all danmaku related operations."""

    @classmethod
    def _base_url(cls, server: str) -> str:
        """Get the base URL for the danmaku server API.

        Args:
            server: The danmaku server base URL.

        Returns:
            The base URL for the danmaku server API.
        """
        base_url = server.rstrip("/")
        if base_url.endswith(("/v2", "/api/v2")):
            return base_url
        return f"{base_url}/api/v2"

    @classmethod
    def _append_query(cls, url: str, params: dict) -> str:
        """Append query parameters to a URL.

        Args:
            url: The URL to append to.
            params: The query parameters to append.

        Returns:
            The URL with the query parameters appended.
        """
        if "?" in url:
            # proxy mode: encode the entire query string as a single value
            return f"{url}{quote('?' + urlencode(params))}"
        return f"{url}?{urlencode(params)}"

    @classmethod
    def _cache_path(cls, media: MediaItem) -> Path:
        """Get the local danmaku cache path for a media item.

        Args:
            media: The media item instance.

        Returns:
            The local danmaku cache path.
        """
        if media.danmaku_path:
            return Path(media.danmaku_path)

        # default path: {media_dir}/.{media_name}.json
        return Path(media.dir) / f".{media.name}.json"

    @classmethod
    async def match_danmakus(cls, path: str) -> DanmakuWrapper:
        """Match danmakus for the given media resource.

        Args:
            path: The media resource path.

        Returns:
            The wrapped danmakus with metadata if available.
        """
        # get the media item by the path
        media = await MediaItem.filter(path=path).first().select_related("lib")
        if not media:
            return DanmakuWrapper(comments=[])

        # check if the local cache file exists
        danmaku_path = cls._cache_path(media)
        cached = danmaku_path.exists()

        # check if the local cache file is expired
        expired = False
        if cached and (ttl := media.lib.danmaku_ttl) is not None:
            mtime = danmaku_path.stat().st_mtime
            if mtime + ttl * 3600 < datetime.now().timestamp():
                expired = True

        if (not cached or expired) and (server := media.lib.danmaku_server):
            meta = media.danmaku_meta
            if not meta:
                # try to match the metadata from the danmaku server
                meta = await cls.match_metadata(server, media)

            if meta:
                # load danmakus from the danmaku server
                meta = DanmakuMeta.model_validate(meta)
                danmakus = await cls.load_from_server(
                    server, meta.episode_id, media.lib.language
                )
                if danmakus:
                    # save to local cache file
                    async with aiofiles.open(danmaku_path, "wb") as f:
                        await f.write(json.dumps([d.model_dump() for d in danmakus]))

                    # update the media item with the danmaku info
                    await MediaItem.filter(id=media.id).update(
                        danmaku_meta=meta,
                        danmaku_path=str(danmaku_path),
                    )

                    return DanmakuWrapper(metadata=meta, comments=danmakus)

        # load danmakus from the local cache file
        meta = media.danmaku_meta
        return DanmakuWrapper(
            metadata=DanmakuMeta.model_validate(meta) if meta else None,
            comments=await cls.load_from_cache(danmaku_path),
        )

    @classmethod
    async def match_metadata(cls, server: str, media: MediaItem) -> DanmakuMeta | None:
        """Match the metadata for the given media item from the danmaku server.

        Args:
            server: The danmaku server base URL.
            media: The media item instance.

        Returns:
            The matched metadata, or `None` if not found.
        """
        client: httpx.AsyncClient = Sanic.get_app().ctx.httpx
        try:
            url = f"{cls._base_url(server)}/match"
            response = await client.post(
                url,
                json={
                    "fileName": media.name,
                    "fileHash": media.hash,
                    "fileSize": media.size,
                },
            )
            if response.status_code != 200:
                logger.error(
                    'Failed to match metadata for media "%s": HTTP %s',
                    media.name,
                    response.status_code,
                )
                return None

            data = response.json()
            if not data.get("success"):
                logger.error(
                    'Failed to match metadata for media "%s": %s',
                    media.name,
                    data.get("errorMessage"),
                )
                return None

            matches = data.get("matches")
            if (
                matches
                and isinstance(matches, list)
                and isinstance((m := matches[0]), dict)
            ):
                meta = DanmakuMeta(
                    anime_id=m.get("animeId", 0),
                    anime_title=m.get("animeTitle"),
                    episode_id=m.get("episodeId", 0),
                    episode_title=m.get("episodeTitle"),
                    type=m.get("type", ""),
                    type_description=m.get("typeDescription"),
                )
                logger.info(
                    'Matched episode ID "%s" for media "%s" from: %s',
                    meta.episode_id,
                    media.name,
                    server,
                )
                return meta
        except httpx.RequestError:
            logger.error("An error occurred while requesting %s.", url, exc_info=True)

        return None

    @classmethod
    async def load_from_cache(cls, path: Path) -> list[Danmaku]:
        """Load danmakus from the local cache file.

        Args:
            path: The local cache file path.

        Returns:
            A list of danmakus loaded from the cache.
        """
        if not path.exists():
            return []

        async with aiofiles.open(path, encoding=ENCODING) as f:
            danmakus = json.loads(await f.read())
        return [Danmaku.model_validate(danmaku) for danmaku in danmakus]

    @classmethod
    async def load_from_server(
        cls, server: str, episode_id: str, language: Language | None = None
    ) -> list[Danmaku]:
        """Load danmakus from the danmaku server.

        Args:
            server: The danmaku server base URL.
            episode_id: The episode ID.
            language: The optional language code.

        Returns:
            A list of danmakus loaded from the server.
        """
        client: httpx.AsyncClient = Sanic.get_app().ctx.httpx
        try:
            url = f"{cls._base_url(server)}/comment/{episode_id}"
            params = {"withRelated": "true"}
            if language == Language.ZH_CN:
                # request the converted simplified Chinese comments
                params["chConvert"] = "1"

            response = await client.get(cls._append_query(url, params))
            if response.status_code != 200:
                logger.error(
                    'Failed to load danmakus for episode ID "%s": HTTP %s',
                    episode_id,
                    response.status_code,
                )
                return []

            data = response.json()
            comments = data.get("comments")
            if comments and isinstance(comments, list):
                return cls.format_danmakus(comments)
        except httpx.RequestError:
            logger.error("An error occurred while requesting %s.", url, exc_info=True)

        return []

    @classmethod
    def format_danmakus(cls, raw: list[dict]) -> list[Danmaku]:
        """Format raw danmaku dicts into Danmaku objects.

        Each dict is expected to have:
          - `cid`: the unique danmaku ID.
          - `p`: a comma-separated string with four fields:
              - `parts[0]` — start time in seconds.
              - `parts[1]` — display mode (1=scroll, 4=bottom, 5=top).
              - `parts[2]` — color as a 32-bit integer.
              - `parts[3]` — user ID (numeric string, ignored here).
          - `m`: the comment text.

        Args:
            raw: List of raw danmaku dicts.

        Returns:
            A list of parsed Danmaku objects.
        """
        _MODE_MAP: dict[str, Mode] = {"1": "scroll", "4": "bottom", "5": "top"}

        danmakus: list[Danmaku] = []
        for item in raw:
            text = item.get("m")
            if not isinstance(text, str):
                continue

            cid = item.get("cid")
            parts = str(item.get("p", "")).split(",")

            # parts[0]: start time in seconds, converted to milliseconds
            start: int | None = None
            if len(parts) >= 1:
                with contextlib.suppress(ValueError):
                    start = int(float(parts[0]) * 1000)

            # parts[1]: display mode, mapped to Mode enum
            mode: Mode | None = None
            if len(parts) >= 2:
                mode = _MODE_MAP.get(parts[1])

            # parts[2]: color integer, converted to hex color string
            color: str | None = None
            if len(parts) >= 3:
                with contextlib.suppress(ValueError):
                    c = int(parts[2])
                    r = (c >> 16) & 0xFF
                    g = (c >> 8) & 0xFF
                    b = c & 0xFF
                    color = f"#{r:02X}{g:02X}{b:02X}"

            danmakus.append(
                Danmaku(
                    id=str(cid) if cid else None,
                    text=text,
                    mode=mode,
                    color=color,
                    start=start,
                )
            )

        return danmakus

    @classmethod
    async def delete_danmakus(cls, path: str):
        """Delete the locally cached danmakus for the given media resource.

        Args:
            path: The media resource path.
        """
        # get the media item by the path
        media = await MediaItem.filter(path=path).first()
        if not media:
            return
        danmaku_path = cls._cache_path(media)
        if danmaku_path.is_file():
            danmaku_path.unlink()
        await MediaItem.filter(id=media.id).update(danmaku_path=None)

    @classmethod
    async def search_episodes(cls, path: str, title: str) -> list[DanmakuMeta]:
        """Search for episodes matching the given title from the danmaku server.

        Args:
            path: The media resource path.
            title: The search title.

        Returns:
            A flat list of DanmakuMeta items from the search results.
        """
        media = await MediaItem.filter(path=path).first().select_related("lib")
        if not media or not (server := media.lib.danmaku_server):
            return []

        # determine episode filter based on lib type
        episode: str | None = None
        if media.lib.lib_type == LibType.MOVIE:
            episode = "movie"
        elif media.episode is not None:
            episode = str(media.episode)

        client: httpx.AsyncClient = Sanic.get_app().ctx.httpx
        try:
            url = f"{cls._base_url(server)}/search/episodes"
            params = {"anime": title}
            if episode:
                params["episode"] = episode

            response = await client.get(cls._append_query(url, params))
            if response.status_code != 200:
                logger.error(
                    'Failed to search episodes for "%s": HTTP %s',
                    title,
                    response.status_code,
                )
                return []

            data = response.json()
            if not data.get("success"):
                logger.error(
                    'Failed to search episodes for "%s": %s',
                    title,
                    data.get("errorMessage"),
                )
                return []

            results: list[DanmakuMeta] = []
            for a in data.get("animes", []):
                for e in a.get("episodes", []):
                    results.append(
                        DanmakuMeta(
                            anime_id=a.get("animeId", 0),
                            anime_title=a.get("animeTitle"),
                            episode_id=e.get("episodeId", 0),
                            episode_title=e.get("episodeTitle"),
                            type=a.get("type", ""),
                            type_description=a.get("typeDescription"),
                        )
                    )
            return results
        except httpx.RequestError:
            logger.error("An error occurred while requesting %s.", url, exc_info=True)

        return []

    @classmethod
    async def confirm_episode(cls, path: str, meta: DanmakuMeta) -> DanmakuWrapper:
        """Confirm the episode match result for the given media resource.

        Args:
            path: The media resource path.
            meta: The confirmed metadata.

        Returns:
            The wrapped danmakus with the confirmed metadata.
        """
        result = DanmakuWrapper(metadata=meta, comments=[])
        media = await MediaItem.filter(path=path).first().select_related("lib")
        if not media or not (server := media.lib.danmaku_server):
            return result

        # load danmakus from the danmaku server
        danmakus = await cls.load_from_server(
            server, meta.episode_id, media.lib.language
        )
        if danmakus:
            result.comments = danmakus
            # save to local cache file
            danmaku_path = cls._cache_path(media)
            async with aiofiles.open(danmaku_path, "wb") as f:
                await f.write(json.dumps([d.model_dump() for d in danmakus]))

            # update the media item with the danmaku info
            await MediaItem.filter(id=media.id).update(
                danmaku_meta=meta,
                danmaku_path=str(danmaku_path),
            )

        # also refresh the danmaku metadata of sibling episodes if it's a TV show
        if media.lib.lib_type == LibType.TV_SHOW:
            await cls.refresh_episodes(media, meta)

        return result

    @classmethod
    async def refresh_episodes(cls, item: MediaItem, meta: DanmakuMeta):
        """Refresh the danmaku metadata of the episodes under an anime.

        Args:
            item: The episode media item.
            meta: The confirmed danmaku metadata.
        """
        if not item.parent_id or not (server := item.lib.danmaku_server):
            return

        anime_id = meta.anime_id
        if item.danmaku_meta and str(item.danmaku_meta.get("anime_id")) == anime_id:
            # skip if the anime ID hasn't changed
            return

        # get sibling episodes under the same parent item
        db_episodes = await MediaItem.filter(
            parent_id=item.parent_id, id__not=item.id, episode__not_isnull=True
        ).all()
        if not db_episodes:
            return

        # get bangumi info from the danmaku server to find the corresponding episode IDs
        client: httpx.AsyncClient = Sanic.get_app().ctx.httpx
        try:
            url = f"{cls._base_url(server)}/bangumi/{anime_id}"
            response = await client.get(url)
            if response.status_code != 200:
                logger.error(
                    'Failed to get bangumi info for anime ID "%s": HTTP %s',
                    anime_id,
                    response.status_code,
                )
                return

            data = response.json()
            if not data.get("success"):
                logger.error(
                    'Failed to get bangumi info for anime ID "%s": %s',
                    anime_id,
                    data.get("errorMessage"),
                )
                return

            api_episodes = data.get("bangumi", {}).get("episodes", [])
            if not api_episodes:
                return

            # build a map from episodeNumber -> episode data
            ep_data: dict[str, dict] = {
                str(num): e
                for e in api_episodes
                if (num := e.get("episodeNumber")) is not None
            }

            # match each sibling episode by its episode number and update danmaku_meta
            for db_episode in db_episodes:
                ep = ep_data.get(str(db_episode.episode))
                if not ep:
                    continue

                # delete the old cached danmaku file if exists
                if db_episode.danmaku_path:
                    danmaku_path = Path(db_episode.danmaku_path)
                    if danmaku_path.is_file():
                        danmaku_path.unlink()

                # update the danmaku_meta with the new episode ID and title
                danmaku_meta = DanmakuMeta(
                    anime_id=meta.anime_id,
                    anime_title=meta.anime_title,
                    episode_id=ep.get("episodeId", 0),
                    episode_title=ep.get("episodeTitle"),
                    type=meta.type,
                    type_description=meta.type_description,
                )

                await MediaItem.filter(id=db_episode.id).update(
                    danmaku_path=None, danmaku_meta=danmaku_meta
                )

        except httpx.RequestError:
            logger.error("An error occurred while requesting %s.", url, exc_info=True)
