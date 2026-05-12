import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiofiles
from lxml import etree
from sanic.log import Colors, logger

from app.core.constants import ENCODING, NFO_MIME_TYPE
from app.core.flow.context import RETVAL_KEY, Context
from app.core.media.handlers.base import MediaMeta, get_handler
from app.core.renderer import render
from app.models.media import LibType, MediaItem, MediaLib, NFOType

# the path to the NFO templates
TEMPLATES_PATH = Path(__file__).resolve().parents[3] / "static/templates"


def is_nfo(path: Path | str) -> bool:
    """Check if the path is an NFO file.

    Args:
        path: The path to check.

    Returns:
        True if the path is an NFO file, False otherwise.
    """
    if not isinstance(path, Path):
        path = Path(path)
    mime_type, _ = mimetypes.guess_file_type(path)
    return mime_type == NFO_MIME_TYPE


def is_locked(path: Path | str) -> bool:
    """Check if the NFO file is locked by reading the <lockdata> tag.

    Args:
        path: Path to the NFO file.

    Returns:
        True if the NFO file is locked, False otherwise.
    """
    if not isinstance(path, Path):
        path = Path(path)
    if not (path.exists() and path.is_file()):
        return False
    try:
        for _, element in etree.iterparse(path, events=("end",)):
            if element.tag == "lockdata":
                text = element.text
                element.clear()
                return text and text.lower() == "true"
        return False
    except Exception:
        logger.error("Failed to read existing NFO file!", exc_info=True)
        return False


def get_nfo_type(lib_type: LibType) -> str:
    """Get the corresponding NFO type for the given library type.

    Args:
        lib_type: The library type.

    Returns:
        The corresponding NFO type.
    """
    if lib_type == LibType.MOVIE:
        return NFOType.MOVIE
    elif lib_type == LibType.TV_SHOW:
        return NFOType.TV_SHOW
    return ""


def get_nfo_path(item_path: str) -> str:
    """Get the corresponding NFO path for the given media item path.

    Args:
        item_path: The media item path.

    Returns:
        The corresponding NFO path.
    """
    path = Path(item_path)
    if path.is_dir():
        return str(path / f"{path.name}.nfo")
    else:
        return str(path.parent / f"{path.stem}.nfo")


def nfo_context(flow_ctx: Context) -> tuple[str, str, dict]:
    """Extract the NFO context from the flow context.

    Args:
        context: The flow context.

    Returns:
        A tuple of (NFO type, NFO path, NFO data).
    """
    # ensure we have NFO type and path
    bootparams = flow_ctx.bootparams
    nfo_type = bootparams.get("nfo_type")
    nfo_path = bootparams.get("nfo_path")
    if not isinstance(nfo_type, str) or not isinstance(nfo_path, str):
        return "", "", {}

    # ensure we have return value
    retval = flow_ctx.get(RETVAL_KEY)
    if not retval or not isinstance(retval, list) or not isinstance(retval[0], dict):
        return "", "", {}

    # return the NFO context
    return nfo_type, nfo_path, retval[0]


async def gen_nfo(
    nfo_type: str, nfo_path: str, data: dict, *, overwrite: bool = False
) -> bool:
    """Generate NFO file from the given context.

    Args:
        nfo_type: The type of the NFO file (e.g. "movie", "tvshow").
        nfo_path: The path to the NFO file to generate.
        data: The data to render the NFO file with.
        overwrite: Whether to overwrite the NFO file if it already exists.

    Returns:
        True if the NFO file is generated successfully, False otherwise.
    """
    # validate the parameters
    if not nfo_type or not nfo_path or not data:
        return False
    if nfo_type not in NFOType:
        logger.error("Invalid NFO type: %s", nfo_type)
        return False

    # check if NFO file already exists
    path = Path(nfo_path)
    if not overwrite and path.exists():
        logger.info("NFO file already exists, skipping generation: %s", nfo_path)
        return False

    # create parent directory if it doesn't exist
    path.parent.mkdir(parents=True, exist_ok=True)

    # load the NFO template
    tmpl_path = TEMPLATES_PATH / f"{nfo_type}.nfo"
    async with aiofiles.open(tmpl_path, encoding=ENCODING) as f:
        template = await f.read()

    # generate NFO file
    mode = "w" if overwrite else "x"
    async with aiofiles.open(nfo_path, mode, encoding=ENCODING) as f:
        await f.write(render(template, context=data))
    return True


def parse_nfo(lib_type: LibType, path: Path | str) -> MediaMeta | None:
    """Parse the NFO file at the given path.

    Args:
        lib_type: The media library type.
        path: The path to the NFO file.

    Returns:
        The parsed metadata as a MediaMeta object.
    """
    data = None
    if not isinstance(path, Path):
        path = Path(path)
    if path.exists() and path.is_file():
        try:
            data = etree.parse(path, parser=etree.XMLParser(recover=True))
        except Exception:
            logger.error(
                f"Failed to parse the NFO file: {Colors.RED}%s{Colors.END}",
                path,
                exc_info=True,
            )

    # extract metadata from the NFO file
    meta = None
    if data is not None:
        handler = get_handler(lib_type)
        meta = handler.extract_meta(data)
        meta.nfo_path = str(path)

    return meta


async def update_metadata(
    lib: MediaLib, path: Path | str, *, alternative: dict | None = None
):
    """Update the metadata of the media item corresponding to the given NFO file.

    Args:
        lib: The media library instance.
        path: The path to the NFO file.
        alternative: An alternative metadata dictionary.
    """
    # parse the NFO file to get the metadata
    if not isinstance(path, Path):
        path = Path(path)
    meta = parse_nfo(lib.lib_type, path)

    # update the media item in the database
    if meta is not None:
        # helper function to get the value from the alternative metadata
        def _alternative(key: str) -> Any:
            value = None
            if hasattr(meta, key):
                value = getattr(meta, key)
            if value is not None:
                return value
            return alternative.get(key) if alternative else None

        # prepare the data to update the media item
        data = {
            "nfo_path": meta.nfo_path,
            "nfo_mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            "nfo_source": meta.nfo_source,
            "unique_id": meta.unique_id,
            "aired": meta.aired,
            "rating": meta.rating,
            "poster": meta.poster,
            "backdrop": meta.backdrop,
        }
        if (year := _alternative("year")) is not None:
            data["year"] = year
        if (season := _alternative("season")) is not None:
            data["season"] = season
        if (episode := meta.episode) is not None:
            data["episode"] = episode
        if title := meta.title:
            data["title"] = title

        # match the media item by library, directory and name
        await MediaItem.filter(
            lib_id=lib.id,
            dir=str(path.parent),
            name=path.stem,
        ).update(**data)
