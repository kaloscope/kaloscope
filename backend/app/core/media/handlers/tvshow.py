from pathlib import Path

from lxml import etree

from app.core.media.handlers.base import (
    _HANDLERS,
    Actor,
    MediaHandler,
    MediaMeta,
    MediaPathInfo,
)
from app.models.media import LibType, MediaItem, MediaLib, NFOType
from app.services.media import MediaItemService
from app.utils.extractor import (
    extract_episode,
    extract_season,
    extract_title,
    extract_year,
)
from app.utils.xml import get_all_text, get_decimal, get_element, get_integer, get_text


class TVShowMediaHandler(MediaHandler):
    """The media item handler for TV show library type."""

    def accept(self) -> list[str]:
        """The mime types accepted by the TV show library type.

        Returns:
            The list of mime types.
        """
        return ["video/*"]

    def hierarchies(self) -> list[int]:
        """The hierarchy levels for the TV show library type.

        Shows
        ├── Series Name A (2010)
        │   ├── Series Name A S01E01.mkv
        │   ├── Series Name A S01E02.mkv
        │   └── Season 02
        │       ├── Series Name A S02E01.mkv
        │       └── Series Name A S02E02.mkv
        └── Series Name B (2018)
            ├── Season 01
            │   ├── Series Name B S01E01.mkv
            │   └── Series Name B S01E02.mkv
            └── Season 02
                ├── Series Name B S02E01.mkv
                └── Series Name B S02E03.mkv

        Returns:
            The list of hierarchy levels.
        """
        return [2, 3]

    def extract_meta(self, data: etree._ElementTree) -> MediaMeta:
        """Extract the metadata for a TV show.

        Args:
            data: The XML data to extract from.

        Returns:
            The extracted metadata.
        """
        meta = MediaMeta()
        root = data.getroot()
        uniqueid = get_element(root, "uniqueid", {"default": "true"})
        if uniqueid is not None:
            meta.nfo_source = uniqueid.get("type")
            meta.unique_id = uniqueid.text
        meta.title = get_text(root, "title")
        meta.originaltitle = get_text(root, "originaltitle")
        meta.tagline = get_text(root, "tagline")
        meta.plot = get_text(root, "plot")
        meta.rating = get_decimal(root, "rating")
        meta.year = get_integer(root, "year")
        meta.premiered = get_text(root, "premiered")
        meta.country = get_text(root, "country")
        meta.mpaa = get_text(root, "mpaa")
        # multiple
        meta.tags = get_all_text(root, "tag")
        meta.genres = get_all_text(root, "genre")
        meta.studios = get_all_text(root, "studio")
        meta.directors = get_all_text(root, "director")
        meta.writers = get_all_text(root, "writer")
        meta.credits = get_all_text(root, "credits")
        # actors
        actors: list[Actor] = []
        for el in root.findall("actor"):
            actors.append(
                Actor(
                    name=get_text(el, "name"),
                    role=get_text(el, "role"),
                    thumb=get_text(el, "thumb"),
                )
            )
        meta.actors = actors or None
        # images
        art = root.find("art")
        meta.poster = get_text(art, "poster")
        meta.backdrop = get_text(art, "fanart")
        # episode specific
        meta.aired = get_text(root, "aired")
        meta.season = get_integer(root, "season")
        meta.episode = get_integer(root, "episode")
        return meta

    async def gen_items(self, lib: MediaLib, path: Path) -> list[MediaPathInfo]:
        """Generate the media items for a TV show.

        Args:
            lib: The media library instance.
            path: The path to generate from.

        Returns:
            The list of media path info for the media items.
        """
        result = []

        # helper function to create parent media path info
        def _parent(path: Path, *, series: str, season: int | None) -> MediaPathInfo:
            info = MediaPathInfo(path)
            info.nfo_path = Path(info.item_dir) / f"{info.item_name}.nfo"
            if not info.nfo_path.exists():
                info.nfo_type = NFOType.TV_SHOW
            info.language = lib.language
            info.title = extract_title(series)
            info.year = extract_year(series)
            info.season = season
            return info

        # helper function to create child media path info
        def _child(path: Path, *, parent: MediaItem) -> MediaPathInfo:
            info = MediaPathInfo(path)
            if info.item_name != (dir := Path(info.item_dir)).name:
                info.nfo_path = dir / f"{info.item_name}.nfo"
                if not info.nfo_path.exists():
                    info.nfo_type = NFOType.EPISODE
            info.language = lib.language
            info.title = parent.title
            info.year = parent.year
            info.season = parent.season
            info.episode = extract_episode(info.item_name)
            info.series_id = parent.unique_id
            info.nfo_source = parent.nfo_source
            return info

        dir = Path(lib.dir)
        parts = path.relative_to(dir).parts
        series = parts[0]
        # extract season number based on the directory structure
        if len(parts) == 2:
            parent_info = _parent(
                dir / series,
                series=series,
                season=extract_season(series) or 1,
            )
        elif len(parts) == 3:
            season = parts[1]
            parent_info = _parent(
                dir / series / season,
                series=series,
                season=extract_season(season),
            )
        else:
            return result

        # create parent item for the directory
        parent_item = await MediaItemService.create(lib.id, path_info=parent_info)
        result.append(parent_info)
        # create child item for the file
        parent_item.title = parent_item.title or parent_info.title
        child_info = _child(path, parent=parent_item)
        await MediaItemService.create(
            lib.id,
            path_info=child_info,
            parent_id=parent_item.id,
            default_title=extract_title(child_info.item_name),
        )
        result.append(child_info)

        return result


_HANDLERS[LibType.TV_SHOW] = TVShowMediaHandler()
