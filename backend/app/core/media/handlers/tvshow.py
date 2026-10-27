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


def episode_location(parts: tuple[str, ...]) -> tuple[int | None, int | None] | None:
    """Read the season and episode numbers the library assigns to a video path.

    Args:
        parts: Path components relative to the library directory, ending with the
            video file name.

    Returns:
        Season and episode numbers, each `None` when unparsed, or `None` when the
        path is not a series or season file. Series folders default to season 1.
    """
    if len(parts) == 2:
        season = extract_season(parts[0])
        if season is None:
            season = 1
    elif len(parts) == 3:
        season = extract_season(parts[1])
    else:
        return None
    return season, extract_episode(Path(parts[-1]).stem)


class TVShowMediaHandler(MediaHandler):
    """The media item handler for TV show library type."""

    def accept(self) -> list[str]:
        """The mime types accepted by the TV show library type.

        Returns:
            List of mime types.
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
            List of hierarchy levels.
        """
        return [2, 3]

    def extract_meta(self, data: etree._ElementTree) -> MediaMeta:
        """Extract the metadata for a TV show.

        Args:
            data: The XML data to extract from.

        Returns:
            Extracted metadata.
        """
        meta = MediaMeta()
        root = data.getroot()
        uniqueid = get_element(root, "uniqueid", {"default": "true"})
        if uniqueid is not None:
            meta.site = uniqueid.get("type")
            meta.id = uniqueid.text
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
            lib: Media library instance.
            path: Path to generate from.

        Returns:
            List of media path info for the media items.
        """
        result = []

        def _parent(path: Path, *, series: str, season: int | None) -> MediaPathInfo:
            """Build series or season metadata for the parent item.

            Args:
                path: Parent directory path.
                series: Series directory name.
                season: Season number, or `None` when unknown.

            Returns:
                Parent metadata and missing NFO details.
            """
            info = MediaPathInfo(path)
            info.nfo_path = Path(info.item_dir) / f"{info.item_name}.nfo"
            if not info.nfo_path.exists():
                info.nfo_type = NFOType.TV_SHOW
            info.language = lib.language
            info.title = extract_title(series)
            info.year = extract_year(series)
            info.season = season
            return info

        def _child(path: Path, *, parent: MediaItem) -> MediaPathInfo:
            """Build episode metadata using the parent series information.

            Args:
                path: Episode file path.
                parent: Persisted series or season item.

            Returns:
                Episode metadata and missing NFO details.
            """
            info = MediaPathInfo(path)
            if info.item_name != (dir := Path(info.item_dir)).name:
                info.nfo_path = dir / f"{info.item_name}.nfo"
                if not info.nfo_path.exists():
                    info.nfo_type = NFOType.EPISODE
            info.language = lib.language
            info.title = parent.title or parent_info.title
            info.year = parent.year
            info.season = parent.season
            info.episode = extract_episode(info.item_name)
            info.series_id = parent.unique_id
            info.site = parent.nfo_source
            return info

        dir = Path(lib.dir)
        parts = path.relative_to(dir).parts
        # extract season number based on the directory structure
        if (location := episode_location(parts)) is None:
            return result
        series = parts[0]
        parent_info = _parent(
            dir.joinpath(*parts[:-1]), series=series, season=location[0]
        )

        # create parent item for the directory
        parent_item = await MediaItemService.create(lib.id, path_info=parent_info)
        result.append(parent_info)
        # create child item for the file
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
