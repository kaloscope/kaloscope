import asyncio
import hashlib
import mimetypes
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from copy import copy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

from lxml import etree
from sanic.log import logger
from tortoise.exceptions import ValidationError
from tortoise.transactions import in_transaction

from app.core.media.coordination import notify_media_events
from app.core.media.metadata import METADATA_BYTES, _read_xml, merge_metadata
from app.core.media.naming import render_directory, render_filename, render_path
from app.models.download import DownloadTask, OfflineDownloadJob
from app.models.media import (
    IndexState,
    LibType,
    MediaEvent,
    MediaFormat,
    MediaItem,
    MediaLib,
)
from app.utils.asyncio import write_in_thread
from app.utils.disk import original_file_info, rename_exclusive
from app.utils.xml import get_integer

if TYPE_CHECKING:
    from app.core.media.handlers.reading import ReadingSource

_SUBTITLES = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".lrc"}


# map normalized metadata to the existing database columns
_METADATA = {
    "title": "title",
    "year": "year",
    "season": "season",
    "episode": "episode",
    "aired": "aired",
    "unique_id": "id",
    "nfo_source": "site",
    "poster": "poster",
    "backdrop": "backdrop",
    "rating": "rating",
}


# one library walk and one task query shared by many plans of a batch review
_LINKS: ContextVar[list[tuple[str, str, str | None]] | None] = ContextVar(
    "organizer_links", default=None
)
_TASKS: ContextVar[list[DownloadTask] | None] = ContextVar(
    "organizer_tasks", default=None
)


class OrganizePendingError(RuntimeError):
    """Indicate that a persisted organization plan could not finish."""


class OrganizeDeferredError(RuntimeError):
    """A media group must wait for its remaining download files before moving."""


def _safe_path(root: Path, path: Path):
    """Validate a path before accessing it during organization.

    Args:
        root: Absolute media library root.
        path: Absolute source or destination path to validate.

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


def _metadata(path: Path, lib_type: LibType, root_tag: str) -> dict:
    """Extract organization metadata from a complete NFO document.

    Args:
        path: The NFO file path.
        lib_type: Library type used to select a metadata handler.
        root_tag: Required XML root element name.

    Raises:
        OSError: If the NFO file cannot be read.
        etree.LxmlError: If the NFO document cannot be parsed.
        ValueError: If the root element is unexpected or the title is missing.

    Returns:
        Extracted metadata, including the NFO path.
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
    """Get external identity used to match media directories.

    Args:
        metadata: Parsed media metadata.

    Returns:
        Metadata site and external ID, or `None` if no ID is available.
    """
    value = metadata.get("id")
    return (metadata.get("site"), value) if value else None


def _fingerprint(path: Path) -> list[int]:
    """Read file attributes used to detect changes during recovery.

    Args:
        path: File or symlink path to inspect without following the link.

    Returns:
        Device ID, inode, byte size, and modification time in nanoseconds.
    """
    stat = path.lstat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _companions(path: Path) -> list[Path]:
    """Find the NFO and subtitle files associated with a video basename.

    Args:
        path: Video file path.

    Returns:
        Matching files and symlinks in the video's directory, accepting
        case-insensitive extensions and excluding subtitles with a longer
        matching video basename.
    """
    siblings = [
        sibling
        for sibling in path.parent.iterdir()
        if sibling.is_file() or sibling.is_symlink()
    ]
    prefix = f"{path.stem}."
    other_video_prefixes = tuple(
        f"{sibling.stem}."
        for sibling in siblings
        if sibling.stem.startswith(prefix)
        and (mimetypes.guess_file_type(sibling)[0] or "").startswith("video/")
    )
    return [
        sibling
        for sibling in siblings
        if (sibling.stem == path.stem and sibling.suffix.lower() == ".nfo")
        or (
            sibling.suffix.lower() in _SUBTITLES
            and sibling.name.startswith(prefix)
            and not sibling.name.startswith(other_video_prefixes)
        )
    ]


def _reference_key(path: Path) -> str:
    """Normalize a file reference without following its final symlink.

    Args:
        path: File reference whose parent directory aliases are resolved.

    Returns:
        Absolute path normalized to `NFC` and case-folded for alias lookup,
        preserving the final component to distinguish a link from its target.
    """
    return unicodedata.normalize(
        "NFC", str(path.parent.resolve() / path.name)
    ).casefold()


def _validate_link_references(root: Path, moves: dict[str, str]):
    """Reject moves referenced by library symlinks outside the plan.

    Include links without index records and directory aliases. Do not traverse
    directory symlinks. Normalize case and Unicode when matching references.

    Args:
        root: Absolute media library root to inspect.
        moves: Planned source and destination paths, including retained links
            whose targets the plan updates.

    Raises:
        OSError: If a directory or symlink cannot be inspected.
        ValueError: If a library symlink outside the plan references a file that
            would move, directly or through a directory alias.
    """
    sources = {
        _reference_key(Path(source))
        for source, destination in moves.items()
        if source != destination
    }
    if not sources:
        return
    source_directories = {
        str(parent) for source in sources for parent in Path(source).parents
    }
    links = _LINKS.get()
    if links is None:
        links = _library_links(root)
    for path, target, directory in links:
        if path in moves:
            continue
        if target in sources or (
            directory is not None and directory in source_directories
        ):
            raise ValueError(
                f"moving a file would break a symlink outside the group: {path}"
            )


def _library_links(root: Path) -> list[tuple[str, str, str | None]]:
    """List a library's symlinks with their normalized targets.

    Args:
        root: Absolute media library root to inspect without following links.

    Returns:
        Each link's path, its target's reference key and, for a directory link,
        the resolved target directory's reference key.

    Raises:
        OSError: If a directory or symlink cannot be inspected.
    """
    links = []
    directories = [root]
    while directories:
        for path in directories.pop().iterdir():
            if path.is_symlink():
                target = path.parent / os.readlink(path)
                links.append(
                    (
                        str(path),
                        _reference_key(target),
                        _reference_key(target.resolve()) if path.is_dir() else None,
                    )
                )
            elif path.is_dir():
                directories.append(path)
    return links


@asynccontextmanager
async def shared_validation(lib: MediaLib) -> AsyncGenerator[None]:
    """Share one symlink walk and one download query across many plans.

    Planning validates every group against the library's symlinks and download
    tasks. A batch review plans many groups at one moment, so it reads both once.
    Plans made inside describe that moment, as previews already do.

    Args:
        lib: Library whose plans are being previewed.

    Yields:
        Control while validators reuse the snapshot.

    Raises:
        OSError: If the library cannot be walked.
    """
    links = _LINKS.set(
        await asyncio.to_thread(_library_links, Path(lib.dir).absolute())
    )
    tasks = _TASKS.set(await DownloadTask.all())
    try:
        yield
    finally:
        _LINKS.reset(links)
        _TASKS.reset(tasks)


def _relocate_reference(
    value: str | None, old_nfo: Path, new_nfo: Path, moves: dict[str, str]
) -> str | None:
    """Update a local resource reference for the NFO's destination.

    Args:
        value: Resource path or URL stored in the NFO metadata.
        old_nfo: Original NFO path used to resolve relative references.
        new_nfo: Destination NFO path used to rebuild relative references.
        moves: Mapping from source file paths to destination file paths.

    Returns:
        Adjusted local path or unchanged reference with surrounding whitespace
        removed, or `None` if no reference was provided.
    """
    if value is None:
        return None
    value = value.strip()
    if not value or urlsplit(value).scheme or value.startswith("//"):
        return value
    original = Path(value)
    source = original if original.is_absolute() else old_nfo.parent / original
    destination = moves.get(os.path.normpath(source))
    if destination is None:
        if original.is_absolute() or not source.is_file():
            return value
        destination = str(source)
    if os.path.normpath(new_nfo.parent / original) == os.path.normpath(destination):
        return value
    if original.is_absolute():
        return destination
    return os.path.relpath(destination, new_nfo.parent)


def _shared_artwork(root: Path, artwork: set[Path], nfo_paths: set[Path]) -> set[Path]:
    """Find artwork referenced by NFO files outside the organization plan.

    Inspect disk files to preserve references from media without index records.
    Keep all candidate artwork in place if another NFO cannot be read or parsed.

    Args:
        root: Media library root to search for other NFO files.
        artwork: Local artwork paths proposed for movement.
        nfo_paths: The NFO paths whose references are updated by the plan.

    Returns:
        Artwork paths that must remain at their original locations.
    """
    candidates = {path: path.resolve() for path in artwork}
    shared = set()
    for nfo in root.rglob("*.nfo", case_sensitive=False):
        if nfo in nfo_paths or nfo.is_dir():
            continue
        try:
            tree = etree.parse(
                nfo,
                etree.XMLParser(recover=False, resolve_entities=False, no_network=True),
            )
        except (OSError, etree.LxmlError):
            return artwork
        for element in tree.iter():
            value = (element.text or "").strip()
            if (
                element.tag not in ("poster", "fanart", "thumb")
                or not value
                or urlsplit(value).scheme
                or value.startswith("//")
            ):
                continue
            reference = (nfo.parent / value).resolve()
            shared.update(
                path for path, resolved in candidates.items() if resolved == reference
            )
        if shared == artwork:
            break
    return shared


def _nfo_edits(
    moves: dict[str, str], seasons: dict[str, int | None] | None = None
) -> list[dict]:
    """Prepare NFO edits for resource references and season metadata.

    Args:
        moves: Mapping from source file paths to destination file paths.
        seasons: Optional destination NFO paths mapped to their season numbers; `None`
            leaves season metadata unchanged.
            a `None` season removes the season element.

    Raises:
        OSError: If an NFO source cannot be read.

    Returns:
        Destination paths, original content hashes, and replacement XML for
        changed NFO files, including symlinks so the caller can reject unsafe
        moves. Invalid XML documents are skipped.
    """
    edits = []
    for source, destination in moves.items():
        if Path(source).suffix.lower() != ".nfo":
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
            elif get_integer(tree, "season") != season:
                element.text = str(season)
                changed = True
        for element in tree.iter():
            if element.tag in ("poster", "fanart", "thumb") and element.text:
                value = _relocate_reference(
                    element.text, Path(source), Path(destination), moves
                )
                if value != element.text.strip():
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
        metadata: Media item's parsed metadata.
        parent: Parent show's metadata, or `None` for a standalone item.

    Returns:
        A metadata copy with a parent namespace and supported missing values
        inherited from the show. Standalone items use their own parent metadata.
    """
    result = dict(metadata)
    result["parent"] = dict(parent if parent is not None else metadata)
    if parent:
        for name in ("year", "season", "site"):
            if result.get(name) is None:
                result[name] = parent.get(name)
    return result


async def _validate_updates(lib: MediaLib, updates: list[dict]):
    """Check ORM constraints before a journal can move any files.

    Args:
        lib: Media library whose destination paths must remain unique.
        updates: Planned media item field values, including item IDs.

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


async def _validate_moves(root: Path, moves: dict[str, str]):
    """Validate file destinations, filesystem boundaries and external links.

    Args:
        root: Absolute library root.
        moves: Source and destination file paths, including unchanged companions.

    Raises:
        OSError: If a source or destination cannot be inspected.
        ValueError: If a move conflicts, crosses filesystems or breaks a link.
    """
    for source, destination in moves.items():
        _safe_path(root, Path(source))
        _safe_path(root, Path(destination))
    changing = {src: dst for src, dst in moves.items() if src != dst}
    targets = [
        unicodedata.normalize("NFC", path).casefold() for path in changing.values()
    ]
    target_paths = set(targets)
    if len(targets) != len(target_paths):
        raise ValueError("multiple files render to the same destination")
    if any(
        str(ancestor) in target_paths
        for target in target_paths
        for ancestor in Path(target).parents
    ):
        raise ValueError("destination file conflicts with a planned directory")
    for source, destination in changing.items():
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise ValueError(f"destination already exists: {target}")
        ancestor = target.parent
        while not ancestor.exists():
            ancestor = ancestor.parent
        if not ancestor.is_dir():
            raise ValueError(f"destination ancestor is not a directory: {ancestor}")
        if not os.access(Path(source).parent, os.W_OK | os.X_OK) or not os.access(
            ancestor, os.W_OK | os.X_OK
        ):
            raise ValueError("media directory is not writable")
        if Path(source).lstat().st_dev != ancestor.stat().st_dev:
            raise ValueError("organization cannot cross filesystem boundaries")

    await asyncio.to_thread(_validate_link_references, root, moves)


async def _validate_downloads(lib: MediaLib, affected_paths: set[str]):
    """Protect original downloads and defer incomplete transfers.

    Args:
        lib: Library containing the planned destinations.
        affected_paths: Files moved or rewritten by the plan.

    Raises:
        OrganizeDeferredError: If an affected download is still transferring.
        ValueError: If the plan would modify an original download source.
    """
    tasks = _TASKS.get()
    for task in tasks if tasks is not None else await DownloadTask.all():
        targets = task.transfer_targets or {}
        if (
            task.transfer_lib_id == lib.id
            and affected_paths.intersection(targets.values())
            and any(
                not targets.get(name) or not Path(targets[name]).is_file()
                for name in task.files or []
            )
            and (
                task.transfer_pending
                or await OfflineDownloadJob.filter(
                    download_id=task.id, completion_due_at__not_isnull=True
                ).exists()
            )
        ):
            raise OrganizeDeferredError(
                f"waiting for download task {task.id} to finish transferring"
            )
        download_dir = Path(task.dir).resolve()
        sources = {_reference_key(download_dir / name) for name in task.files or []}
        if any(
            _reference_key(Path(path)) in sources
            or (
                not task.files
                and (Path(path).parent.resolve() / Path(path).name).is_relative_to(
                    download_dir
                )
            )
            for path in affected_paths
        ):
            raise ValueError("organization would modify an original download source")


def _reading_metadata(source: "ReadingSource") -> dict:
    """Read naming fields without using the display title's filename fallback.

    Args:
        source: Discovered source inside a validated work directory.

    Returns:
        Current fields with external priority and supported parent inheritance.

    Raises:
        ValueError: If a metadata source cannot be read reliably.
    """
    from app.core.media.reader import read_metadata

    metadata = read_metadata(source)
    if metadata.state == "error":
        raise ValueError(f"reading metadata is unavailable: {source.path}")
    external, embedded, parent = (
        origin.parsed.data if origin and origin.parsed else None
        for origin in (metadata.external, metadata.embedded, metadata.parent)
    )
    return merge_metadata(external, embedded, parent=parent).model_dump()


def _opf_edits(paths: set[Path], moves: dict[str, str]) -> list[dict]:
    """Relocate local OPF links while preserving unrelated metadata and elements.

    Args:
        paths: Owned external OPF files, including lower-priority companion files.
        moves: Source and destination paths for every file in the work.

    Returns:
        Recoverable edits only for OPF files whose local links actually change.

    Raises:
        OSError: If an OPF cannot be read.
        ValueError: If an OPF is oversized or invalid.
    """
    edits = []
    for path in sorted(paths):
        with path.open("rb") as file:
            before = file.read(METADATA_BYTES + 1)
        tree = _read_xml(before, {"package", "{http://www.idpf.org/2007/opf}package"})
        destination = Path(moves[str(path)])
        changed = False
        for element in tree.iter():
            value = element.get("href")
            if not value:
                continue
            url = urlsplit(value)
            if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
                continue
            original = Path(os.path.normpath(path.parent / unquote(url.path)))
            target = moves.get(str(original))
            if target is None:
                continue
            relative = os.path.relpath(target, destination.parent)
            if relative == os.path.normpath(unquote(url.path)):
                continue
            element.set("href", url._replace(path=quote(relative, safe="/")).geturl())
            changed = True
        if changed:
            edits.append(
                {
                    "path": str(destination),
                    "before": hashlib.sha256(before).hexdigest(),
                    "content": etree.tostring(tree, encoding="unicode"),
                }
            )
    return edits


def _reading_files(lib: MediaLib, items: list[MediaItem]) -> dict:
    """Plan one complete reading work in a worker thread without changing files.

    Args:
        lib: Reading library with an enabled naming template.
        items: Work followed by all its chapters, or one standalone item.

    Returns:
        File moves, item path updates, OPF edits and the target work directory.

    Raises:
        OSError: If files cannot be inspected or read.
        ValueError: If sources, naming metadata, layout or destinations are unsafe.
    """
    from app.core.media.handlers.reading import ReadingMediaHandler, is_ignored_name

    root = Path(lib.dir).absolute()
    work = items[0]
    source_dir = Path(work.dir)
    template = lib.rename_template
    if not template or source_dir.parent != root or work.parent_id is not None:
        raise ValueError("reading organization requires a direct work directory")
    _safe_path(root, source_dir)
    snapshots = {}
    directories = {}
    for directory, dirs, files in os.walk(source_dir, followlinks=False):
        path = Path(directory)
        _safe_path(root, path)
        directories[path] = _fingerprint(path)
        for name in dirs:
            _safe_path(root, path / name)
        for name in files:
            file = path / name
            if not stat.S_ISREG(file.lstat().st_mode):
                raise ValueError(f"reading organization requires regular files: {file}")
            snapshots[file] = _fingerprint(file)
    scan = ReadingMediaHandler(lib.lib_type).scan_sources(lib.dir, work_path=source_dir)
    sources = {str(source.path): source for source in scan.sources}
    if scan.issues or set(sources) != {item.path for item in items}:
        raise ValueError("reading work contains missing or unindexed sources")
    for item in items:
        source = sources[item.path]
        if source.format != item.format or (
            item.id != work.id
            and (item.parent_id != work.id or source.parent_path != source_dir)
        ):
            raise ValueError("reading source ownership changed")
    collection_metadata = _reading_metadata(sources[work.path])
    collection_context = {
        **collection_metadata,
        "parent": collection_metadata,
    }
    target_dir = root / render_directory(template, collection_context, lib.lib_type)
    if is_ignored_name(target_dir.name):
        raise ValueError("reading work names must remain discoverable")
    containers = {source_dir: target_dir}
    bodies = {}
    opfs = set()
    updates = []
    for item in items:
        source = sources[item.path]
        destination = target_dir
        if source.format is not None and not (
            source.format == MediaFormat.DIR and source.parent_path is None
        ):
            metadata = (
                collection_metadata if item.id == work.id else _reading_metadata(source)
            )
            basename = render_filename(
                template,
                {
                    **metadata,
                    "parent": collection_metadata,
                },
                lib.lib_type,
            )
            if is_ignored_name(basename):
                raise ValueError("reading unit names must remain discoverable")
            if source.parent_path is not None:
                destination /= basename
                containers[source.directory] = destination
            if source.format != MediaFormat.DIR:
                destination /= basename + source.path.suffix
                bodies[source.path] = destination
        data = {
            "id": item.id,
            "path": str(destination),
            "dir": str(destination)
            if source.format in (None, MediaFormat.DIR)
            else str(destination.parent),
            "name": destination.name
            if source.format in (None, MediaFormat.DIR)
            else destination.stem,
        }
        if data["path"] != item.path:
            data.update(index_state=IndexState.PENDING, index_error=None)
        updates.append(data)
        if source.format in (MediaFormat.TXT, MediaFormat.EPUB):
            for path in snapshots:
                if path.parent != source.directory:
                    continue
                if path.name.casefold() in {"content.opf", "metadata.opf"}:
                    opfs.add(path)
                elif path.name.casefold() == f"{source.path.stem}.opf".casefold():
                    opfs.add(path)
                    bodies[path] = destination.with_suffix(path.suffix)
    targets = [
        unicodedata.normalize("NFC", str(path)).casefold()
        for path in containers.values()
    ]
    if len(set(targets)) != len(targets):
        raise ValueError("multiple reading containers render to the same destination")
    for source, target in containers.items():
        _safe_path(root, target)
        if source != target and (target.exists() or target.is_symlink()):
            raise ValueError(f"reading destination already exists: {target}")
    moves = {}
    for path in snapshots:
        container = next(
            parent for parent in (path.parent, *path.parents) if parent in containers
        )
        destination = bodies.get(
            path, containers[container] / path.relative_to(container)
        )
        moves[str(path)] = str(destination)
    edits = _opf_edits(opfs, moves)
    if any(
        _fingerprint(path) != before
        for path, before in (snapshots | directories).items()
    ):
        raise ValueError("reading source changed during organization planning")
    return {
        "moves": [
            {"src": source, "dst": destination, "identity": snapshots[Path(source)]}
            for source, destination in moves.items()
            if source != destination
        ],
        "updates": updates,
        "parent": None,
        "delete_parent": None,
        "mapping": {
            **moves,
            **{str(source): str(target) for source, target in containers.items()},
        },
        "edits": edits,
        "symlinks": [],
        "creates": [],
        "cleanup": str(source_dir),
        "reconcile": str(target_dir),
    }


async def _reading_plan(
    lib: MediaLib, group: list[MediaItem], parent: MediaItem | None
) -> dict:
    """Build a reading plan with shared collision and download safeguards.

    Args:
        lib: Reading library whose lock is held by the caller.
        group: All chapters or the standalone reading item.
        parent: Collection item, or None for a standalone work.

    Returns:
        A journal compatible with the existing organization executor.

    Raises:
        OSError: If source files cannot be inspected.
        ValueError: If metadata, ownership or destinations are invalid.
        OrganizeDeferredError: If affected download transfers have not finished.
    """
    items = [parent, *group] if parent else group
    payload = await asyncio.to_thread(_reading_files, lib, items)
    moves = payload["mapping"]
    file_moves = {move["src"]: move["dst"] for move in payload["moves"]}
    # retain unmoved files when checking links into the source work
    file_moves.update(
        {source: target for source, target in moves.items() if Path(source).is_file()}
    )
    await _validate_moves(Path(lib.dir).absolute(), file_moves)
    await _validate_downloads(
        lib,
        {path for move in payload["moves"] for path in (move["src"], move["dst"])}
        | {edit["path"] for edit in payload["edits"]},
    )
    ids = {item.id for item in items}
    scope = (Path(items[0].dir), Path(payload["reconcile"]))
    for other in await MediaItem.filter(lib_id=lib.id).exclude(id__in=ids):
        if any(Path(other.path).is_relative_to(path) for path in scope):
            raise ValueError(f"reading destination is already indexed: {other.path}")
    await _validate_updates(lib, payload["updates"])
    payload["mapping"] = {
        source: target for source, target in moves.items() if source != target
    }
    return payload


async def _plan(
    lib: MediaLib,
    group: list[MediaItem],
    parent: MediaItem | None,
    *,
    season: int | None = None,
    split: bool = False,
) -> dict:
    """Build a recoverable organization plan while holding the library lock.

    Validate paths, metadata, references, and download ownership before any move.
    Preserve media IDs and shared resources.

    Args:
        lib: Media library containing the naming template and root path.
        group: Nonempty group of media items to organize together.
        parent: Group's directory item, or `None` for a standalone movie.
        season: Resolved group season used for the destination directory and `None`
            keeps the current season.
            missing episode metadata, or `None` to use the parent metadata.
        split: Whether the source directory is being split into season groups. Defaults
            to `False`.

    Raises:
        OSError: If required files cannot be inspected or read.
        etree.LxmlError: If the required parent NFO cannot be parsed.
        OrganizeDeferredError: If an affected download has unfinished transfers.
        ValueError: If the proposed organization is unsafe, would modify an
            original download source or linked NFO, or cannot be saved.

    Returns:
        Journal payload describing file moves, metadata changes, reference
        edits, and source directory cleanup.
    """
    if lib.lib_type in (LibType.NOVEL, LibType.COMIC):
        return await _reading_plan(lib, group, parent)
    root = Path(lib.dir).absolute()
    template = lib.rename_template
    if not template:
        raise ValueError("naming template is not configured")
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
    if is_tv and season is not None:
        # retain the group season if an earlier group edited the parent NFO
        parent_meta["season"] = season
    parent_context = _context(parent_meta, parent_meta if is_tv else None)
    if is_tv:
        for name in ("season", "year"):
            if parent_context.get(name) is None:
                parent_context[name] = getattr(parent, name)
        parent_context["parent"]["year"] = parent_context.get("year")
    target_dir = root / render_directory(template, parent_context, lib.lib_type)
    _safe_path(root, target_dir)

    if not is_tv:
        basename = Path(group[0].path).stem
        videos = [
            file
            for file in source_dir.iterdir()
            if file.is_file()
            and (parent is not None or file.stem == basename)
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
    if target_dir not in (root, source_dir) and target_dir.exists():
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
                # merge only when all destination parents identify the same show
                if target_parent is not None and (
                    not _identity(parent_meta)
                    or (target_parent.nfo_source, target_parent.unique_id)
                    != _identity(parent_meta)
                ):
                    raise ValueError("destination has an unknown media identity")

    moves: dict[str, str] = {}
    if parent and target_dir != source_dir and not split:
        # keep other seasons in place while moving this directory's companion files
        blocked = set()
        ids = {item.id for item in group} | {parent.id}
        for other in await MediaItem.filter(lib_id=lib.id):
            other_path = Path(other.path)
            if other.id not in ids and other_path.is_relative_to(source_dir):
                relative = other_path.relative_to(source_dir)
                if len(relative.parts) > 1:
                    blocked.add(relative.parts[0])
        if is_tv:
            for child in source_dir.iterdir():
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and any(
                        file.is_file()
                        and (mimetypes.guess_file_type(file)[0] or "").startswith(
                            "video/"
                        )
                        for file in child.iterdir()
                    )
                ):
                    blocked.add(child.name)
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
    source_nfos = {}
    for item in group:
        old = Path(item.path)
        _safe_path(root, old)
        metadata = {} if is_tv else parent_meta
        own_nfo = Path(item.nfo_path or str(old.with_suffix(".nfo")))
        if is_tv and parent is not None:
            try:
                _safe_path(root, own_nfo)
                metadata = await asyncio.to_thread(
                    _metadata, own_nfo, lib.lib_type, "episodedetails"
                )
                context = _context(metadata, parent_meta)
                if metadata.get("season") is None and item.season is not None:
                    context["season"] = item.season
                if context.get("season") is None:
                    context["season"] = parent.season
                destination = target_dir / (
                    render_filename(template, context, lib.lib_type) + old.suffix
                )
            except (OSError, ValueError, etree.LxmlError):
                # the parent NFO may arrive before the episode scraper finishes
                destination = target_dir / old.name
        else:
            destination = root / render_path(template, parent_context, lib.lib_type)
            destination = destination.with_name(destination.name + old.suffix)
            if target_dir.is_dir() and any(
                file != old
                and (target_dir != root or file.stem == destination.stem)
                and file.is_file()
                and (mimetypes.guess_file_type(file)[0] or "").startswith("video/")
                for file in target_dir.iterdir()
            ):
                raise ValueError("destination contains another movie video")
        moves[str(old)] = str(destination)
        for companion in _companions(old):
            if companion == parent_nfo:
                continue
            moves[str(companion)] = str(
                destination.parent
                / (destination.stem + companion.name[len(old.stem) :])
            )
        cache = old.parent / f".{old.stem}.json"
        if (not item.danmaku_path or Path(item.danmaku_path) == cache) and (
            cache.is_file() or cache.is_symlink()
        ):
            moves[str(cache)] = str(destination.parent / f".{destination.stem}.json")
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
            for name, field in _METADATA.items():
                value = parent_meta.get(field)
                if value is not None:
                    data[name] = str(value) if name == "rating" else value
            data["nfo_path"] = str(destination.with_suffix(".nfo"))
        elif not parent and target_dir != root:
            data["nfo_path"] = None
        if is_tv and parent is not None:
            if metadata:
                for name, field in _METADATA.items():
                    value = metadata.get(field)
                    if value is not None or name not in ("year", "season", "episode"):
                        data[name] = (
                            str(value)
                            if name == "rating" and value is not None
                            else value
                        )
                data["nfo_path"] = moves.get(str(own_nfo), str(own_nfo))
                source_nfos[item.id] = own_nfo
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
    copy_parent_nfo = split and parent_nfo != target_nfo
    if reuse_nfo or copy_parent_nfo:
        # preserve an existing NFO even when its external ID matches
        moves.pop(str(parent_nfo), None)
        if reuse_nfo:
            moves[str(target_nfo)] = str(target_nfo)
    else:
        moves[str(parent_nfo)] = str(target_nfo)

    artwork = {
        Path(source)
        for source, destination in moves.items()
        if source != destination
        and (mimetypes.guess_file_type(source)[0] or "").startswith("image/")
    }
    for field in ("poster", "backdrop"):
        value = parent_meta.get(field)
        if value and not urlsplit(value).scheme and not value.startswith("//"):
            path = Path(os.path.normpath(parent_nfo.parent / value))
            if path.is_file() and path.is_relative_to(root):
                # include artwork outside a flat movie's basename companions
                if parent is None:
                    moves[str(path)] = str(target_dir / path.name)
                if str(path) in moves and moves[str(path)] != str(path):
                    artwork.add(path)
    if artwork:
        nfo_paths = {
            Path(source)
            for source in moves
            if Path(source).suffix.lower() == ".nfo" and not Path(source).is_symlink()
        }
        for path in await asyncio.to_thread(_shared_artwork, root, artwork, nfo_paths):
            moves.pop(str(path))

    parent_nfo_key = unicodedata.normalize("NFC", str(target_nfo)).casefold()
    for source, destination in moves.items():
        _safe_path(root, Path(source))
        _safe_path(root, Path(destination))
        if (
            is_tv
            and (mimetypes.guess_file_type(source)[0] or "").startswith("video/")
            and unicodedata.normalize(
                "NFC", str(Path(destination).with_suffix(".nfo"))
            ).casefold()
            == parent_nfo_key
        ):
            raise ValueError("episode and parent NFO names conflict")
    changing = {src: dst for src, dst in moves.items() if src != dst}
    await _validate_moves(root, moves)
    targets = [
        unicodedata.normalize("NFC", path).casefold() for path in changing.values()
    ]

    for item, data in zip(group, updates, strict=True):
        for name in ("nfo_path", "danmaku_path"):
            value = getattr(item, name)
            if name in data or not value:
                continue
            if value in moves:
                data[name] = moves[value]
            elif not Path(value).exists() and not Path(value).is_symlink():
                data[name] = None
                if name == "nfo_path":
                    data["nfo_mtime"] = None
        old_nfo = source_nfos.get(item.id) or Path(item.nfo_path or parent_nfo)
        new_nfo = Path(
            data.get("nfo_path", item.nfo_path)
            or moves.get(str(old_nfo), str(Path(data["path"]).with_suffix(".nfo")))
        )
        for name in ("poster", "backdrop"):
            value = data.get(name, getattr(item, name))
            updated = _relocate_reference(value, old_nfo, new_nfo, moves)
            if updated != value:
                data[name] = updated

    parent_data = None
    if target_dir != root:
        # keep stored metadata and modification time aligned with the retained NFO
        if reuse_nfo:
            assert target_meta is not None
            stored_meta = target_meta
        else:
            stored_meta = parent_meta
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
        for name, field in _METADATA.items():
            value = stored_meta.get(field)
            if value is not None or reuse_nfo:
                parent_data[name] = (
                    str(value) if name == "rating" and value is not None else value
                )
        if is_tv and parent is not None:
            parent_data["season"] = parent_context.get("season")
            if reuse_nfo:
                existing_season = stored_meta.get("season")
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
    aliases: dict[str, list[tuple[str, str]]] = {}
    for source, destination in changing.items():
        aliases.setdefault(_reference_key(Path(source)), []).append(
            (source, destination)
        )
    symlinks = []
    for source, destination in moves.items():
        if Path(source).is_symlink():
            link = os.readlink(source)
            target = os.path.normpath(Path(source).parent / link)
            relocated = moves.get(target)
            if relocated is None and (
                candidates := aliases.get(_reference_key(Path(target)))
            ):
                identity = _fingerprint(Path(target))
                matches = {
                    dst
                    for src, dst in candidates
                    if _fingerprint(Path(src)) == identity
                }
                if len(matches) > 1:
                    raise ValueError(f"ambiguous symlink target: {target}")
                relocated = next(iter(matches), None)
            if source == destination and relocated in (None, target):
                continue
            target = relocated or target
            target = (
                target
                if os.path.isabs(link)
                else os.path.relpath(target, Path(destination).parent)
            )
            if target != link:
                if source == destination and not os.access(
                    Path(source).parent, os.W_OK | os.X_OK
                ):
                    raise ValueError("media directory is not writable")
                symlinks.append(
                    {
                        "path": destination,
                        "before": link,
                        "target": target,
                    }
                )
    creates = []
    seasons = (
        {str(target_nfo): parent_data.get("season")} if is_tv and parent_data else None
    )
    if copy_parent_nfo and not reuse_nfo:
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
        creates.append(
            {
                "path": str(target_nfo),
                "content": copied_content,
                "mode": parent_nfo.stat().st_mode & 0o777,
            }
        )
    nfo_edits = await asyncio.to_thread(_nfo_edits, moves, seasons)
    linked_nfos = {
        destination
        for source, destination in moves.items()
        if Path(source).is_symlink()
    }
    if any(edit["path"] in linked_nfos for edit in nfo_edits):
        raise ValueError("organization would modify a linked NFO")
    affected_paths = set(changing) | set(changing.values())
    affected_paths.update(link["path"] for link in symlinks)
    affected_paths.update(edit["path"] for edit in nfo_edits)
    affected_paths.update(create["path"] for create in creates)
    await _validate_downloads(lib, affected_paths)
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
        "nfo_edits": nfo_edits,
        "symlinks": symlinks,
        "creates": creates,
        "cleanup": str(source_dir) if parent and source_dir != target_dir else None,
    }


def _move_files(root: Path, payload: dict):
    """Apply or resume a journal's filesystem changes under the library lock.

    Preserve recorded permissions when publishing copied NFO files. Accept legacy
    journals without a recorded `mode`, retaining their private temporary-file mode.

    Args:
        root: Absolute media library root.
        payload: Persisted organization plan with file identities and edits.

    Raises:
        OrganizePendingError: If a file changed or disappeared unexpectedly.
        ValueError: If a journal path is no longer safe to access.
        OSError: If a filesystem operation fails or a destination already exists.
    """
    edits = payload.get("edits", payload.get("nfo_edits", []))
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
            # allow rewritten metadata to have a new inode when its contents match
            edit = next(
                (edit for edit in edits if edit["path"] == str(destination)),
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
                if (mode := create.get("mode")) is not None:
                    os.fchmod(stream.fileno(), mode)
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
    for edit in edits:
        path = Path(edit["path"])
        _safe_path(root, path)
        current = path.read_bytes()
        content = edit["content"].encode()
        if current == content:
            continue
        if hashlib.sha256(current).hexdigest() != edit["before"]:
            raise OrganizePendingError(f"metadata changed during organization: {path}")

        # replace only the original metadata contents verified against the journal
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


async def _finish(lib: MediaLib, event: MediaEvent) -> dict[str, str]:
    """Finish a journal's filesystem and database updates under the library lock.

    Keep episode NFO files scannable if the journal lacks full metadata, including
    legacy plans. Retain filesystem events for reused sources or destinations
    changed by external writers before the database commit.
    Preserve existing original snapshots and capture file information for new parents.

    Args:
        lib: Media library whose lock is held by the caller.
        event: Persisted organization event containing the plan to finish.

    Raises:
        OrganizePendingError: If the plan cannot be completed and needs recovery.

    Returns:
        Mapping from original media paths to their organized destinations.
    """
    payload = event.payload
    try:
        if payload is None:
            raise ValueError("organization event has no payload")
        await write_in_thread(_move_files, Path(lib.dir).absolute(), payload)
        async with in_transaction("default"):
            parent = payload["parent"]
            parent_id = None
            if parent:
                data = {key: value for key, value in parent.items() if key != "id"}
                data["nfo_mtime"] = datetime.fromtimestamp(
                    Path(data["nfo_path"]).stat().st_mtime, tz=UTC
                )
                if parent["id"] is None:
                    path = data.pop("path")
                    row, _ = await MediaItem.get_or_create(
                        lib_id=lib.id,
                        path=path,
                        defaults={
                            **data,
                            "original": await asyncio.to_thread(
                                original_file_info, path
                            ),
                        },
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
                    if lib.lib_type == LibType.TV_SHOW and "title" not in data:
                        data["nfo_mtime"] = None
                    else:
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
            if directory := payload.get("reconcile"):
                await MediaEvent.create(
                    lib_id=lib.id,
                    event_type="modified",
                    src_path=directory,
                    is_directory=True,
                )
            await event.delete()
        if payload.get("reconcile"):
            notify_media_events(lib.id)
        cleanup = payload.get("cleanup")
        if cleanup:
            await write_in_thread(_remove_empty_directories, Path(cleanup))
        return payload["mapping"]
    except Exception as error:
        raise OrganizePendingError(
            f"organization {event.id} needs recovery "
            f"before library {lib.id} can continue"
        ) from error


def _remove_empty_directories(path: Path):
    """Remove empty directories from the source tree after organization.

    Args:
        path: Source directory to clean up from its leaves to its root.
    """
    if not path.exists():
        return
    for directory, _, _ in os.walk(path, topdown=False, followlinks=False):
        with suppress(OSError):
            Path(directory).rmdir()


async def recover_organizing(lib: MediaLib) -> dict[str, str]:
    """Finish persisted plans before accepting filesystem events or scan results.

    Caller must hold the library lock throughout recovery.

    Args:
        lib: Media library whose pending organization events are recovered.

    Raises:
        OrganizePendingError: If a persisted plan cannot be completed.

    Returns:
        Combined mapping from original paths to recovered destinations.
    """
    mapping = {}
    for event in await MediaEvent.filter(lib_id=lib.id, event_type="organize"):
        mapping.update(await _finish(lib, event))
    return mapping


async def _season_groups(
    lib: MediaLib, group: list[MediaItem], parent: MediaItem | None
) -> list[tuple[int | None, list[MediaItem]]]:
    """Group episodes by season when the directory template uses a season field.

    Args:
        lib: Media library containing the directory naming template.
        group: Video items sharing the source directory.
        parent: Directory item whose NFO and stored season supply fallback
            metadata, if any.

    Raises:
        ValueError: If splitting requires unknown seasons or would leave
            videos without index records in the source directory.

    Returns:
        Pairs of season numbers and item lists, or one pair with a `None` season
        when the template does not require grouping by season.
    """
    template = lib.rename_template
    if (
        lib.lib_type != LibType.TV_SHOW
        or parent is None
        or not template
        or not re.search(r"{{\s*season\s*}}", template.rsplit("/", 1)[0])
    ):
        return [(None, group)]
    parent_season = parent.season
    try:
        parent_metadata = await asyncio.to_thread(
            _metadata,
            Path(parent.nfo_path or Path(parent.path) / f"{parent.name}.nfo"),
            lib.lib_type,
            "tvshow",
        )
        if parent_metadata.get("season") is not None:
            parent_season = parent_metadata["season"]
    except (OSError, ValueError, etree.LxmlError):
        pass
    groups = {}
    for item in group:
        season = item.season if item.season is not None else parent_season
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


async def _group_of(
    lib: MediaLib, item: MediaItem
) -> tuple[MediaItem | None, list[MediaItem]]:
    """Find the directory item and videos organized together with an item.

    Args:
        lib: Library containing the item.
        item: Video, directory or reading item.

    Returns:
        The group's directory item, if any, and its members.
    """
    parent = None
    if item.parent_id is not None:
        parent = await MediaItem.filter(id=item.parent_id).first()
    elif Path(item.path).is_dir() and (
        lib.lib_type not in (LibType.NOVEL, LibType.COMIC) or item.format is None
    ):
        parent = item
    group = (
        await MediaItem.filter(parent_id=parent.id, lib_id=lib.id) if parent else [item]
    )
    return parent, group


async def preview_organization(
    lib: MediaLib, template: str, item: MediaItem
) -> dict[str, Any]:
    """Plan one work under a template as organization would, changing nothing.

    Uses the organizer's own grouping, path rendering and conflict checks on an
    unsaved copy of the library. Recovery, transfer backfill and journals are left
    out, and the library lock is not held, so results describe the moment of the
    call rather than a guaranteed future organization.

    Args:
        lib: Video library; it is not modified.
        template: Validated naming template to plan with.
        item: Top-level work to plan.

    Returns:
        `status` (`moves`, `unchanged`, `deferred`, `skipped` or `blocked`),
        videos' current and proposed `paths`, the planned `moved_files` count and
        an `issue` naming why a group would not be organized.
    """
    proposed = copy(lib)
    proposed.rename_template = template
    parent, group = await _group_of(proposed, item)
    paths: list[dict[str, str]] = []
    moved_files = 0
    changed = False
    status = None
    issue = None
    try:
        groups = await _season_groups(proposed, group, parent) if group else []
    except ValueError as error:
        groups, status, issue = [], "blocked", str(error)
    except (OSError, etree.LxmlError):
        groups, status = [], "skipped"
        issue = "metadata is missing or unreadable"
    for season, subgroup in groups:
        if len(groups) > 1:
            # organization saves these seasons first; this copy stays in memory
            for row in subgroup:
                if row.season is None:
                    row.season = season
        try:
            payload = await _plan(
                proposed, subgroup, parent, season=season, split=len(groups) > 1
            )
        except OrganizeDeferredError as error:
            status, issue = "deferred", str(error)
            continue
        except ValueError as error:
            status, issue = "blocked", str(error)
            continue
        except (OSError, etree.LxmlError):
            status = status or "skipped"
            issue = issue or "metadata is missing or unreadable"
            continue
        moved_files += len(payload["moves"])
        changed = changed or any(
            payload.get(key) for key in ("moves", "creates", "nfo_edits", "edits")
        )
        current = {row.id: row.path for row in subgroup}
        paths.extend(
            {"current": current[update["id"]], "proposed": update["path"]}
            for update in payload["updates"]
        )
    return {
        "status": status or ("moves" if changed else "unchanged"),
        "paths": paths,
        "moved_files": moved_files,
        "issue": issue,
    }


async def organize_items(lib: MediaLib, item_ids: list[int]) -> dict[str, str]:
    """Organize metadata-backed groups while holding the library lock throughout.

    Recover pending plans, skip invalid groups, and continue past deferred transfers.
    Save inherited seasons before splitting so retries survive parent NFO edits.

    Args:
        lib: Media library whose lock is held by the caller.
        item_ids: Media item IDs identifying groups to organize.

    Raises:
        OrganizePendingError: If a persisted organization plan cannot finish.
        OrganizeDeferredError: If any group is waiting for its remaining transfers.

    Returns:
        Combined mapping of paths changed by recovery and new plans.
    """
    mapping = await recover_organizing(lib)
    if not lib.rename_template or not item_ids:
        return mapping
    from app.core.dl.syncer import backfill_transfer_targets

    await backfill_transfer_targets(lib)
    processed = set()
    deferred = None
    for item_id in item_ids:
        item = await MediaItem.filter(id=item_id, lib_id=lib.id).first()
        if item is None:
            continue
        parent, group = await _group_of(lib, item)
        key = parent.id if parent else item.id
        if key in processed:
            continue
        processed.add(key)
        if not group:
            continue
        try:
            groups = await _season_groups(lib, group, parent)
        except (OSError, ValueError, etree.LxmlError) as error:
            logger.warning(
                "Skipping automatic organization for %s: %s", item.path, error
            )
            continue
        if len(groups) > 1:
            for season, subgroup in groups:
                await MediaItem.filter(
                    id__in=[row.id for row in subgroup], season=None
                ).update(season=season)
        for season, subgroup in groups:
            try:
                payload = await _plan(
                    lib, subgroup, parent, season=season, split=len(groups) > 1
                )
                if not any(
                    payload.get(key)
                    for key in ("moves", "creates", "nfo_edits", "edits")
                ):
                    continue
            except OrganizeDeferredError as error:
                deferred = error
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
    if deferred is not None:
        raise deferred
    return mapping


async def plan_relocation(
    lib: MediaLib,
    item: MediaItem,
    destination: Path,
    *,
    parent_id: int | None = None,
) -> dict:
    """Plan giving one indexed video a new name or directory inside its library.

    The video keeps its extension. Its same-stem NFO, `stem.*` subtitles,
    `stem-*` artwork and danmaku cache go with it under the new stem, and local
    references in the NFO and the index follow them.

    Args:
        lib: Video library whose lock the caller holds when applying.
        item: Indexed video file to relocate.
        destination: Absolute new path of the video, with its extension.
        parent_id: Directory item owning the destination when the video changes
            directory; `None` keeps the current parent, as a rename does.

    Returns:
        A journal compatible with the existing organization executor. A video
        that leaves its directory names its former parent for removal once that
        parent has no children left.

    Raises:
        OSError: If files cannot be inspected or read.
        OrganizeDeferredError: If an affected download has unfinished transfers.
        ValueError: If the change is unsafe, conflicts with an existing file,
            involves a symlink or would modify an original download source.
    """
    root = Path(lib.dir).absolute()
    old = Path(item.path)
    _safe_path(root, old)
    if item.path == item.dir or not old.is_file():
        raise ValueError("only an indexed video file can be relocated")
    if destination.suffix != old.suffix:
        raise ValueError("a relocated video keeps its extension")
    folder, stem = destination.parent, destination.stem
    moves = {str(old): str(destination)}
    for companion in _companions(old):
        moves[str(companion)] = str(folder / (stem + companion.name[len(old.stem) :]))
    for sibling in old.parent.iterdir():
        if (
            sibling.name.startswith(f"{old.stem}-")
            and sibling.is_file()
            and (mimetypes.guess_file_type(sibling)[0] or "").startswith("image/")
        ):
            moves[str(sibling)] = str(folder / (stem + sibling.name[len(old.stem) :]))
    cache = old.parent / f".{old.stem}.json"
    if cache.is_file() or cache.is_symlink():
        moves[str(cache)] = str(folder / f".{stem}.json")
    if any(Path(source).is_symlink() for source in moves):
        raise ValueError("relocating a linked file is not supported")
    await _validate_moves(root, moves)

    own_nfo = Path(item.nfo_path) if item.nfo_path else old.with_suffix(".nfo")
    new_nfo = Path(moves.get(str(own_nfo), str(own_nfo)))
    data: dict[str, Any] = {
        "id": item.id,
        "path": str(destination),
        "dir": str(folder),
        "name": stem,
    }
    moving = folder != old.parent
    if moving:
        data["parent_id"] = parent_id
    for name in ("nfo_path", "danmaku_path"):
        value = getattr(item, name)
        if value in moves:
            data[name] = moves[value]
    for name in ("poster", "backdrop"):
        value = getattr(item, name)
        updated = _relocate_reference(value, own_nfo, new_nfo, moves)
        if updated != value:
            data[name] = updated
    updates = [data]
    # a movie folder is registered on its video's NFO
    for other in await MediaItem.filter(lib_id=lib.id, nfo_path__in=list(moves)):
        if other.id != item.id:
            updates.append(
                {"id": other.id, "path": other.path, "nfo_path": moves[other.nfo_path]}
            )
    nfo_edits = await asyncio.to_thread(_nfo_edits, moves)
    await _validate_downloads(
        lib, set(moves) | set(moves.values()) | {edit["path"] for edit in nfo_edits}
    )
    await _validate_updates(lib, updates)
    return {
        "moves": [
            {"src": src, "dst": dst, "identity": _fingerprint(Path(src))}
            for src, dst in moves.items()
        ],
        "updates": updates,
        "parent": None,
        "delete_parent": item.parent_id if moving else None,
        "mapping": dict(moves),
        "nfo_edits": nfo_edits,
        "symlinks": [],
        "creates": [],
        "cleanup": str(old.parent) if moving and old.parent != root else None,
    }


async def _journal(lib: MediaLib, item: MediaItem, payload: dict) -> dict[str, str]:
    """Persist a plan and finish it under the library lock held by the caller.

    Args:
        lib: Video library whose lock is held by the caller.
        item: Item the plan starts from.
        payload: Journal built by a planner in this module.

    Returns:
        Mapping from original paths to their new paths.

    Raises:
        OrganizePendingError: If the persisted plan cannot finish.
    """
    event = await MediaEvent.create(
        lib_id=lib.id,
        event_type="organize",
        src_path=item.path,
        dest_path=None,
        is_directory=item.path == item.dir,
        payload=payload,
    )
    return await _finish(lib, event)


async def rename_item(lib: MediaLib, item: MediaItem, stem: str) -> dict[str, str]:
    """Rename one indexed video with its companions through a recoverable journal.

    Args:
        lib: Video library whose lock is held by the caller.
        item: Indexed video file to rename.
        stem: New file name without its extension.

    Returns:
        Mapping from original paths to their new paths.

    Raises:
        OSError: If files cannot be inspected or read.
        OrganizeDeferredError: If an affected download has unfinished transfers.
        OrganizePendingError: If the persisted plan cannot finish.
        ValueError: If the rename is unsafe or conflicts with an existing file.
    """
    old = Path(item.path)
    payload = await plan_relocation(lib, item, old.with_name(stem + old.suffix))
    return await _journal(lib, item, payload)


async def move_episode(
    lib: MediaLib, item: MediaItem, directory: Path
) -> dict[str, str]:
    """Move one indexed episode and its companions into another show directory.

    The destination's directory item is reused, or created from the episode's
    current parent when the directory is new to the index. The former parent is
    removed once its last episode has left; its own NFO and artwork stay on disk.

    Args:
        lib: TV library whose lock is held by the caller.
        item: Indexed episode to move.
        directory: Absolute series or season directory to move it into.

    Returns:
        Mapping from original paths to their new paths.

    Raises:
        OSError: If files cannot be inspected or read.
        OrganizeDeferredError: If an affected download has unfinished transfers.
        OrganizePendingError: If the persisted plan cannot finish.
        ValueError: If the move is unsafe or conflicts with an existing file.
    """
    from app.core.media.handlers.tvshow import episode_location

    root = Path(lib.dir).absolute()
    location = episode_location(
        (*directory.relative_to(root).parts, Path(item.path).name)
    )
    if location is None:
        raise ValueError("the destination is not a series or season directory")
    target = await MediaItem.filter(lib_id=lib.id, path=str(directory)).first()
    # plan before registering a new directory so a refused move leaves no record
    payload = await plan_relocation(
        lib,
        item,
        directory / Path(item.path).name,
        parent_id=target.id if target else 0,
    )
    if target is None:
        source = await MediaItem.filter(id=item.parent_id).first()
        target, _ = await MediaItem.get_or_create(
            lib_id=lib.id,
            path=str(directory),
            defaults={
                "dir": str(directory),
                "name": directory.name,
                "title": source.title if source else None,
                "year": source.year if source else None,
                "season": location[0],
                "visible": source.visible if source else True,
            },
        )
        for update in payload["updates"]:
            if update["id"] == item.id:
                update["parent_id"] = target.id
    return await _journal(lib, item, payload)


async def plan_directory_move(
    lib: MediaLib, parent: MediaItem, directory: Path
) -> dict:
    """Plan moving a whole work directory to a new, unused directory.

    Every file below the directory moves with it, the directory item keeps its
    identity, and every item below it is rebased.

    Args:
        lib: Video library whose lock the caller holds when applying.
        parent: Directory item to move.
        directory: Absolute destination, which must not exist and must sit at
            the same depth as the source.

    Returns:
        A journal compatible with the existing organization executor.

    Raises:
        OSError: If files cannot be inspected or read.
        OrganizeDeferredError: If an affected download has unfinished transfers.
        ValueError: If the move is unsafe, the destination exists or is indexed,
            the depth changes, or the directory holds a symlink.
    """
    root = Path(lib.dir).absolute()
    source = Path(parent.path)
    _safe_path(root, source)
    _safe_path(root, directory)
    if parent.path != parent.dir or not source.is_dir():
        raise ValueError("only a directory item can be moved as a whole")
    if directory.exists() or directory.is_symlink():
        raise ValueError(f"destination already exists: {directory}")
    if len(directory.relative_to(root).parts) != len(source.relative_to(root).parts):
        raise ValueError("a moved directory keeps its depth in the library")
    if directory.is_relative_to(source):
        raise ValueError("a directory cannot move into itself")
    moves: dict[str, str] = {}
    for current, dirs, files in os.walk(source, followlinks=False):
        base = Path(current)
        for name in (*dirs, *files):
            if (base / name).is_symlink():
                raise ValueError("moving a directory with links is not supported")
        for name in files:
            path = base / name
            moves[str(path)] = str(directory / path.relative_to(source))
    # the work's own NFO is named after its directory
    own = source / f"{source.name}.nfo"
    if str(own) in moves:
        moves[str(own)] = str(directory / f"{directory.name}.nfo")
    await _validate_moves(root, moves)

    def rebase(value: str | None) -> str | None:
        """Rebase a stored path below the source onto the destination.

        Args:
            value: Stored absolute path, if any.

        Returns:
            The moved path, or the value unchanged when it is not below the source.
        """
        if not value:
            return value
        if value in moves:
            return moves[value]
        path = Path(value)
        if path == source or path.is_relative_to(source):
            return str(directory / path.relative_to(source))
        return value

    updates = []
    for row in await MediaItem.filter(lib_id=lib.id):
        path = Path(row.path)
        if not (path == source or path.is_relative_to(source)):
            continue
        data: dict[str, Any] = {
            "id": row.id,
            "path": rebase(row.path),
            "dir": rebase(row.dir),
        }
        if row.id == parent.id:
            data["name"] = directory.name
        for name in ("nfo_path", "danmaku_path"):
            value = getattr(row, name)
            if value and rebase(value) != value:
                data[name] = rebase(value)
        updates.append(data)
    nfo_edits = await asyncio.to_thread(_nfo_edits, moves)
    await _validate_downloads(
        lib, set(moves) | set(moves.values()) | {edit["path"] for edit in nfo_edits}
    )
    await _validate_updates(lib, updates)
    return {
        "moves": [
            {"src": src, "dst": dst, "identity": _fingerprint(Path(src))}
            for src, dst in moves.items()
        ],
        "updates": updates,
        "parent": None,
        "delete_parent": None,
        "mapping": {**moves, str(source): str(directory)},
        "nfo_edits": nfo_edits,
        "symlinks": [],
        "creates": [],
        "cleanup": str(source),
    }


async def move_directory(
    lib: MediaLib, parent: MediaItem, directory: Path
) -> dict[str, str]:
    """Move a whole work directory through a recoverable journal.

    Args:
        lib: Video library whose lock is held by the caller.
        parent: Directory item to move.
        directory: Absolute destination that does not exist yet.

    Returns:
        Mapping from original paths to their new paths.

    Raises:
        OSError: If files cannot be inspected or read.
        OrganizeDeferredError: If an affected download has unfinished transfers.
        OrganizePendingError: If the persisted plan cannot finish.
        ValueError: If the move is unsafe or the destination is taken.
    """
    payload = await plan_directory_move(lib, parent, directory)
    return await _journal(lib, parent, payload)
