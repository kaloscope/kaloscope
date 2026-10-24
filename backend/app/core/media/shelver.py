import asyncio
import mimetypes
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import aiofiles
from jinja2 import TemplateError
from lxml import etree
from sanic.log import logger
from tortoise.exceptions import ValidationError
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from app.core.constants import ENCODING, NFO_MIME_TYPE
from app.core.flow.context import RETVAL_KEY, Context
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock, notify_media_events
from app.core.media.handlers.base import MediaMeta, get_handler
from app.core.media.metadata import METADATA_BYTES
from app.core.renderer import render
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib, NFOType
from app.utils.asyncio import write_in_thread
from app.utils.disk import rename_exclusive
from app.utils.extractor import extract_title
from app.utils.xml import get_text

# the path to the NFO templates
TEMPLATES_PATH = Path(__file__).resolve().parents[3] / "static/templates"


def is_nfo(path: Path | str) -> bool:
    """Check if the path is an NFO file.

    Args:
        path: Path to check.

    Returns:
        True if the path is an NFO file, False otherwise.
    """
    if not isinstance(path, Path):
        path = Path(path)
    mime_type, _ = mimetypes.guess_file_type(path)
    return mime_type == NFO_MIME_TYPE


def is_locked(path: Path | str) -> bool:
    """Check if the NFO file is locked by reading the `lockdata` tag.

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
    """Get corresponding NFO type for the given library type.

    Args:
        lib_type: Library type.

    Returns:
        Corresponding NFO type.
    """
    if lib_type == LibType.MOVIE:
        return NFOType.MOVIE
    elif lib_type == LibType.TV_SHOW:
        return NFOType.TV_SHOW
    return ""


def get_nfo_path(item_path: str) -> str:
    """Get corresponding NFO path for the given media item path.

    Args:
        item_path: Media item path.

    Returns:
        Corresponding NFO path.
    """
    path = Path(item_path)
    if path.is_dir():
        return str(path / f"{path.name}.nfo")
    else:
        return str(path.parent / f"{path.stem}.nfo")


def nfo_context(flow_ctx: Context) -> tuple[str, str, dict]:
    """Extract NFO type, path and data from the flow context.

    Args:
        flow_ctx: Flow context containing source and rendered NFO fields.

    Returns:
        NFO type, path and data, or empty values if the result is incomplete.
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
    """Generate an NFO using the media item's current path when available.

    Recover organization before writing indexed media. When refreshing metadata,
    persist any follow-up organization and notify its watcher after committing.

    Args:
        nfo_type: Type of the NFO file (e.g. `movie`, `tvshow`).
        nfo_path: Path to the NFO file to generate.
        data: Metadata used to render the NFO and fill missing parsed values.
        overwrite: Whether to overwrite the NFO file if it already exists. Defaults to
            `False`.
        item_id: Media item ID used to resolve the current NFO path. `None` uses the
            supplied NFO path.
        refresh: Whether to update metadata after writing when `item_id` is set.
            Defaults to `False`.

    Returns:
        `True` if the NFO file is generated successfully, `False` otherwise.

    Raises:
        ContentError: If the candidate cannot be rendered as valid metadata.
        OrganizePendingError: If pending organization cannot finish safely.
    """
    if item_id is not None:
        item = await MediaItem.get_or_none(id=item_id).select_related("lib")
        if item is None or item.lib.lib_type not in (LibType.MOVIE, LibType.TV_SHOW):
            return False
        async with library_lock(item.lib.dir):
            from app.core.media.organizer import recover_organizing

            await recover_organizing(item.lib)
            item = await MediaItem.get_or_none(id=item_id).select_related("lib")
            if item is None or item.lib.lib_type not in (
                LibType.MOVIE,
                LibType.TV_SHOW,
            ):
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
                async with in_transaction("default"):
                    affected = await update_metadata(
                        item.lib, current_nfo, fallback=data
                    )
                    if affected and item.lib.rename_template:
                        await MediaEvent.create(
                            lib_id=item.lib_id,
                            src_path=current_nfo,
                            event_type="ingest",
                            payload={"bootparams": [], "organize_ids": affected},
                        )
                if affected and item.lib.rename_template:
                    notify_media_events(item.lib_id)
            return written
    return await _write_nfo(nfo_type, nfo_path, data, overwrite=overwrite)


async def _write_nfo(
    nfo_type: str, nfo_path: str, data: dict, *, overwrite: bool
) -> bool:
    """Validate rendered NFO metadata before publishing the file atomically.

    Args:
        nfo_type: The NFO template type.
        nfo_path: Destination NFO path.
        data: Metadata used to render the NFO.
        overwrite: Whether to replace an existing NFO.

    Returns:
        `True` on success; `False` when an existing file prevents publication.

    Raises:
        ContentError: If rendered metadata is invalid or cannot be read back.
        OSError: If file I/O fails without cancellation.
        asyncio.CancelledError: If cancelled, after active publication stops.
    """
    # validate the parameters
    if not nfo_type or not nfo_path or not data:
        raise ContentError("invalid_metadata")
    if nfo_type not in NFOType:
        raise ContentError("invalid_metadata")

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

    rendered = render_nfo(nfo_type, template, data)

    temporary = path.with_name(f".nfo-{uuid4().hex}.tmp")
    cancelled = False
    try:
        async with aiofiles.open(temporary, "x", encoding=ENCODING) as f:
            await f.write(rendered)
        if overwrite and path.exists() and not path.is_symlink():
            temporary.chmod(path.stat().st_mode & 0o777)
        if overwrite:
            await write_in_thread(os.replace, temporary, path)
        else:
            await write_in_thread(rename_exclusive, temporary, path)
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


def render_nfo(nfo_type: str, template: str, data: dict) -> str:
    """Render and read back NFO metadata without touching a destination file.

    Args:
        nfo_type: Video template type used to select the metadata parser.
        template: Bundled NFO template text.
        data: Candidate fields supplied to the template.

    Returns:
        Valid XML text ready for publication or fixture validation.

    Raises:
        ContentError: If the candidate or rendered metadata is unusable.
    """
    try:
        if not isinstance(data.get("title"), str) or not data["title"].strip():
            raise ContentError("invalid_metadata")
        for name in ("year", "season"):
            _nfo_integer(name, data.get(name))
        rendered = render(template, context=data)
        _, invalid = _parse_nfo_data(
            LibType.MOVIE if nfo_type == NFOType.MOVIE else LibType.TV_SHOW,
            rendered.encode(ENCODING),
            "",
        )
        if invalid:
            raise ContentError("invalid_metadata")
        return rendered
    except ContentError:
        raise
    except (TemplateError, TypeError, ValueError) as error:
        raise ContentError("invalid_metadata") from error


def _nfo_integer(name: str, value: Any) -> int | None:
    """Validate numeric summary fallbacks even when the NFO template omits them.

    Args:
        name: The `year` or `season` field selecting the accepted range.
        value: A candidate integer or decimal string; empty values mean absent.

    Returns:
        Normalized integer, or `None` for an absent value.

    Raises:
        ContentError: If the value is malformed or exceeds the field's range.
    """
    if value is None or not (text := str(value).strip()):
        return None
    if not text.isdecimal():
        raise ContentError("invalid_metadata")
    try:
        number = int(text)
    except ValueError as error:
        raise ContentError("invalid_metadata") from error
    minimum, maximum = (1, 9999) if name == "year" else (0, 2**31 - 1)
    if not minimum <= number <= maximum:
        raise ContentError("invalid_metadata")
    return number


def _parse_nfo_data(
    lib_type: LibType, content: bytes, path: str
) -> tuple[MediaMeta, tuple[str, ...]]:
    """Parse bounded NFO bytes and identify unusable persisted fields.

    Args:
        lib_type: Video library type selecting the supported XML roots.
        content: Current XML bytes, including an optional encoding declaration.
        path: File identity attached to the parsed metadata.

    Returns:
        Parsed metadata and the names of invalid fields omitted from it.

    Raises:
        ContentError: If the document is too large, malformed or unsupported.
    """
    if len(content) > METADATA_BYTES:
        raise ContentError("media_limit_exceeded")
    try:
        root = etree.fromstring(
            content, parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
    except etree.XMLSyntaxError as error:
        raise ContentError("invalid_metadata") from error
    roots = ("movie",) if lib_type == LibType.MOVIE else ("tvshow", "episodedetails")
    data = root.getroottree()
    if root.tag not in roots or data.docinfo.internalDTD is not None:
        raise ContentError("invalid_metadata")
    meta = get_handler(lib_type).extract_meta(data)
    meta.nfo_path = path
    invalid = []
    for name in ("rating", "year", "season", "episode"):
        raw = get_text(root, name)
        value = getattr(meta, name)
        if not raw:
            continue
        valid = value is not None
        if valid and name == "rating":
            valid = 0 <= value <= 10
        elif valid and name == "year":
            valid = 1 <= value <= 9999
        elif valid and name in ("season", "episode"):
            valid = 0 <= value <= 2**31 - 1
        if not valid:
            invalid.append(name)
            setattr(meta, name, None)
    for name, field in {
        "title": "title",
        "site": "nfo_source",
        "id": "unique_id",
        "aired": "aired",
        "poster": "poster",
        "backdrop": "backdrop",
    }.items():
        try:
            MediaItem._meta.fields_map[field].to_db_value(
                getattr(meta, name), MediaItem
            )
        except (ValidationError, TypeError, ValueError):
            invalid.append(name)
            setattr(meta, name, None)
    return meta, tuple(invalid)


def read_nfo(
    lib_type: LibType, path: Path | str | None
) -> tuple[MediaMeta | None, str | None, tuple[str, ...]]:
    """Read regular NFO files with separate missing and failure outcomes.

    Args:
        lib_type: Media library type.
        path: The NFO file path, or `None` when no file is registered.

    Returns:
        Metadata, a controlled read error and invalid field names. A missing file
        returns no metadata and no error; failures never expose raw I/O messages.
    """
    if path is None:
        return None, None, ()
    try:
        path = Path(path)
        if not stat.S_ISREG(path.stat().st_mode):
            return None, "media_source_unavailable", ()
        with path.open("rb") as file:
            meta, invalid = _parse_nfo_data(
                lib_type, file.read(METADATA_BYTES + 1), str(path)
            )
        return meta, None, invalid
    except FileNotFoundError:
        return None, None, ()
    except OSError:
        return None, "media_source_unavailable", ()
    except ContentError as error:
        return None, error.code, ()


def parse_nfo(lib_type: LibType, path: Path | str) -> MediaMeta | None:
    """Read usable NFO metadata for existing synchronization callers.

    Args:
        lib_type: Video library type.
        path: Current NFO file path.

    Returns:
        Parsed metadata, or `None` for a missing or unreadable document.
    """
    return read_nfo(lib_type, path)[0]


async def update_metadata(
    lib: MediaLib, path: Path | str, *, fallback: dict | None = None
) -> list[int]:
    """Update media items matched by their NFO path.

    Args:
        lib: Media library instance.
        path: Path to the NFO file.
        fallback: Fallback metadata dictionary. `None` supplies no fallback
            metadata.

    Returns:
        The IDs of the updated media items, or an empty list if the NFO cannot be
        parsed or no matching items exist.

    Raises:
        ContentError: If a numeric summary fallback cannot be stored safely.
    """
    # parse the NFO file to get the metadata
    if not isinstance(path, Path):
        path = Path(path)
    meta = parse_nfo(lib.lib_type, path)

    # update the media item in the database
    if meta is not None:

        def _fallback(key: str) -> Any:
            """Read a parsed field before consulting the save candidate.

            Args:
                key: Field name to resolve.

            Returns:
                Current value, or `None` when both sources omit it.

            Raises:
                ContentError: If the numeric fallback is invalid.
            """
            value = None
            if hasattr(meta, key):
                value = getattr(meta, key)
            if value is not None:
                return value
            return _nfo_integer(key, fallback.get(key)) if fallback else None

        # prepare the data to update the media item
        data = {
            "nfo_path": meta.nfo_path,
            "nfo_mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
            "nfo_source": meta.site,
            "unique_id": meta.id,
            "aired": meta.aired,
            "rating": meta.rating,
            "poster": meta.poster,
            "backdrop": meta.backdrop,
            "year": _fallback("year"),
        }
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
