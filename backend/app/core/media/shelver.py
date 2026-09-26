import asyncio
import mimetypes
import os
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import aiofiles
from lxml import etree
from sanic.log import Colors, logger
from tortoise.expressions import Q

from app.core.constants import ENCODING, NFO_MIME_TYPE
from app.core.flow.context import RETVAL_KEY, Context
from app.core.media.coordination import library_lock
from app.core.media.handlers.base import MediaMeta, get_handler
from app.core.renderer import render
from app.models.media import LibType, MediaItem, MediaLib, NFOType
from app.utils.disk import rename_exclusive
from app.utils.extractor import extract_title

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
    nfo_type: str,
    nfo_path: str,
    data: dict,
    *,
    overwrite: bool = False,
    item_id: int | None = None,
    refresh: bool = False,
) -> bool:
    """Generate NFO file from the given context.

    Args:
        nfo_type: The type of the NFO file (e.g. `movie`, `tvshow`).
        nfo_path: The path to the NFO file to generate.
        data: The metadata used to render the NFO and fill missing parsed values.
        overwrite: Whether to overwrite the NFO file if it already exists.
        item_id: The media item ID used to resolve the current NFO path.
        refresh: Whether to update metadata after writing when `item_id` is set.

    Returns:
        `True` if the NFO file is generated successfully, `False` otherwise.
    """
    if item_id is not None:
        item = await MediaItem.get_or_none(id=item_id).select_related("lib")
        if item is None:
            return False
        async with library_lock(item.lib.dir):
            item = await MediaItem.get_or_none(id=item_id).select_related("lib")
            if item is None:
                return False
            # the workflow may still hold a path from before organization
            if not Path(item.path).exists():
                return False
            current_nfo = item.nfo_path
            if (
                not current_nfo
                and item.parent_id is not None
                and item.lib.lib_type == LibType.MOVIE
            ):
                parent = await MediaItem.get_or_none(
                    id=item.parent_id, lib_id=item.lib_id
                )
                if parent is not None:
                    current_nfo = parent.nfo_path or get_nfo_path(parent.path)
            current_nfo = current_nfo or get_nfo_path(item.path)
            written = await _write_nfo(
                nfo_type,
                current_nfo,
                data,
                overwrite=overwrite,
            )
            if written and refresh:
                await update_metadata(item.lib, current_nfo, fallback=data)
            return written
    return await _write_nfo(nfo_type, nfo_path, data, overwrite=overwrite)


async def _write_nfo(
    nfo_type: str, nfo_path: str, data: dict, *, overwrite: bool
) -> bool:
    """Publish an NFO file atomically.

    Args:
        nfo_type: The NFO template type.
        nfo_path: The destination NFO path.
        data: The metadata used to render the NFO.
        overwrite: Whether to replace an existing NFO.

    Returns:
        `True` on success; `False` for invalid input or a name conflict.

    Raises:
        OSError: If file I/O fails without cancellation.
        asyncio.CancelledError: If cancelled, after active publication stops.
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

    temporary = path.with_name(f".nfo-{uuid4().hex}.tmp")
    cancelled = False
    try:
        async with aiofiles.open(temporary, "x", encoding=ENCODING) as f:
            await f.write(render(template, context=data))
        if overwrite and path.exists() and not path.is_symlink():
            temporary.chmod(path.stat().st_mode & 0o777)
        publication = asyncio.create_task(
            asyncio.to_thread(os.replace, temporary, path)
            if overwrite
            else asyncio.to_thread(rename_exclusive, temporary, path)
        )
        try:
            await asyncio.shield(publication)
        except asyncio.CancelledError:
            # wait for publication before removing the temporary file
            with suppress(Exception):
                await publication
            raise
    except asyncio.CancelledError:
        cancelled = True
        raise
    except FileExistsError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            if not cancelled:
                raise
            logger.warning(
                "Failed to remove temporary NFO after cancellation: %s",
                temporary,
                exc_info=True,
            )
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
    lib: MediaLib, path: Path | str, *, fallback: dict | None = None
) -> list[int]:
    """Update the metadata of the media item corresponding to the given NFO file.

    Args:
        lib: The media library instance.
        path: The path to the NFO file.
        fallback: The fallback metadata dictionary.

    Returns:
        The IDs of the updated media items, or an empty list if the NFO cannot be
        parsed or no matching items exist.
    """
    # parse the NFO file to get the metadata
    if not isinstance(path, Path):
        path = Path(path)
    meta = parse_nfo(lib.lib_type, path)

    # update the media item in the database
    if meta is not None:
        # helper function to get the value from the fallback metadata
        def _fallback(key: str) -> Any:
            value = None
            if hasattr(meta, key):
                value = getattr(meta, key)
            if value is not None:
                return value
            return fallback.get(key) if fallback else None

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
        if (year := _fallback("year")) is not None:
            data["year"] = year
        if (season := _fallback("season")) is not None:
            data["season"] = season
        if (episode := meta.episode) is not None:
            data["episode"] = episode

        # extract title from the filename if not provided
        if title := meta.title:
            data["title"] = title
        else:
            data["title"] = extract_title(path.stem)

        # match registered NFO paths or the default filename
        items = MediaItem.filter(
            Q(nfo_path=str(path)) | Q(dir=str(path.parent), name=path.stem),
            lib_id=lib.id,
        )
        # account for `flat=True` in the return annotation
        ids = cast(list[int], await items.values_list("id", flat=True))
        if ids:
            await MediaItem.filter(id__in=ids).update(**data)
        return ids
    return []
