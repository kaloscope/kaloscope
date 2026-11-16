"""Select and classify library files for reviewed batches without changing them."""

import mimetypes
import os
import re
import stat
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from time import monotonic, time_ns
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
from app.utils.regex import substitute

_PADDED = re.compile(r"\{\{\s*(\w+)\s*:\s*0?(\d)d?\s*\}\}")
_DOLLAR = re.compile(r"\$(?:(\d+)|\{(\d+)\})")
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
    # directory -> every fact above that its classification reads, so a
    # remembered classification is reused only while they are unchanged
    folders: dict[str, tuple[Any, ...]] = field(default_factory=dict)

    def folder_key(self, directory: str) -> tuple[Any, ...]:
        """Identify what classifying one directory depends on.

        Args:
            directory: Absolute directory path.

        Returns:
            The library type and the directory's own index facts.
        """
        return (self.lib_type, self.folders.get(directory, ()))


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
    facts: dict[str, list[tuple[Any, ...]]] = {}
    for kind, lookup in (("body", index.bodies), ("owned", index.owned)):
        for path, item_id in lookup.items():
            parent, name = os.path.split(path)
            facts.setdefault(parent, []).append((kind, name, item_id))
    for kind, lookup in (
        ("directory", index.directories),
        ("pages", index.page_dirs),
        ("container", index.containers),
    ):
        for path, value in lookup.items():
            facts.setdefault(path, []).append((kind, value))
    index.folders.update((path, tuple(sorted(rows))) for path, rows in facts.items())
    return index


def _name_parts(name: str) -> tuple[str, str]:
    """Split a file name as `Path.stem` and a case-folded `Path.suffix` would.

    Args:
        name: File name without a directory.

    Returns:
        The stem and the lower-case suffix, which is empty for a name without
        one or a name that only starts with a dot.
    """
    stem, suffix = os.path.splitext(name)
    if suffix == ".":
        # `Path` treats a trailing dot as part of the stem
        return name, ""
    return stem, suffix.casefold()


def _is_video(name: str) -> bool:
    """Check whether a file name has a video type.

    Args:
        name: File name.

    Returns:
        Whether its guessed MIME type is a video type or its extension is a
        video container the library does not index, which must not look loose.
    """
    return _video_suffix(_name_parts(name)[1])


@lru_cache(maxsize=1024)
def _video_suffix(suffix: str) -> bool:
    """Check whether a lower-case file suffix names a video type.

    Args:
        suffix: Suffix with its dot, possibly empty.

    Returns:
        Whether its guessed MIME type is a video type or it is a video container
        the library does not index.
    """
    return bool(suffix) and (
        (mimetypes.guess_file_type(f"file{suffix}")[0] or "").startswith("video/")
        or suffix in _VIDEO_EXTENSIONS
    )


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
    folder = os.path.basename(base)
    videos = {name for name in names if _is_video(name)}
    indexed = {
        _name_parts(name)[0]: index.bodies[f"{base}/{name}"]
        for name in videos
        if f"{base}/{name}" in index.bodies
    }
    stems = sorted((_name_parts(name)[0] for name in videos), key=len, reverse=True)
    holder = index.directories.get(base)
    if holder is None and indexed:
        holder = next(iter(indexed.values()))
    result: dict[str, tuple[FileOwner, int | None]] = {}
    for name in names:
        path = f"{base}/{name}"
        stem, suffix = _name_parts(name)
        if path in index.bodies:
            result[name] = (FileOwner.INDEXED, index.bodies[path])
        elif name in videos:
            result[name] = (FileOwner.UNINDEXED, None)
        elif path in index.owned:
            result[name] = (FileOwner.COMPANION, index.owned[path])
        elif suffix == ".nfo" and (stem in indexed or stem == folder):
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
        stem, suffix = _name_parts(name)
        stem = stem.casefold()
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


@dataclass
class _Listing:
    """Remember one directory's entries as they were when it was read."""

    mtime: int
    read_at: int
    # file name -> device, inode, size and modification time
    files: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    directories: list[Path] = field(default_factory=list)
    # normalized targets of this directory's symlinks
    links: set[str] = field(default_factory=set)
    # the files' classes, and the index facts they were computed from
    owners: dict[str, tuple[FileOwner, int | None]] | None = None
    owners_key: tuple[Any, ...] | None = None


@dataclass
class _Walk:
    """Hold the directories one walk visited, in visiting order."""

    directories: dict[Path, _Listing] = field(default_factory=dict)
    files: int = 0
    truncated: bool = False
    at: float = field(default_factory=monotonic)


# library root -> its last complete walk, kept per worker process
_WALKS: dict[str, _Walk] = {}
WALK_ROOTS = 4
WALK_FILES = 100_000
WALK_SECONDS = 600
# coarse filesystems stamp changes up to two seconds apart
_RACY_NS = 2_000_000_000


def forget_walk(root: Path | str):
    """Drop a library's remembered walk after its files were changed.

    Args:
        root: Library root directory.
    """
    _WALKS.pop(str(root), None)


def _walk(start: Path, known: _Walk | None, limit: int, deadline: float) -> _Walk:
    """List every directory below a start, reading only those that changed.

    A directory's modification time changes whenever an entry is added, removed
    or renamed, so an unchanged one is taken from the earlier walk. A file that
    was rewritten in place keeps its remembered size until the walk expires; a
    plan built on it is refused for that file when applied, because every unit
    is compared with the file on disk first.

    Args:
        start: Directory to walk; symlinked directories are not followed.
        known: Earlier complete walk of the same library, if remembered.
        limit: Entries to visit before stopping with `truncated`.
        deadline: Monotonic time to stop at with `truncated`.

    Returns:
        The visited directories with their files, subdirectories and symlinks.
    """
    if known is not None and monotonic() - known.at > WALK_SECONDS:
        known = None
    walk = _Walk()
    visited = 0
    pending = [start]
    while pending:
        current = pending.pop()
        try:
            mtime = os.stat(current, follow_symlinks=False).st_mtime_ns
        except FileNotFoundError:
            continue
        listing = known.directories.get(current) if known else None
        # a change in the same clock tick as the last read would go unnoticed
        if (
            listing is not None
            and listing.mtime == mtime
            and listing.read_at - mtime > _RACY_NS
        ):
            visited += 1
        else:
            listing = _Listing(mtime, time_ns())
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > limit or monotonic() > deadline:
                            walk.truncated = True
                            break
                        if entry.name.startswith(".organizing-"):
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            link = Path(entry.path)
                            listing.links.add(
                                _reference_key(link.parent / os.readlink(link))
                            )
                            if link.is_dir():
                                continue
                        elif stat.S_ISDIR(info.st_mode):
                            listing.directories.append(Path(entry.path))
                            continue
                        elif not stat.S_ISREG(info.st_mode):
                            continue
                        listing.files[entry.name] = (
                            info.st_dev,
                            info.st_ino,
                            info.st_size,
                            info.st_mtime_ns,
                        )
            except FileNotFoundError:
                continue
        if walk.truncated or visited > limit or monotonic() > deadline:
            walk.truncated = True
            break
        walk.directories[current] = listing
        walk.files += len(listing.files)
        pending.extend(listing.directories)
    return walk


def scan_files(
    index: LibraryIndex,
    *,
    directory: str | None = None,
    extensions: list[str] | None = None,
    name: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    whole: bool = False,
    fresh: bool = False,
    limit: int = SCAN_LIMIT,
    seconds: float = SCAN_SECONDS,
) -> FileScan:
    """Walk a library directory and classify the files that match the filters.

    Run this synchronous filesystem operation in a worker thread. Directory
    symlinks are recorded but never followed, and files the application is
    writing (`.organizing-*`) are never matched.
    A complete walk of the library is remembered for a few minutes, so a review
    that follows a listing reads only the directories that changed in between.
    Each remembered directory also keeps its files' classes until its listing
    or the index facts about it change, and a directory without a file that
    passes the filters is not classified at all.

    Args:
        index: Library lookups built from its items.
        directory: Library-relative directory to match below; `None` is the root.
        extensions: Lower-case suffixes with their dot; empty matches any.
        name: Case-insensitive glob on the file name; `None` matches any.
        min_size: Smallest matching size in bytes, if any.
        max_size: Largest matching size in bytes, if any.
        whole: Walk the whole library so symlinks outside the directory are
            known. Matches still come from the directory only.
        fresh: Read current file attributes instead of reusing a remembered walk.
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
    start = root if whole else target
    walk = _walk(
        start, None if fresh else _WALKS.get(str(root)), limit, monotonic() + seconds
    )
    scan.truncated = walk.truncated
    if start == root and not walk.truncated and walk.files <= WALK_FILES:
        _WALKS.pop(str(root), None)
        while len(_WALKS) >= WALK_ROOTS:
            del _WALKS[next(iter(_WALKS))]
        _WALKS[str(root)] = walk
    for current, listing in walk.directories.items():
        scan.links.update(listing.links)
        if not listing.files or not (
            current == target or current.is_relative_to(target)
        ):
            continue
        wanted = [
            (entry_name, identity)
            for entry_name, identity in listing.files.items()
            if not (
                (suffixes and _name_parts(entry_name)[1] not in suffixes)
                or (pattern and not fnmatchcase(entry_name.casefold(), pattern))
                or (min_size is not None and identity[2] < min_size)
                or (max_size is not None and identity[2] > max_size)
            )
        ]
        if not wanted:
            # page totals are only read for comics with matched pages
            continue
        base = str(current)
        key = index.folder_key(base)
        owners = listing.owners
        if owners is None or listing.owners_key != key:
            owners = classify(index, current, list(listing.files))
            # remembered listings are shared with later scans of this library
            listing.owners, listing.owners_key = owners, key
        if (page_item := index.page_dirs.get(base)) is not None:
            scan.pages[page_item] = sum(
                1 for owner, _ in owners.values() if owner == FileOwner.PAGE
            )
        for entry_name, identity in wanted:
            size = identity[2]
            owner, item_id = owners[entry_name]
            scan.matches.append(
                FileMatch(current / entry_name, size, owner, item_id, list(identity))
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


def rename_targets(
    pattern: str,
    replacement: str,
    names: list[tuple[str, bool, dict[str, Any] | None]],
) -> list[str | None]:
    """Apply a rename rule to many names, keeping each file's extension.

    Args:
        pattern: Regular expression replaced in each name without its extension.
        replacement: Replacement, optionally a template using `title`, `year`,
            `season`, `episode` and `episode_title`; `{{season:02}}` pads a
            number with zeros. Groups may be written `\\1`, `\\g<1>`, `$1` or
            `${1}`.
        names: Current name, whether it is a directory's (which has no extension
            even when it contains dots), and saved metadata that takes
            precedence over what the name itself says.

    Returns:
        For each name the new name, or `None` when the result is empty, hidden,
        reserved, too long or contains characters a file name cannot hold.

    Raises:
        re.error: If the pattern or a rendered replacement is invalid.
        jinja2.TemplateError: If the replacement template cannot be rendered.
        TimeoutError: If the pattern takes too long to apply.
    """
    # `{{season:02}}` is how most people ask for padding; Jinja has no format spec
    replacement = _PADDED.sub(
        lambda match: (
            f"{{{{ '%0{match[2]}d' % {match[1]} if {match[1]} is not none else '' }}}}"
        ),
        replacement,
    )
    # most regex tools write group references as `$1` or `${1}`; converting the
    # rule, not rendered values, leaves a title such as `$5 a Day` alone
    replacement = _DOLLAR.sub(lambda match: f"\\g<{match[1] or match[2]}>", replacement)
    templated = is_template(replacement)
    parts = []
    pairs = []
    for name, directory, saved in names:
        old, suffix = (name, "") if directory else (Path(name).stem, Path(name).suffix)
        rendered = replacement
        if templated:
            # a transfer rule reads these from the name; saved metadata is surer
            context = {
                "title": extract_title(old),
                "year": extract_year(old),
                "season": extract_season(old),
                "episode": extract_episode(old),
                "episode_title": "",
            }
            context.update(saved or {})
            rendered = render(replacement, context=context, sandboxed=True)
        parts.append(suffix)
        pairs.append((rendered, old))
    targets: list[str | None] = []
    for stem, suffix in zip(substitute(pattern, pairs), parts, strict=True):
        stem = stem.strip()
        target = stem + suffix
        targets.append(
            None
            if (
                not stem
                or stem.startswith(".")
                or "/" in stem
                or _UNSAFE.search(stem)
                or _RESERVED.match(stem)
                or len(target.encode()) > 255
            )
            else target
        )
    return targets


def rename_target(
    pattern: str, replacement: str, name: str, *, directory: bool = False
) -> str | None:
    """Apply a rename rule to one name; see `rename_targets`.

    Args:
        pattern: Regular expression replaced in the name without its extension.
        replacement: Replacement text or template.
        name: Current file or directory name.
        directory: Whether the name is a directory's.

    Returns:
        The new name, or `None` when the result is not a usable name.

    Raises:
        re.error: If the pattern or the rendered replacement is invalid.
        jinja2.TemplateError: If the replacement template cannot be rendered.
        TimeoutError: If the pattern takes too long to apply.
    """
    return rename_targets(pattern, replacement, [(name, directory, None)])[0]


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
    metadata: dict[int, dict[str, Any]] | None = None,
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
        metadata: Saved title, year, season and episode of items by ID, offered
            to the replacement template ahead of what a name says.

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
        TimeoutError: If the pattern takes too long to apply.
    """
    allowed = set(owners)
    entries: list[PlanEntry] = []
    unchanged = 0
    targets: dict[str, list[PlanEntry]] = {}
    renamed = iter(
        rename_targets(
            pattern,
            replacement,
            [
                (
                    match.path.name,
                    match.owner == FileOwner.INDEXED
                    and str(match.path) not in index.bodies,
                    (metadata or {}).get(match.item_id or 0),
                )
                for match in scan.matches
                if match.owner in allowed
                and match.owner not in (FileOwner.COMPANION, FileOwner.PAGE)
                and not (match.owner == FileOwner.INDEXED and templated)
            ],
        )
    )
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
            target = next(renamed)
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


def plan_reading_relocation(
    root: Path, source: Path, target: Path, *, create_parent: bool = False
) -> dict[str, Any]:
    """Freeze a reading file or container move before changing the filesystem.

    A body file takes its same-stem OPF along. Persist the returned payload as
    an organization journal before applying it under the library lock.

    Args:
        root: Absolute library root.
        source: Body file, work directory or chapter directory to rename.
        target: New path, which must not exist.
        create_parent: Whether moving a chapter may create its destination parent.

    Returns:
        File identities, path mappings and the existing executor's empty lists.

    Raises:
        FileExistsError: If the target is taken.
        FileNotFoundError: If the source or a required target parent is missing.
        ValueError: If a path is unsafe or the source is a symlink.
        OSError: If file attributes cannot be read.
    """
    _safe_path(root, source)
    _safe_path(root, target)
    if source.is_symlink():
        raise ValueError("renaming a linked source is not supported")
    if not os.path.lexists(source) or (
        not create_parent and not target.parent.is_dir()
    ):
        raise FileNotFoundError(str(source))
    renames = [(source, target)]
    if source.is_file():
        wanted = f"{source.stem}.opf".casefold()
        for sibling in source.parent.iterdir():
            if sibling.name.casefold() == wanted and sibling.is_file():
                renames.append((sibling, target.with_suffix(sibling.suffix)))
    if any(os.path.lexists(new) for _, new in renames):
        raise FileExistsError(str(target))
    return {
        "moves": [
            {"src": str(old), "dst": str(new), "identity": _fingerprint(old)}
            for old, new in renames
        ],
        "mapping": {str(old): str(new) for old, new in renames},
        "creates": [],
        "symlinks": [],
    }


def restore_reading(root: Path, moves: list[dict[str, Any]]) -> bool:
    """Undo journaled reading moves without replacing unrelated files.

    Args:
        root: Absolute library root.
        moves: Source, destination and original fingerprint of each move.

    Returns:
        Whether every source was restored or was already unchanged. A missing,
        replaced or occupied path leaves the journal available for recovery.
    """
    from app.utils.disk import rename_exclusive

    for move in reversed(moves):
        old, new = Path(move["src"]), Path(move["dst"])
        try:
            _safe_path(root, old)
            _safe_path(root, new)
            if os.path.lexists(old):
                if _fingerprint(old) == move["identity"] and not os.path.lexists(new):
                    continue
                return False
            if _fingerprint(new) != move["identity"]:
                return False
            rename_exclusive(new, old)
        except (OSError, ValueError):
            return False
    return True


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


def leftover_directories(
    index: LibraryIndex, scan: FileScan, directory: str | None, busy_paths: set[str]
) -> list[tuple[Path, int, int]]:
    """Find folders whose every file is a loose leftover.

    A folder qualifies when nothing below it is indexed, unindexed content, a
    companion of an item, a comic page, a symlink target or a file a download
    still uses, and no item is registered on it or below it.

    Args:
        index: Library lookups built from its items.
        scan: Unfiltered scan of the directory, made with the whole library's
            symlinks.
        directory: Library-relative directory that was scanned; `None` is the root.
        busy_paths: Files of downloads that are still downloading or transferring.

    Returns:
        The topmost qualifying folders in path order, each with the number of
        files below it and their bytes. The library root never qualifies.
    """
    root = index.root
    target = root / directory if directory else root
    blocked: set[Path] = set()
    totals: dict[Path, list[int]] = {}
    for match in scan.matches:
        kept = (
            match.owner != FileOwner.LOOSE
            or str(match.path) in busy_paths
            or is_linked(match.path, scan.links)
        )
        for parent in match.path.parents:
            if parent == root:
                break
            if kept:
                blocked.add(parent)
            else:
                total = totals.setdefault(parent, [0, 0])
                total[0] += 1
                total[1] += match.size
    for registered in (*index.directories, *index.page_dirs, *index.containers):
        path = Path(registered)
        blocked.update((path, *path.parents))
    found = {
        path
        for path in totals
        if path not in blocked and (path == target or path.is_relative_to(target))
    }
    return [(path, *totals[path]) for path in sorted(found) if path.parent not in found]


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
