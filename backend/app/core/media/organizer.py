"""Recoverable, no-overwrite media organization under a library's writer lock."""

import asyncio
import hashlib
import mimetypes
import os
import re
import tempfile
import unicodedata
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from lxml import etree
from sanic.log import logger
from tortoise.exceptions import ValidationError
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from app.core.media.naming import render_directory, render_filename, render_path
from app.models.download import DownloadTask
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.utils.disk import rename_exclusive

_SUBTITLES = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".lrc"}
_METADATA = (
    "title",
    "year",
    "season",
    "episode",
    "aired",
    "unique_id",
    "nfo_source",
    "poster",
    "backdrop",
    "rating",
)


class OrganizePendingError(RuntimeError):
    """A persisted organization must finish before this library is scanned."""


def _safe_path(root: Path, path: Path):
    """Validate a path before accessing it during organization.

    Args:
        root: The absolute media library root.
        path: The absolute source or destination path to validate.

    Raises:
        ValueError: If the path escapes the library, exceeds path limits, or
            traverses a directory symlink.
    """
    if not path.is_absolute() or not path.is_relative_to(root):
        raise ValueError(f"path is outside the library: {path}")
    if ".." in path.relative_to(root).parts:
        raise ValueError("parent traversal is not allowed")
    if len(str(path)) > 4096 or any(
        len(part.encode("utf-8")) > 255 for part in path.relative_to(root).parts
    ):
        raise ValueError("path exceeds filesystem or database limits")
    for ancestor in (root, *path.parents):
        if ancestor.is_relative_to(root) and ancestor.is_symlink():
            raise ValueError(f"directory symlink is not allowed: {ancestor}")
    if path.is_symlink() and path.is_dir():
        raise ValueError(f"directory symlink is not allowed: {path}")


def _metadata(path: Path, lib_type: str, root_tag: str) -> dict:
    """Extract organization metadata from a complete NFO document.

    Args:
        path: The NFO file path.
        lib_type: The library type used to select a metadata handler.
        root_tag: The required XML root element name.

    Raises:
        OSError: If the NFO file cannot be read.
        etree.LxmlError: If the NFO document cannot be parsed.
        ValueError: If the root element is unexpected or the title is missing.

    Returns:
        The extracted metadata, including the NFO path.
    """
    # require a complete XML document before changing files
    from app.core.media.handlers.base import get_handler

    tree = etree.parse(
        path,
        etree.XMLParser(recover=False, resolve_entities=False, no_network=True),
    )
    if tree.getroot().tag != root_tag:
        raise ValueError(f"unexpected NFO type: {path}")
    metadata = get_handler(lib_type).extract_meta(tree)
    metadata.nfo_path = str(path)
    data = asdict(metadata)
    if not data.get("title"):
        raise ValueError(f"NFO has no title: {path}")
    return data


def _identity(metadata: dict) -> tuple | None:
    """Get the external identity used to match media directories.

    Args:
        metadata: The parsed media metadata.

    Returns:
        The NFO source and unique ID, or None if no unique ID is available.
    """
    value = metadata.get("unique_id")
    return (metadata.get("nfo_source"), value) if value else None


def _fingerprint(path: Path) -> list[int]:
    """Read file attributes used to detect changes during recovery.

    Args:
        path: The file or symlink path to inspect without following the link.

    Returns:
        A list containing the device ID, inode number, byte size, and modification
        time in nanoseconds.
    """
    stat = path.lstat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _companions(path: Path) -> list[Path]:
    """Find the NFO and subtitle files associated with a video basename.

    Args:
        path: The video file path.

    Returns:
        The matching files and symlinks in the video's directory.
    """
    return [
        sibling
        for sibling in path.parent.iterdir()
        if (sibling.is_file() or sibling.is_symlink())
        and (
            sibling.name == f"{path.stem}.nfo"
            or (
                sibling.suffix.lower() in _SUBTITLES
                and sibling.name.startswith(f"{path.stem}.")
            )
        )
    ]


def _relocate_reference(
    value: str | None, old_nfo: Path, new_nfo: Path, moves: dict[str, str]
) -> str | None:
    """Update a local resource reference for the NFO's destination.

    Args:
        value: The resource path or URL stored in the NFO metadata.
        old_nfo: The original NFO path used to resolve relative references.
        new_nfo: The destination NFO path used to rebuild relative references.
        moves: The mapping from source file paths to destination file paths.

    Returns:
        The adjusted local path, or the original value if no change is needed.
    """
    if not value or urlsplit(value).scheme or value.startswith("//"):
        return value
    original = Path(value)
    source = original if original.is_absolute() else old_nfo.parent / original
    destination = moves.get(os.path.normpath(source))
    if destination is None:
        if original.is_absolute() or not source.is_file():
            return value
        destination = str(source)
    if original.is_absolute():
        return destination
    return os.path.relpath(destination, new_nfo.parent)


def _nfo_edits(
    moves: dict[str, str], seasons: dict[str, int | None] | None = None
) -> list[dict]:
    """Prepare NFO edits for resource references and season metadata.

    Args:
        moves: The mapping from source file paths to destination file paths.
        seasons: Optional destination NFO paths mapped to their season numbers;
            a None season removes the season element.

    Returns:
        The destination paths, original content hashes, and replacement XML for
        changed NFO files. Invalid XML documents and NFO symlinks are skipped.
    """
    edits = []
    for source, destination in moves.items():
        if Path(source).suffix.lower() != ".nfo" or Path(source).is_symlink():
            continue
        original = Path(source).read_bytes()
        try:
            tree = etree.fromstring(
                original,
                etree.XMLParser(recover=False, resolve_entities=False, no_network=True),
            )
        except etree.XMLSyntaxError:
            continue
        changed = False
        if seasons is not None and destination in seasons and tree.tag == "tvshow":
            season = seasons[destination]
            element = tree.find("season")
            if season is None:
                if element is not None:
                    tree.remove(element)
                    changed = True
            elif element is None:
                etree.SubElement(tree, "season").text = str(season)
                changed = True
            elif element.text != str(season):
                element.text = str(season)
                changed = True
        for element in tree.iter():
            if element.tag in ("poster", "fanart", "thumb") and element.text:
                value = _relocate_reference(
                    element.text, Path(source), Path(destination), moves
                )
                if value != element.text:
                    element.text = value
                    changed = True
        if changed:
            content = etree.tostring(tree, encoding="unicode")
            edits.append(
                {
                    "path": destination,
                    "before": hashlib.sha256(original).hexdigest(),
                    "content": content,
                }
            )
    return edits


def _context(metadata: dict, parent: dict | None) -> dict:
    """Build template values from media and optional parent metadata.

    Args:
        metadata: The media item's parsed metadata.
        parent: The parent show's metadata, or None for a standalone item.

    Returns:
        A metadata copy with show fields and missing values inherited from the
        parent where supported.
    """
    result = dict(metadata)
    if parent:
        result["show_title"] = parent.get("title")
        result["show_year"] = parent.get("year")
        for name in ("year", "season", "nfo_source"):
            if result.get(name) is None:
                result[name] = parent.get(name)
    return result


async def _validate_updates(lib: MediaLib, updates: list[dict]):
    """Check ORM constraints before a journal can move any files.

    Args:
        lib: The media library whose destination paths must remain unique.
        updates: The planned media item field values, including item IDs.

    Raises:
        ValueError: If a field cannot be saved or a destination path conflicts
            with another planned or existing media item.
    """
    try:
        for update in updates:
            for name, value in update.items():
                if name == "parent_id" and value == "target":
                    continue
                MediaItem._meta.fields_map[name].to_db_value(value, MediaItem)
    except ValidationError as error:
        raise ValueError(f"organized metadata cannot be saved: {error}") from error
    paths = {update["path"]: update["id"] for update in updates}
    if len(paths) != len(updates):
        raise ValueError("multiple media items render to the same database path")
    for item in await MediaItem.filter(lib_id=lib.id, path__in=paths):
        if item.id != paths[item.path]:
            raise ValueError(f"destination is already indexed: {item.path}")


async def _plan(
    lib: MediaLib,
    group: list[MediaItem],
    parent: MediaItem | None,
    *,
    season: int | None = None,
    split: bool = False,
):
    """Build a recoverable organization plan while holding the library lock.

    Args:
        lib: The media library containing the naming template and root path.
        group: The nonempty group of video items to organize together.
        parent: The group's directory item, or None for a standalone movie.
        season: An optional season override for the destination directory.
        split: Whether this group is one part of a source directory being split
            into separate season directories.

    Raises:
        OSError: If required files cannot be inspected or read.
        etree.LxmlError: If the required parent NFO cannot be parsed.
        ValueError: If the proposed organization is unsafe or cannot be saved.

    Returns:
        The journal payload describing file moves, metadata changes, reference
        edits, and source directory cleanup.
    """
    root = Path(lib.dir).absolute()
    template = lib.rename_template
    source_dir = Path(parent.path) if parent else Path(group[0].path).parent
    _safe_path(root, source_dir)
    is_tv = lib.lib_type == LibType.TV_SHOW
    if is_tv and parent is None:
        raise ValueError("TV episodes require a parent directory")
    if not is_tv and len(group) != 1:
        raise ValueError("multi-file movies require an explicit part naming rule")
    parent_nfo = (
        Path(parent.nfo_path or str(source_dir / f"{source_dir.name}.nfo"))
        if parent
        else Path(group[0].nfo_path or Path(group[0].path).with_suffix(".nfo"))
    )
    _safe_path(root, parent_nfo)
    parent_meta = await asyncio.to_thread(
        _metadata, parent_nfo, lib.lib_type, "tvshow" if is_tv else "movie"
    )
    parent_context = _context(parent_meta, parent_meta if is_tv else None)
    if is_tv:
        for name in ("season", "year"):
            if parent_context.get(name) is None:
                parent_context[name] = getattr(parent, name)
        if season is not None:
            parent_context["season"] = season
        parent_context["show_year"] = parent_context.get("year")
    target_dir = root / render_directory(template, parent_context, lib.lib_type)
    _safe_path(root, target_dir)

    if not is_tv and parent:
        videos = [
            file
            for file in source_dir.iterdir()
            if file.is_file()
            and (mimetypes.guess_file_type(file)[0] or "").startswith("video/")
        ]
        if len(videos) != 1:
            raise ValueError("multi-file movies require an explicit part naming rule")

    target_parent = None
    if target_dir != root:
        target_parent = await MediaItem.filter(
            lib_id=lib.id, path=str(target_dir)
        ).first()
    if target_parent and (parent is None or target_parent.id != parent.id):
        if not target_dir.is_dir():
            raise ValueError("indexed destination directory is unavailable")
        source_visible = parent.visible if parent else group[0].visible
        if target_parent.visible != source_visible:
            raise ValueError("merging would change the media item's visibility")
    target_nfo = target_dir / f"{target_dir.name}.nfo"
    reuse_nfo = False
    target_meta = None
    if target_dir != source_dir and target_dir.exists():
        if not target_dir.is_dir():
            raise ValueError("destination is not a directory")
        if any(target_dir.iterdir()):
            if target_nfo.exists():
                target_meta = await asyncio.to_thread(
                    _metadata, target_nfo, lib.lib_type, "tvshow" if is_tv else "movie"
                )
                if not _identity(parent_meta) or _identity(target_meta) != _identity(
                    parent_meta
                ):
                    raise ValueError("destination belongs to a different media item")
                reuse_nfo = True
            elif target_dir not in source_dir.parents:
                raise ValueError("nonempty destination has no matching NFO identity")
            else:
                # merge into the series directory only when every indexed parent
                # at the destination identifies the same show
                if target_parent is not None and (
                    not _identity(parent_meta)
                    or (target_parent.nfo_source, target_parent.unique_id)
                    != _identity(parent_meta)
                ):
                    raise ValueError("destination has an unknown media identity")

    moves: dict[str, str] = {}
    if parent and target_dir != source_dir and not split:
        # keep other indexed seasons in place while moving unindexed files
        # with this media directory under their original names
        blocked = set()
        ids = {item.id for item in group} | {parent.id}
        for other in await MediaItem.filter(lib_id=lib.id):
            other_path = Path(other.path)
            if other.id not in ids and other_path.is_relative_to(source_dir):
                relative = other_path.relative_to(source_dir)
                if len(relative.parts) > 1:
                    blocked.add(relative.parts[0])
        for directory, dirs, files in os.walk(source_dir, followlinks=False):
            directory = Path(directory)
            dirs[:] = [
                name
                for name in dirs
                if directory / name != target_dir
                and not (directory == source_dir and name in blocked)
            ]
            for name in dirs:
                _safe_path(root, directory / name)
            for name in files:
                path = directory / name
                moves[str(path)] = str(target_dir / path.relative_to(source_dir))

    updates = []
    for item in group:
        old = Path(item.path)
        _safe_path(root, old)
        metadata = parent_meta
        own_nfo = Path(item.nfo_path or str(old.with_suffix(".nfo")))
        if is_tv:
            try:
                _safe_path(root, own_nfo)
                metadata = await asyncio.to_thread(
                    _metadata, own_nfo, lib.lib_type, "episodedetails"
                )
                context = _context(metadata, parent_meta)
                if context.get("season") is None:
                    context["season"] = (
                        item.season if item.season is not None else parent.season
                    )
                destination = target_dir / (
                    render_filename(template, context, lib.lib_type) + old.suffix
                )
            except (OSError, ValueError, etree.LxmlError):
                # the parent NFO may arrive before the episode scraper finishes
                destination = target_dir / old.name
                metadata = {}
        else:
            destination = root / render_path(template, parent_context, lib.lib_type)
            destination = destination.with_name(destination.name + old.suffix)
        moves[str(old)] = str(destination)
        for companion in _companions(old):
            if companion == parent_nfo:
                continue
            moves[str(companion)] = str(
                destination.parent
                / (destination.stem + companion.name[len(old.stem) :])
            )
        data = {
            "id": item.id,
            "path": str(destination),
            "dir": str(destination.parent),
            "name": destination.stem,
            "parent_id": "target" if target_dir != root else None,
        }
        if parent and target_dir == root:
            data["visible"] = item.visible and parent.visible
        if not is_tv and target_dir == root:
            for name in _METADATA:
                value = parent_meta.get(name)
                if value is not None:
                    data[name] = str(value) if name == "rating" else value
            data["nfo_path"] = str(destination.with_suffix(".nfo"))
        elif not parent and target_dir != root:
            data["nfo_path"] = None
        if is_tv:
            for name in ("season", "episode"):
                value = metadata.get(name)
                if value is None:
                    value = getattr(item, name)
                if value is None and name == "season":
                    value = parent_meta.get(name)
                    if value is None:
                        value = parent.season
                if value is not None:
                    data[name] = value
        updates.append(data)

    if target_dir == root:
        target_nfo = Path(updates[0]["path"]).with_suffix(".nfo")
    if reuse_nfo or split:
        # preserve an existing NFO even when its external ID matches
        moves.pop(str(parent_nfo), None)
        if reuse_nfo:
            moves[str(target_nfo)] = str(target_nfo)
    else:
        moves[str(parent_nfo)] = str(target_nfo)

    # include local artwork whose name differs from the flat movie's video
    if parent is None:
        for field in ("poster", "backdrop"):
            value = parent_meta.get(field)
            if value and not urlsplit(value).scheme:
                path = Path(value)
                if not path.is_absolute():
                    path = source_dir / path
                if path.is_file() and path.is_relative_to(root):
                    moves[str(path)] = str(target_dir / path.name)

    for source, destination in moves.items():
        _safe_path(root, Path(source))
        _safe_path(root, Path(destination))
    changing = {src: dst for src, dst in moves.items() if src != dst}
    targets = [
        unicodedata.normalize("NFC", path).casefold() for path in changing.values()
    ]
    if len(targets) != len(set(targets)):
        raise ValueError("multiple files render to the same destination")
    for source, destination in changing.items():
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise ValueError(f"destination already exists: {target}")
        ancestor = target.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not os.access(Path(source).parent, os.W_OK | os.X_OK) or not os.access(
            ancestor, os.W_OK | os.X_OK
        ):
            raise ValueError("media directory is not writable")
        if Path(source).lstat().st_dev != ancestor.stat().st_dev:
            raise ValueError("organization cannot cross filesystem boundaries")
    for task in await DownloadTask.all():
        download_dir = Path(task.dir).resolve()
        sources = {
            str((download_dir / name).parent.resolve() / Path(name).name)
            for name in task.files or []
        }
        if any(
            str(Path(source).parent.resolve() / Path(source).name) in sources
            or (
                not task.files
                and (Path(source).parent.resolve() / Path(source).name).is_relative_to(
                    download_dir
                )
            )
            for source in changing
        ):
            raise ValueError("organization would move an original download source")

    for item, data in zip(group, updates, strict=True):
        for name in ("nfo_path", "danmaku_path"):
            value = getattr(item, name)
            if name not in data and value and value in moves:
                data[name] = moves[value]
        old_nfo = Path(item.nfo_path) if item.nfo_path else parent_nfo
        new_nfo = Path(data.get("nfo_path") or moves.get(str(old_nfo), str(old_nfo)))
        for name in ("poster", "backdrop"):
            value = data.get(name, getattr(item, name))
            updated = _relocate_reference(value, old_nfo, new_nfo, moves)
            if updated != value:
                data[name] = updated

    parent_data = None
    if target_dir != root:
        # use the retained destination NFO's metadata when merging so the stored
        # metadata and mtime describe the same file
        stored_meta = target_meta if reuse_nfo else parent_meta
        parent_data = {
            "id": target_parent.id
            if target_parent
            else (parent.id if parent and not split else None),
            "path": str(target_dir),
            "dir": str(target_dir),
            "name": target_dir.name,
            "nfo_path": str(target_nfo),
            "parent_id": None,
            "visible": (
                target_parent.visible
                if target_parent
                else (parent.visible if parent else group[0].visible)
            ),
        }
        for name in _METADATA:
            value = stored_meta.get(name)
            if value is not None or reuse_nfo:
                parent_data[name] = (
                    str(value) if name == "rating" and value is not None else value
                )
        if is_tv:
            parent_data["season"] = parent_context.get("season")
            if reuse_nfo:
                existing_season = target_meta.get("season")
                if existing_season is None and target_parent:
                    existing_season = target_parent.season
                if existing_season != parent_data["season"]:
                    parent_data["season"] = None
            elif (
                target_parent
                and target_parent.id != parent.id
                and target_parent.season != parent_data["season"]
            ):
                parent_data["season"] = None
        for name in ("poster", "backdrop"):
            parent_data[name] = _relocate_reference(
                parent_data.get(name),
                target_nfo if reuse_nfo else parent_nfo,
                target_nfo,
                moves,
            )

    await _validate_updates(lib, [*updates, *([parent_data] if parent_data else [])])

    path_map = dict(changing)
    if parent and source_dir != target_dir and not split:
        path_map[str(source_dir)] = str(target_dir)
    symlinks = []
    for source, destination in changing.items():
        if Path(source).is_symlink():
            link = os.readlink(source)
            target = os.path.normpath(Path(source).parent / link)
            target = changing.get(target, target)
            target = (
                target
                if os.path.isabs(link)
                else os.path.relpath(target, Path(destination).parent)
            )
            if target != link:
                symlinks.append(
                    {
                        "path": destination,
                        "before": link,
                        "target": target,
                    }
                )
    event_paths = {item.path for item in group} | {data["path"] for data in updates}
    if parent:
        if not split:
            event_paths.add(parent.path)
        event_paths.add(str(target_dir))
    creates = []
    seasons = (
        {str(target_nfo): parent_data.get("season")} if is_tv and parent_data else None
    )
    if split and not reuse_nfo:
        if target_nfo.exists() or target_nfo.is_symlink():
            raise ValueError(f"destination already exists: {target_nfo}")
        if unicodedata.normalize("NFC", str(target_nfo)).casefold() in targets:
            raise ValueError("episode and parent NFO names conflict")
        copied_content = etree.tostring(etree.parse(parent_nfo), encoding="unicode")
        for edit in await asyncio.to_thread(
            _nfo_edits, {**moves, str(parent_nfo): str(target_nfo)}, seasons
        ):
            if edit["path"] == str(target_nfo):
                copied_content = edit["content"]
        creates.append({"path": str(target_nfo), "content": copied_content})
    return {
        "moves": [
            {"src": src, "dst": dst, "identity": _fingerprint(Path(src))}
            for src, dst in changing.items()
        ],
        "updates": updates,
        "parent": parent_data,
        "delete_parent": (
            parent.id
            if parent and (parent_data is None or parent_data["id"] != parent.id)
            else None
        ),
        "mapping": path_map,
        "nfo_edits": await asyncio.to_thread(_nfo_edits, moves, seasons),
        "symlinks": symlinks,
        "event_paths": list(event_paths),
        "creates": creates,
        "cleanup": str(source_dir) if parent and source_dir != target_dir else None,
    }


def _move_files(root: Path, payload: dict):
    """Apply or resume a journal's filesystem changes under the library lock.

    Args:
        root: The absolute media library root.
        payload: The persisted organization plan with file identities and edits.

    Raises:
        OrganizePendingError: If a file changed or disappeared unexpectedly.
        ValueError: If a journal path is no longer safe to access.
        OSError: If a filesystem operation fails or a destination already exists.
    """
    for move in payload["moves"]:
        source, destination = Path(move["src"]), Path(move["dst"])
        _safe_path(root, source)
        _safe_path(root, destination)
        if source.exists() or source.is_symlink():
            if _fingerprint(source) != move["identity"]:
                raise OrganizePendingError(
                    f"source changed during organization: {source}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            _safe_path(root, destination)
            rename_exclusive(source, destination)
        elif not (destination.exists() or destination.is_symlink()):
            raise OrganizePendingError(f"source and destination are missing: {source}")
        elif _fingerprint(destination) != move["identity"]:
            # allow rewritten NFOs to have a new inode when their contents match
            edit = next(
                (
                    edit
                    for edit in payload["nfo_edits"]
                    if edit["path"] == str(destination)
                ),
                None,
            )
            link = next(
                (
                    link
                    for link in payload["symlinks"]
                    if link["path"] == str(destination)
                ),
                None,
            )
            fixed_link = (
                link
                and destination.is_symlink()
                and os.readlink(destination) == link["target"]
            )
            if not fixed_link and (
                edit is None or destination.read_bytes() != edit["content"].encode()
            ):
                raise OrganizePendingError(f"destination was replaced: {destination}")
    for create in payload["creates"]:
        path = Path(create["path"])
        _safe_path(root, path)
        content = create["content"].encode()
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_bytes() != content:
                raise OrganizePendingError(f"copied NFO destination changed: {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(root, path)
        descriptor, name = tempfile.mkstemp(prefix=".organizing-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            rename_exclusive(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    for link in payload["symlinks"]:
        path = Path(link["path"])
        _safe_path(root, path)
        current = os.readlink(path)
        if current == link["target"]:
            continue
        if current != link["before"]:
            raise OrganizePendingError(f"symlink changed during organization: {path}")
        descriptor, name = tempfile.mkstemp(prefix=".organizing-", dir=path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.unlink()
            temporary.symlink_to(link["target"])
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    for edit in payload["nfo_edits"]:
        path = Path(edit["path"])
        _safe_path(root, path)
        current = path.read_bytes()
        content = edit["content"].encode()
        if current == content:
            continue
        if hashlib.sha256(current).hexdigest() != edit["before"]:
            raise OrganizePendingError(f"NFO changed during organization: {path}")
        # replace only the original NFO contents verified against the journal
        descriptor, name = tempfile.mkstemp(prefix=".organizing-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


async def _write_in_thread(function, *args):
    """Run a filesystem writer without releasing its lock on cancellation.

    The caller holds the library lock until this function finishes waiting for
    the worker thread, including when the calling task is cancelled.

    Args:
        function: The synchronous filesystem operation to run in a worker thread.
        *args: The positional arguments passed to the operation.

    Raises:
        asyncio.CancelledError: If cancelled, after the worker has stopped.

    Returns:
        The result of the filesystem operation.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # keep the caller's library lock until its filesystem writer has stopped
        await task
        raise


async def _finish(lib: MediaLib, event: MediaEvent) -> dict[str, str]:
    """Finish a journal's filesystem and database updates under the library lock.

    Args:
        lib: The media library whose lock is held by the caller.
        event: The persisted organization event containing the plan to finish.

    Raises:
        OrganizePendingError: If the plan cannot be completed and needs recovery.

    Returns:
        The mapping from original media paths to their organized destinations.
    """
    payload = event.payload
    try:
        await _write_in_thread(_move_files, Path(lib.dir).absolute(), payload)
        async with in_transaction("default"):
            parent = payload["parent"]
            parent_id = None
            if parent:
                data = {key: value for key, value in parent.items() if key != "id"}
                data["nfo_mtime"] = datetime.fromtimestamp(
                    Path(data["nfo_path"]).stat().st_mtime, tz=UTC
                )
                if parent["id"] is None:
                    row, _ = await MediaItem.get_or_create(
                        lib_id=lib.id, path=data.pop("path"), defaults=data
                    )
                    parent_id = row.id
                else:
                    await MediaItem.filter(id=parent["id"], lib_id=lib.id).update(
                        **data
                    )
                    parent_id = parent["id"]
            for update in payload["updates"]:
                data = {key: value for key, value in update.items() if key != "id"}
                if data.get("parent_id") == "target":
                    data["parent_id"] = parent_id
                if data.get("nfo_path") and Path(data["nfo_path"]).exists():
                    data["nfo_mtime"] = datetime.fromtimestamp(
                        Path(data["nfo_path"]).stat().st_mtime, tz=UTC
                    )
                await MediaItem.filter(id=update["id"], lib_id=lib.id).update(**data)
            if (
                payload["delete_parent"]
                and not await MediaItem.filter(
                    parent_id=payload["delete_parent"]
                ).exists()
            ):
                await MediaItem.filter(id=payload["delete_parent"]).delete()
            file_moves = {move["src"]: move["dst"] for move in payload["moves"]}
            for task in await DownloadTask.filter(transfer_lib_id=lib.id):
                targets = task.transfer_targets or {}
                updated = {
                    key: file_moves.get(value, value) for key, value in targets.items()
                }
                if updated != targets:
                    await DownloadTask.filter(id=task.id).update(
                        transfer_targets=updated
                    )
            await (
                MediaEvent.filter(
                    lib_id=lib.id, event_type__in=("created", "moved", "deleted")
                )
                .filter(
                    Q(src_path__in=payload["event_paths"])
                    | Q(dest_path__in=payload["event_paths"])
                )
                .delete()
            )
            await event.delete()
        cleanup = payload.get("cleanup")
        if cleanup:
            await _write_in_thread(_remove_empty_directories, Path(cleanup))
        return payload["mapping"]
    except Exception as error:
        raise OrganizePendingError(
            f"organization {event.id} needs recovery "
            f"before library {lib.id} can continue"
        ) from error


def _remove_empty_directories(path: Path):
    """Remove empty directories from the source tree after organization.

    Args:
        path: The source directory to clean up from its leaves to its root.
    """
    if not path.exists():
        return
    for directory, _, _ in os.walk(path, topdown=False, followlinks=False):
        with suppress(OSError):
            Path(directory).rmdir()


async def recover_organizing(lib: MediaLib) -> dict[str, str]:
    """Finish persisted plans before accepting filesystem events or scan results.

    The caller must hold the library lock throughout recovery.

    Args:
        lib: The media library whose pending organization events are recovered.

    Raises:
        OrganizePendingError: If a persisted plan cannot be completed.

    Returns:
        The combined mapping from original paths to recovered destinations.
    """
    mapping = {}
    for event in await MediaEvent.filter(lib_id=lib.id, event_type="organize"):
        mapping.update(await _finish(lib, event))
    return mapping


async def _season_groups(
    lib: MediaLib, group: list[MediaItem], parent: MediaItem | None
):
    """Group episodes by season when the directory template uses a season field.

    Args:
        lib: The media library containing the directory naming template.
        group: The video items sharing the source directory.
        parent: The directory item supplying fallback season metadata, if any.

    Raises:
        ValueError: If splitting requires unknown seasons or would leave
            unindexed videos in the source directory.

    Returns:
        Pairs of season numbers and item lists, or one pair with a None season
        when the template does not require grouping by season.
    """
    if (
        lib.lib_type != LibType.TV_SHOW
        or parent is None
        or not re.search(r"{{\s*season\s*}}", lib.rename_template.rsplit("/", 1)[0])
    ):
        return [(None, group)]
    groups = {}
    for item in group:
        season = item.season if item.season is not None else parent.season
        try:
            metadata = await asyncio.to_thread(
                _metadata,
                Path(item.nfo_path or Path(item.path).with_suffix(".nfo")),
                lib.lib_type,
                "episodedetails",
            )
            if metadata.get("season") is not None:
                season = metadata["season"]
        except (OSError, ValueError, etree.LxmlError):
            pass
        groups.setdefault(season, []).append(item)
    if len(groups) > 1:
        if None in groups:
            raise ValueError("cannot split episodes without a known season")
        indexed = {item.path for item in group}
        if any(
            file.is_file()
            and str(file) not in indexed
            and (mimetypes.guess_file_type(file)[0] or "").startswith("video/")
            for file in Path(parent.path).iterdir()
        ):
            raise ValueError("waiting for unindexed videos before splitting seasons")
    return list(groups.items())


async def organize_items(lib: MediaLib, item_ids: list[int]) -> dict[str, str]:
    """Organize NFO-backed groups while holding the library lock throughout.

    Pending plans are recovered first. Groups with unsafe or incomplete plans
    are logged and skipped without creating a new journal.

    Args:
        lib: The media library whose lock is held by the caller.
        item_ids: The media item IDs identifying groups to organize.

    Raises:
        OrganizePendingError: If a persisted organization plan cannot finish.

    Returns:
        The combined mapping of paths changed by recovery and new plans.
    """
    mapping = await recover_organizing(lib)
    if not lib.rename_template or not item_ids:
        return mapping
    from app.core.dl.syncer import backfill_transfer_targets

    await backfill_transfer_targets(lib)
    processed = set()
    for item_id in item_ids:
        item = await MediaItem.filter(id=item_id, lib_id=lib.id).first()
        if item is None:
            continue
        parent = None
        if item.parent_id is not None:
            parent = await MediaItem.filter(id=item.parent_id).first()
        elif Path(item.path).is_dir():
            parent = item
        key = parent.id if parent else item.id
        if key in processed:
            continue
        processed.add(key)
        group = (
            await MediaItem.filter(parent_id=parent.id, lib_id=lib.id)
            if parent
            else [item]
        )
        if not group:
            continue
        try:
            groups = await _season_groups(lib, group, parent)
        except (OSError, ValueError, etree.LxmlError) as error:
            logger.warning(
                "Skipping automatic organization for %s: %s", item.path, error
            )
            continue
        for season, subgroup in groups:
            try:
                payload = await _plan(
                    lib, subgroup, parent, season=season, split=len(groups) > 1
                )
                if not any(payload[key] for key in ("moves", "creates", "nfo_edits")):
                    continue
            except (OSError, ValueError, etree.LxmlError) as error:
                logger.warning(
                    "Skipping automatic organization for %s: %s", item.path, error
                )
                continue
            event = await MediaEvent.create(
                lib_id=lib.id,
                event_type="organize",
                src_path=item.path,
                dest_path=None,
                is_directory=parent is not None,
                payload=payload,
            )
            mapping.update(await _finish(lib, event))
    return mapping
