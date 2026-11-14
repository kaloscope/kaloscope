"""Select and classify library files for reviewed batches without changing them."""

import mimetypes
import os
import re
import stat
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from app.core.media.handlers.reading import (
    COVER_NAMES,
    IMAGE_EXTENSIONS,
    is_ignored_name,
)
from app.core.media.naming import _RESERVED, _UNSAFE
from app.core.media.organizer import (
    _SUBTITLES,
    _fingerprint,
    _reference_key,
    _safe_path,
)
from app.core.renderer import is_template, render
from app.models.media import FileOwner, LibType, MediaFormat, MediaItem, MediaLib
from app.utils.extractor import (
    extract_episode,
    extract_season,
    extract_title,
    extract_year,
)

# a review answers within the tool timeout, so a walk stops at either bound
SCAN_LIMIT = 200_000
SCAN_SECONDS = 10
BATCH_LIMIT = 2000
SAMPLE_ROWS = 20
PATH_CHARS = 160
READING_TYPES = (LibType.NOVEL, LibType.COMIC)
_ARTWORK = (*COVER_NAMES, "fanart", "backdrop", "banner", "thumb", "landscape", "logo")
_NOVEL_BODIES = {".txt", ".epub"}
_COMIC_BODIES = {".cbz", ".zip"}
_NOVEL_METADATA = {"content.opf", "metadata.opf"}
# video containers the standard MIME table does not classify as video
_VIDEO_EXTENSIONS = {
    ".rmvb",
    ".rm",
    ".ts",
    ".m2ts",
    ".mts",
    ".vob",
    ".wmv",
    ".flv",
    ".f4v",
    ".divx",
    ".ogm",
    ".asf",
    ".3gp",
    ".iso",
}
_ARCHIVES = (MediaFormat.TXT, MediaFormat.EPUB, MediaFormat.CBZ, MediaFormat.ZIP)


@dataclass(frozen=True)
class LibraryIndex:
    """Snapshot the index facts that decide which item owns a file."""

    root: Path
    lib_type: LibType
    # body file path -> item ID
    bodies: dict[str, int] = field(default_factory=dict)
    # directory item or collection path -> item ID
    directories: dict[str, int] = field(default_factory=dict)
    # directory comic path -> item ID
    page_dirs: dict[str, int] = field(default_factory=dict)
    # registered metadata, cache and artwork path -> item ID
    owned: dict[str, int] = field(default_factory=dict)
    # directory holding a file body -> (item ID, body stem)
    containers: dict[str, tuple[int, str]] = field(default_factory=dict)


@dataclass(frozen=True)
class FileMatch:
    """Describe one matched file as it was when the library was walked."""

    path: Path
    size: int
    owner: FileOwner
    item_id: int | None
    identity: list[int]


@dataclass
class FileScan:
    """Collect a bounded walk's matches and the facts needed to guard them."""

    matches: list[FileMatch] = field(default_factory=list)
    truncated: bool = False
    # normalized targets of library symlinks
    links: set[str] = field(default_factory=set)
    # directory comic item ID -> its page count
    pages: dict[int, int] = field(default_factory=dict)


@dataclass
class PlanEntry:
    """Describe one unit a batch would apply, or a match it will not touch."""

    kind: str
    owner: FileOwner
    src: str
    size: int | None
    identity: list[Any] | None
    item_id: int | None = None
    files: int = 1
    status: str = "planned"
    reason: str | None = None
    dst: str | None = None


def build_index(lib: MediaLib, items: list[MediaItem]) -> LibraryIndex:
    """Project a library's items into path lookups for classification.

    Args:
        lib: Library being walked.
        items: Every item of that library.

    Returns:
        Lookups for bodies, directories, registered companions and containers.
    """
    index = LibraryIndex(Path(lib.dir).absolute(), lib.lib_type)
    reading = lib.lib_type in READING_TYPES
    for item in items:
        if reading:
            if item.format in _ARCHIVES:
                index.bodies[item.path] = item.id
                index.containers[item.dir] = (item.id, Path(item.path).stem)
            elif item.format == MediaFormat.DIR:
                index.page_dirs[item.path] = item.id
            else:
                index.directories[item.path] = item.id
            continue
        if item.path == item.dir:
            index.directories[item.path] = item.id
        else:
            index.bodies[item.path] = item.id
        for value in (item.nfo_path, item.danmaku_path):
            if value:
                index.owned[value] = item.id
        base = Path(item.nfo_path).parent if item.nfo_path else Path(item.dir)
        for value in (item.poster, item.backdrop):
            if value and not urlsplit(value).scheme and not value.startswith("//"):
                index.owned[os.path.normpath(base / value)] = item.id
    return index


def _is_video(name: str) -> bool:
    """Check whether a file name has a video type.

    Args:
        name: File name.

    Returns:
        Whether its guessed MIME type is a video type or its extension is a
        video container the library does not index, which must not look loose.
    """
    return (mimetypes.guess_file_type(name)[0] or "").startswith("video/") or Path(
        name
    ).suffix.casefold() in _VIDEO_EXTENSIONS


def _video_owners(
    index: LibraryIndex, directory: Path, names: list[str]
) -> dict[str, tuple[FileOwner, int | None]]:
    """Classify one movie or TV directory's files.

    Args:
        index: Library lookups.
        directory: Directory being classified.
        names: Its file names.

    Returns:
        Each name's class and owning item, following the companion rules used
        by organization and deletion: same-stem NFO files, `stem.*` subtitles,
        danmaku caches and artwork beside indexed videos or in item directories.
    """
    base = str(directory)
    videos = {name for name in names if _is_video(name)}
    indexed = {
        Path(name).stem: index.bodies[f"{base}/{name}"]
        for name in videos
        if f"{base}/{name}" in index.bodies
    }
    stems = sorted((Path(name).stem for name in videos), key=len, reverse=True)
    holder = index.directories.get(base)
    if holder is None and indexed:
        holder = next(iter(indexed.values()))
    result: dict[str, tuple[FileOwner, int | None]] = {}
    for name in names:
        path = f"{base}/{name}"
        stem, suffix = Path(name).stem, Path(name).suffix.casefold()
        if path in index.bodies:
            result[name] = (FileOwner.INDEXED, index.bodies[path])
        elif name in videos:
            result[name] = (FileOwner.UNINDEXED, None)
        elif path in index.owned:
            result[name] = (FileOwner.COMPANION, index.owned[path])
        elif suffix == ".nfo" and (stem in indexed or stem == directory.name):
            owner = indexed.get(stem, index.directories.get(base))
            result[name] = (
                (FileOwner.COMPANION, owner)
                if owner is not None
                else (FileOwner.LOOSE, None)
            )
        elif suffix in _SUBTITLES and (
            video := next((s for s in stems if name.startswith(f"{s}.")), None)
        ):
            result[name] = (
                (FileOwner.COMPANION, indexed[video])
                if video in indexed
                else (FileOwner.LOOSE, None)
            )
        elif suffix == ".json" and name.startswith(".") and stem[1:] in indexed:
            result[name] = (FileOwner.COMPANION, indexed[stem[1:]])
        elif (
            suffix in IMAGE_EXTENSIONS
            and holder is not None
            and (
                stem.casefold() in _ARTWORK
                or any(stem.startswith(f"{video}-") for video in indexed)
            )
        ):
            result[name] = (FileOwner.COMPANION, holder)
        else:
            result[name] = (FileOwner.LOOSE, None)
    return result


def _reading_owners(
    index: LibraryIndex, directory: Path, names: list[str]
) -> dict[str, tuple[FileOwner, int | None]]:
    """Classify one novel or comic directory's files.

    Args:
        index: Library lookups.
        directory: Directory being classified.
        names: Its file names.

    Returns:
        Each name's class and owning item. Names discovery ignores are loose;
        content types the library reads are never loose.
    """
    base = str(directory)
    novel = index.lib_type == LibType.NOVEL
    hidden = any(
        is_ignored_name(part) for part in directory.relative_to(index.root).parts
    )
    container = index.containers.get(base)
    page_item = index.page_dirs.get(base)
    holder = (
        container[0]
        if container
        else page_item
        if page_item is not None
        else index.directories.get(base)
    )
    result: dict[str, tuple[FileOwner, int | None]] = {}
    for name in names:
        path = f"{base}/{name}"
        stem, suffix = Path(name).stem.casefold(), Path(name).suffix.casefold()
        image = suffix in IMAGE_EXTENSIONS
        if path in index.bodies:
            result[name] = (FileOwner.INDEXED, index.bodies[path])
        elif hidden or is_ignored_name(name):
            result[name] = (FileOwner.LOOSE, None)
        elif page_item is not None and image and stem not in COVER_NAMES:
            result[name] = (FileOwner.PAGE, page_item)
        elif holder is not None and (
            (image and (novel or stem in COVER_NAMES))
            or (not novel and name.casefold() == "comicinfo.xml")
            or (
                novel
                and container is not None
                and (
                    name.casefold() in _NOVEL_METADATA
                    or name.casefold() == f"{container[1]}.opf".casefold()
                )
            )
        ):
            result[name] = (FileOwner.COMPANION, holder)
        elif suffix in (_NOVEL_BODIES if novel else _COMIC_BODIES) or (
            not novel and image
        ):
            result[name] = (FileOwner.UNINDEXED, None)
        else:
            result[name] = (FileOwner.LOOSE, None)
    return result


def scan_files(
    index: LibraryIndex,
    *,
    directory: str | None = None,
    extensions: list[str] | None = None,
    name: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    whole: bool = False,
    limit: int = SCAN_LIMIT,
    seconds: float = SCAN_SECONDS,
) -> FileScan:
    """Walk a library directory and classify the files that match the filters.

    Run this synchronous filesystem operation in a worker thread. Directory
    symlinks are recorded but never followed, and files the application is
    writing (`.organizing-*`) are never matched.

    Args:
        index: Library lookups built from its items.
        directory: Library-relative directory to match below; `None` is the root.
        extensions: Lower-case suffixes with their dot; empty matches any.
        name: Case-insensitive glob on the file name; `None` matches any.
        min_size: Smallest matching size in bytes, if any.
        max_size: Largest matching size in bytes, if any.
        whole: Walk the whole library so symlinks outside the directory are
            known. Matches still come from the directory only.
        limit: Entries to visit before stopping with `truncated`.
        seconds: Wall-clock time to spend before stopping with `truncated`.

    Returns:
        Matches in path order, symlink targets seen, page totals of matched
        directory comics and whether the walk stopped early.

    Raises:
        ValueError: If the directory leaves the library or is not a directory.
        OSError: If the directory cannot be read.
    """
    root = index.root
    target = root / directory if directory else root
    _safe_path(root, target)
    if not target.is_dir():
        raise ValueError("directory does not exist in this library")
    classify = _reading_owners if index.lib_type in READING_TYPES else _video_owners
    pattern = name.casefold() if name else None
    suffixes = set(extensions or ())
    scan = FileScan()
    deadline = monotonic() + seconds
    visited = 0
    pending = [root if whole else target]
    while pending:
        current = pending.pop()
        names: list[str] = []
        states: dict[str, os.stat_result] = {}
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    visited += 1
                    if visited > limit or monotonic() > deadline:
                        scan.truncated = True
                        break
                    if entry.name.startswith(".organizing-"):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        link = Path(entry.path)
                        scan.links.add(_reference_key(link.parent / os.readlink(link)))
                        if link.is_dir():
                            continue
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                        continue
                    elif not stat.S_ISREG(info.st_mode):
                        continue
                    names.append(entry.name)
                    states[entry.name] = info
        except FileNotFoundError:
            continue
        if scan.truncated:
            break
        if not names or not (current == target or current.is_relative_to(target)):
            continue
        owners = classify(index, current, names)
        if (page_item := index.page_dirs.get(str(current))) is not None:
            scan.pages[page_item] = sum(
                1 for owner, _ in owners.values() if owner == FileOwner.PAGE
            )
        for entry_name in names:
            info = states[entry_name]
            if (
                (suffixes and Path(entry_name).suffix.casefold() not in suffixes)
                or (pattern and not fnmatchcase(entry_name.casefold(), pattern))
                or (min_size is not None and info.st_size < min_size)
                or (max_size is not None and info.st_size > max_size)
            ):
                continue
            owner, item_id = owners[entry_name]
            scan.matches.append(
                FileMatch(
                    current / entry_name,
                    info.st_size,
                    owner,
                    item_id,
                    [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns],
                )
            )
    scan.matches.sort(key=lambda match: str(match.path))
    return scan


def is_linked(path: Path, links: set[str]) -> bool:
    """Check whether a library symlink refers to a path or one of its parents.

    Args:
        path: File that a batch would remove.
        links: Normalized symlink targets from the walk.

    Returns:
        Whether removing the file would break a surviving link.
    """
    key = _reference_key(path)
    return any(key == link or key.startswith(link + os.sep) for link in links)


def relative(root: Path, path: Path | str) -> str:
    """Show a path relative to its library root.

    Args:
        root: Library root.
        path: Absolute path inside the library.

    Returns:
        Relative POSIX path.
    """
    return Path(path).relative_to(root).as_posix()


def shown(path: str, limit: int = PATH_CHARS) -> str:
    """Shorten a long path for a bounded sample, keeping both ends.

    Args:
        path: Library-relative path.
        limit: Longest text to return.

    Returns:
        The path, or its start and end around an ellipsis.
    """
    if len(path) <= limit:
        return path
    keep = (limit - 1) // 2
    return f"{path[:keep]}…{path[-keep:]}"


def plan_deletion(
    index: LibraryIndex,
    scan: FileScan,
    owners: list[FileOwner],
    *,
    busy_items: set[int],
    busy_paths: set[str],
) -> list[PlanEntry]:
    """Turn matched files into deletion units and blocked matches.

    Args:
        index: Library lookups used for the walk.
        scan: Matches and guards from a whole-library walk.
        owners: Classes the deletion may touch.
        busy_items: Items with a queued or running ingest workflow.
        busy_paths: Files of downloads that are still downloading or transferring.

    Returns:
        Entries in path order. An indexed body becomes one `item` unit, every
        other allowed match one `file` unit. Excluded classes, linked files,
        busy items and downloads, unwritable directories and selections that
        would empty a directory comic are blocked with a reason.
    """
    allowed = set(owners)
    selected_pages: dict[int, int] = {}
    for match in scan.matches:
        if match.owner == FileOwner.PAGE and match.owner in allowed:
            assert match.item_id is not None
            selected_pages[match.item_id] = selected_pages.get(match.item_id, 0) + 1
    emptied = {
        item_id
        for item_id, count in selected_pages.items()
        if count >= scan.pages.get(item_id, count)
    }
    writable: dict[Path, bool] = {}
    entries = []
    for match in scan.matches:
        entry = PlanEntry(
            "item" if match.owner == FileOwner.INDEXED else "file",
            match.owner,
            relative(index.root, match.path),
            match.size,
            match.identity,
            match.item_id,
        )
        parent = match.path.parent
        if parent not in writable:
            writable[parent] = os.access(parent, os.W_OK | os.X_OK)
        reason = None
        if match.owner not in allowed:
            reason = "owner_excluded"
        elif str(match.path) in busy_paths:
            reason = "download_active"
        elif match.item_id in busy_items and match.owner != FileOwner.LOOSE:
            reason = "ingesting"
        elif match.owner == FileOwner.PAGE and match.item_id in emptied:
            reason = "empties_item"
        elif is_linked(match.path, scan.links):
            reason = "linked"
        elif not writable[parent]:
            reason = "not_writable"
        if reason:
            entry.status, entry.reason = "blocked", reason
        entries.append(entry)
    return entries


def item_entries(
    index: LibraryIndex,
    items: list[MediaItem],
    children: dict[int, list[MediaItem]],
    *,
    busy_items: set[int],
) -> list[PlanEntry]:
    """Describe explicitly selected items as deletion units.

    Run in a worker thread; it reads file attributes.

    Args:
        index: Library lookups.
        items: Selected items whose parents are not also selected.
        children: Child items of each selected directory item or collection.
        busy_items: Items with a queued or running ingest workflow.

    Returns:
        One `item` unit per selection in path order. A body file carries its
        fingerprint; a directory item carries its sorted child IDs, so a
        change of either is noticed when the batch is applied.
    """
    entries = []
    for item in sorted(items, key=lambda row: row.path):
        path = Path(item.path)
        rows = children.get(item.id, [])
        entry = PlanEntry(
            "item",
            FileOwner.INDEXED,
            relative(index.root, path),
            item.size,
            None,
            item.id,
        )
        if item.path in index.bodies:
            try:
                entry.identity = _fingerprint(path)
            except OSError:
                entry.status, entry.reason = "blocked", "missing"
        else:
            entry.identity = sorted(row.id for row in rows)
            entry.files = len(rows) or 1
            sizes = [row.size for row in rows if row.size is not None]
            entry.size = sum(sizes) if sizes else item.size
        if entry.status == "planned" and (
            {item.id, *(row.id for row in rows)} & busy_items
        ):
            entry.status, entry.reason = "blocked", "ingesting"
        entries.append(entry)
    return entries


def delete_files(root: Path, units: list[tuple[int, str, list[int]]]) -> dict[int, str]:
    """Delete reviewed files whose attributes still match, under the library lock.

    Run with `write_in_thread`; a file being removed finishes before cancellation.

    Args:
        root: Absolute library root.
        units: Entry ID, library-relative path and reviewed fingerprint of each.

    Returns:
        Each entry's outcome: `done`, or the reason it was skipped or failed.
    """
    from app.utils.disk import delete_path

    outcomes = {}
    for entry_id, source, identity in units:
        path = root / source
        try:
            _safe_path(root, path)
            if _fingerprint(path) != identity:
                outcomes[entry_id] = "content_changed"
                continue
            delete_path(path)
            outcomes[entry_id] = "done"
        except FileNotFoundError:
            outcomes[entry_id] = "missing"
        except ValueError:
            outcomes[entry_id] = "unsafe_path"
        except OSError:
            outcomes[entry_id] = "io_error"
    return outcomes


def prune_directories(root: Path, directories: set[Path], kept: set[str]):
    """Remove directories a batch emptied, never the root or an indexed directory.

    Args:
        root: Absolute library root.
        directories: Former parents of deleted files.
        kept: Directory paths that items still use.
    """
    candidates = set()
    for directory in directories:
        for path in (directory, *directory.parents):
            if path == root or not path.is_relative_to(root):
                break
            candidates.add(path)
    for path in sorted(candidates, key=lambda entry: len(entry.parts), reverse=True):
        if str(path) in kept or path.is_symlink():
            continue
        try:
            path.rmdir()
        except OSError:
            # a directory that still holds files stays
            continue


def item_matches(index: LibraryIndex, items: list[MediaItem]) -> FileScan:
    """Describe the body files of explicitly selected items as matches.

    Run in a worker thread; it reads file attributes.

    Args:
        index: Library lookups.
        items: Selected items and the children of selected directory items.

    Returns:
        One indexed match per body file that still exists, in path order. In a
        reading library a collection, directory comic or chapter directory is
        also a match, identified by its item so that page changes do not matter.
    """
    scan = FileScan()
    reading = index.lib_type in READING_TYPES
    for item in sorted(items, key=lambda row: row.path):
        path = Path(item.path)
        if item.path in index.bodies:
            try:
                identity = _fingerprint(path)
            except OSError:
                continue
            size = identity[2]
        elif reading and path.is_dir() and not path.is_symlink():
            identity, size = [item.id], item.size or 0
        else:
            continue
        scan.matches.append(FileMatch(path, size, FileOwner.INDEXED, item.id, identity))
    return scan


def rename_target(
    pattern: str, replacement: str, name: str, *, directory: bool = False
) -> str | None:
    """Apply a rename rule to one file name, keeping its extension.

    Args:
        pattern: Regular expression replaced in the name without its extension.
        replacement: Replacement, optionally a template using the `title`, `year`,
            `season` and `episode` read from the old name.
        name: Current file or directory name.
        directory: Whether the name is a directory's, which has no extension
            even when it contains dots.

    Returns:
        The new file name, or `None` when the result is empty, hidden, reserved,
        too long or contains characters a file name cannot hold.

    Raises:
        re.error: If the pattern or the rendered replacement is invalid.
        jinja2.TemplateError: If the replacement template cannot be rendered.
    """
    old, suffix = (name, "") if directory else (Path(name).stem, Path(name).suffix)
    if is_template(replacement):
        # the same fields a download transfer rule reads from a file name
        replacement = render(
            replacement,
            context={
                "title": extract_title(old),
                "year": extract_year(old),
                "season": extract_season(old),
                "episode": extract_episode(old),
            },
            sandboxed=True,
        )
    stem = re.sub(pattern, replacement, old).strip()
    target = stem + suffix
    if (
        not stem
        or stem.startswith(".")
        or "/" in stem
        or _UNSAFE.search(stem)
        or _RESERVED.match(stem)
        or len(target.encode()) > 255
    ):
        return None
    return target


def plan_renames(
    index: LibraryIndex,
    scan: FileScan,
    owners: list[FileOwner],
    pattern: str,
    replacement: str,
    *,
    templated: bool,
    busy_items: set[int],
    busy_paths: set[str],
) -> tuple[list[PlanEntry], int]:
    """Turn matched files into rename units and blocked matches.

    Args:
        index: Library lookups used for the walk.
        scan: Matches and guards from a whole-library walk or an item selection.
        owners: Classes the rename may touch.
        pattern: Regular expression replaced in each name without its extension.
        replacement: Replacement text or template.
        templated: Whether the library names indexed items from a template.
        busy_items: Items with a queued or running ingest workflow.
        busy_paths: Files of downloads that are still downloading or transferring.

    Returns:
        Entries in path order and the number of matches the rule leaves
        unchanged. An indexed video becomes an `item` unit whose companions
        follow it; a loose or unindexed file a `file` unit. Companions and comic
        pages cannot be renamed on their own, a templated library keeps its
        indexed names, and names that are invalid, taken or produced twice are
        blocked.

    Raises:
        re.error: If the pattern or the rendered replacement is invalid.
        jinja2.TemplateError: If the replacement template cannot be rendered.
    """
    allowed = set(owners)
    entries: list[PlanEntry] = []
    unchanged = 0
    targets: dict[str, list[PlanEntry]] = {}
    for match in scan.matches:
        entry = PlanEntry(
            "item" if match.owner == FileOwner.INDEXED else "file",
            match.owner,
            relative(index.root, match.path),
            match.size,
            match.identity,
            match.item_id,
        )
        reason = None
        if match.owner not in allowed:
            reason = "owner_excluded"
        elif match.owner == FileOwner.COMPANION:
            reason = "follows_item"
        elif match.owner == FileOwner.PAGE:
            reason = "page_order"
        elif match.owner == FileOwner.INDEXED and templated:
            reason = "template_managed"
        else:
            target = rename_target(
                pattern,
                replacement,
                match.path.name,
                directory=match.owner == FileOwner.INDEXED
                and str(match.path) not in index.bodies,
            )
            if target == match.path.name:
                unchanged += 1
                continue
            destination = match.path.with_name(target) if target else None
            if destination is None or (
                index.lib_type in READING_TYPES and is_ignored_name(target or "")
            ):
                # a hidden or temporary name would drop out of a reading library
                reason = "invalid_name"
            else:
                entry.dst = relative(index.root, destination)
                # a case-only rename of the same file is not a conflict
                if os.path.lexists(destination) and not (
                    target.casefold() == match.path.name.casefold()
                    and os.path.samefile(destination, match.path)
                ):
                    reason = "destination_exists"
                elif str(match.path) in busy_paths:
                    reason = "download_active"
                elif match.item_id in busy_items:
                    reason = "ingesting"
                elif match.path.is_symlink() or is_linked(match.path, scan.links):
                    reason = "linked"
                elif not os.access(match.path.parent, os.W_OK | os.X_OK):
                    reason = "not_writable"
                else:
                    targets.setdefault(str(destination).casefold(), []).append(entry)
        if reason:
            entry.status, entry.reason = "blocked", reason
        entries.append(entry)
    for group in targets.values():
        if len(group) > 1:
            for entry in group:
                entry.status, entry.reason = "blocked", "name_conflict"
    return entries, unchanged


def rename_files(
    root: Path, units: list[tuple[int, str, str, list[int]]]
) -> dict[int, str]:
    """Rename reviewed files whose attributes still match, under the library lock.

    Run with `write_in_thread`; a rename in progress finishes before cancellation.

    Args:
        root: Absolute library root.
        units: Entry ID, library-relative source and destination, and the
            reviewed fingerprint of each.

    Returns:
        Each entry's outcome: `done`, or the reason it was skipped or failed.
    """
    from app.utils.disk import rename_exclusive

    outcomes = {}
    for entry_id, source, destination, identity in units:
        path, target = root / source, root / destination
        try:
            _safe_path(root, path)
            _safe_path(root, target)
            if _fingerprint(path) != identity:
                outcomes[entry_id] = "content_changed"
                continue
            if path.parent != target.parent:
                outcomes[entry_id] = "unsafe_path"
                continue
            if source.casefold() == destination.casefold():
                # exclusive renaming refuses a name that differs only by case
                os.rename(path, target)
            else:
                rename_exclusive(path, target)
            outcomes[entry_id] = "done"
        except FileNotFoundError:
            outcomes[entry_id] = "missing"
        except FileExistsError:
            outcomes[entry_id] = "destination_exists"
        except ValueError:
            outcomes[entry_id] = "unsafe_path"
        except OSError:
            outcomes[entry_id] = "io_error"
    return outcomes


def relocate_reading(root: Path, source: Path, target: Path) -> list[tuple[Path, Path]]:
    """Rename a reading body file or container on disk, under the library lock.

    Run with `write_in_thread`. A body file takes its same-stem OPF along. The
    index is updated afterwards by the service that carries an observed move.

    Args:
        root: Absolute library root.
        source: Body file, work directory or chapter directory to rename.
        target: New path, which must not exist and whose parent must exist.

    Returns:
        The renames performed, so the caller can undo them if the index refuses.

    Raises:
        FileExistsError: If the target is taken.
        FileNotFoundError: If the source or the target's parent is missing.
        ValueError: If a path is unsafe or the source is a symlink.
        OSError: If a rename fails; renames already done are undone first.
    """
    from app.utils.disk import rename_exclusive

    _safe_path(root, source)
    _safe_path(root, target)
    if source.is_symlink():
        raise ValueError("renaming a linked source is not supported")
    if not os.path.lexists(source) or not target.parent.is_dir():
        raise FileNotFoundError(str(source))
    renames = [(source, target)]
    if source.is_file():
        wanted = f"{source.stem}.opf".casefold()
        for sibling in source.parent.iterdir():
            if sibling.name.casefold() == wanted and sibling.is_file():
                renames.append((sibling, target.with_suffix(sibling.suffix)))
    if any(os.path.lexists(new) for _, new in renames):
        raise FileExistsError(str(target))
    done: list[tuple[Path, Path]] = []
    try:
        for old, new in renames:
            rename_exclusive(old, new)
            done.append((old, new))
    except OSError:
        restore_reading(done)
        raise
    return done


def restore_reading(renames: list[tuple[Path, Path]]):
    """Undo renames made by `relocate_reading`, newest first.

    Args:
        renames: Renames that were performed.
    """
    for old, new in reversed(renames):
        try:
            os.rename(new, old)
        except OSError:
            # the watcher reconciles whatever could not be put back
            continue


def empty_directories(
    index: LibraryIndex, directory: str | None = None, *, limit: int = SCAN_LIMIT
) -> tuple[list[Path], bool]:
    """Find directories that hold no file anywhere below them.

    Run in a worker thread. Only the topmost empty directory of each empty tree
    is reported. The library root, symlinks and directories an item is
    registered on are never reported.

    Args:
        index: Library lookups built from its items.
        directory: Library-relative directory to search below; `None` is the root.
        limit: Entries to visit before stopping.

    Returns:
        Empty directories in path order, and whether the walk stopped early.

    Raises:
        ValueError: If the directory leaves the library or is not a directory.
        OSError: If the directory cannot be read.
    """
    root = index.root
    target = root / directory if directory else root
    _safe_path(root, target)
    if not target.is_dir():
        raise ValueError("directory does not exist in this library")
    registered = set(index.directories) | set(index.page_dirs) | set(index.containers)
    empty: set[Path] = set()
    visited = 0
    for current, names, files in os.walk(target, topdown=False, followlinks=False):
        visited += len(names) + len(files)
        if visited > limit:
            return [], True
        path = Path(current)
        if (
            not files
            and str(path) not in registered
            and not path.is_symlink()
            and all(
                path / name in empty and not (path / name).is_symlink()
                for name in names
            )
        ):
            empty.add(path)
    empty.discard(root)
    return sorted(path for path in empty if path.parent not in empty), False


def remove_directories(root: Path, units: list[tuple[int, str]]) -> dict[int, str]:
    """Remove reviewed directories that are still empty, under the library lock.

    Run with `write_in_thread`.

    Args:
        root: Absolute library root.
        units: Entry ID and library-relative path of each directory.

    Returns:
        Each entry's outcome: `done`, or the reason it was skipped or failed.
    """
    outcomes = {}
    for entry_id, source in units:
        path = root / source
        try:
            _safe_path(root, path)
            if path == root or path.is_symlink() or not path.is_dir():
                outcomes[entry_id] = "missing"
                continue
            below = list(os.walk(path, topdown=False, followlinks=False))
            if any(files for _, _, files in below) or any(
                (Path(current) / name).is_symlink()
                for current, names, _ in below
                for name in names
            ):
                outcomes[entry_id] = "not_empty"
                continue
            for current, _, _ in below:
                os.rmdir(current)
            outcomes[entry_id] = "done"
        except ValueError:
            outcomes[entry_id] = "unsafe_path"
        except OSError:
            outcomes[entry_id] = "io_error"
    return outcomes
