"""Discover reading sources, observe stability and resolve event ownership."""

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from watchdog.events import (
    EVENT_TYPE_CREATED,
    EVENT_TYPE_DELETED,
    EVENT_TYPE_MODIFIED,
    EVENT_TYPE_MOVED,
    DirModifiedEvent,
    FileModifiedEvent,
    FileSystemEvent,
)

from app.core.media.common import ContentError, file_state
from app.core.media.handlers.base import _HANDLERS, MediaHandler
from app.models.media import LibType, MediaFormat

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# preferred cover basenames, in fallback order
COVER_NAMES = ("cover", "folder", "poster")
MAX_PAGES = 10_000

_NOVEL_EXTENSIONS = {".txt", ".epub"}
_COMIC_EXTENSIONS = {".cbz", ".zip"}
_IGNORED_NAMES = {
    "__macosx",
    "$recycle.bin",
    "system volume information",
    "desktop.ini",
    "thumbs.db",
}
_TEMP_EXTENSIONS = {".tmp", ".part", ".partial", ".download", ".crdownload"}
_EVENT_TYPES = {
    EVENT_TYPE_CREATED,
    EVENT_TYPE_MODIFIED,
    EVENT_TYPE_DELETED,
    EVENT_TYPE_MOVED,
}


@dataclass(frozen=True)
class ReadingSource:
    """A file, image directory or comic collection found on disk."""

    path: Path
    format: MediaFormat | None
    parent_path: Path | None = None
    pages: tuple[Path, ...] = ()

    @property
    def directory(self) -> Path:
        """Return the container owning this source's external metadata.

        Returns:
            The source directory or the parent of a body file.
        """
        return self.path if self.format in (None, MediaFormat.DIR) else self.path.parent


@dataclass
class SourceScan:
    """Discovered sources and scopes that could not be classified safely."""

    sources: list[ReadingSource] = field(default_factory=list)
    issues: dict[Path, str] = field(default_factory=dict)


def natural_key(name: str) -> tuple:
    """Sort ASCII numbers naturally, breaking equal keys by the original name.

    Args:
        name: The filename or relative member path to compare.

    Returns:
        A normalized, case-insensitive key with a deterministic final tie-breaker.
    """
    parts = re.split(r"([0-9]+)", unicodedata.normalize("NFC", name).casefold())
    return tuple(
        int(part) if index % 2 else part for index, part in enumerate(parts)
    ), name


def is_ignored_name(name: str) -> bool:
    """Check names excluded from discovery, content indexes and event ownership.

    Args:
        name: A single path component.

    Returns:
        Whether the component is hidden, temporary or a system artifact.
    """
    return (
        name.startswith(".")
        or name.casefold() in _IGNORED_NAMES
        or Path(name).suffix.casefold() in _TEMP_EXTENSIONS
    )


def list_source_entries(directory: Path) -> tuple[list[Path], list[Path]]:
    """List regular files and directories without following symbolic links.

    Args:
        directory: The directory to inspect.

    Returns:
        Naturally ordered files and child directories.

    Raises:
        OSError: If the directory or any visible entry cannot be inspected.
    """
    if not stat.S_ISDIR(directory.stat(follow_symlinks=False).st_mode):
        raise NotADirectoryError(str(directory))
    files, directories = [], []
    with os.scandir(directory) as entries:
        for entry in entries:
            if is_ignored_name(entry.name):
                continue
            mode = entry.stat(follow_symlinks=False).st_mode
            if stat.S_ISREG(mode):
                files.append(Path(entry.path))
            elif stat.S_ISDIR(mode):
                directories.append(Path(entry.path))
    files.sort(key=lambda path: natural_key(path.name))
    directories.sort(key=lambda path: natural_key(path.name))
    return files, directories


def identify_comic_source(
    directory: Path, files: list[Path], *, parent_path: Path | None = None
) -> ReadingSource | None:
    """Identify one comic reading unit from its direct files.

    Args:
        directory: The work or chapter container.
        files: Its sorted regular files.
        parent_path: The collection path, or None for a standalone comic.

    Returns:
        The reading source, or None when no supported body is present.

    Raises:
        ValueError: With a stable code for ambiguous or oversized content.
    """
    pages = tuple(
        path
        for path in files
        if path.suffix.casefold() in IMAGE_EXTENSIONS
        and path.stem.casefold() not in COVER_NAMES
    )
    archives = [path for path in files if path.suffix.casefold() in _COMIC_EXTENSIONS]
    if len(archives) > 1 or (archives and pages):
        raise ValueError("ambiguous_layout")
    if archives:
        path = archives[0]
        return ReadingSource(path, MediaFormat(path.suffix[1:].lower()), parent_path)
    if len(pages) > MAX_PAGES:
        raise ValueError("media_limit_exceeded")
    if pages:
        return ReadingSource(directory, MediaFormat.DIR, parent_path, pages)
    return None


class ReadingMediaHandler(MediaHandler):
    """Apply the shared work-container rules for novels and comics."""

    def __init__(self, lib_type: LibType):
        """Select the supported reading library type.

        Args:
            lib_type: The novel or comic library type.

        Raises:
            ValueError: If the library type is not a reading type.
        """
        if lib_type not in (LibType.NOVEL, LibType.COMIC):
            raise ValueError(f"unsupported reading library type: {lib_type}")
        self.lib_type = lib_type

    def accept(self) -> list[str]:
        """Return MIME hints; discovery validates layout and explicit suffixes.

        Returns:
            The content MIME hints for this library type.
        """
        if self.lib_type == LibType.NOVEL:
            return ["text/plain", "application/epub+zip"]
        return ["image/*", "application/zip", "application/vnd.comicbook+zip"]

    def scan_sources(
        self, base_path: str, *, work_path: Path | None = None
    ) -> SourceScan:
        """Discover a library or a single work without reading body contents.

        Run this synchronous filesystem operation in a worker thread. Empty
        containers create no candidates; issues must not imply source deletion.

        Args:
            base_path: The absolute library root.
            work_path: One direct work directory, or None to scan the whole root.

        Returns:
            Sources and per-scope layout or access issues, without database writes.

        Raises:
            ValueError: If the root is invalid or a work is outside its direct level.
        """
        root = Path(base_path)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("library root must be an absolute normalized path")
        if work_path is not None and (
            work_path.parent != root or is_ignored_name(work_path.name)
        ):
            raise ValueError("work must be a visible direct child of the library")
        result = SourceScan()
        try:
            if work_path is None:
                files, works = list_source_entries(root)
                extensions = (
                    _NOVEL_EXTENSIONS
                    if self.lib_type == LibType.NOVEL
                    else _COMIC_EXTENSIONS | IMAGE_EXTENSIONS
                )
                result.issues.update(
                    (path, "unsupported_layout")
                    for path in files
                    if path.suffix.casefold() in extensions
                )
            else:
                if not stat.S_ISDIR(root.stat(follow_symlinks=False).st_mode):
                    raise NotADirectoryError(str(root))
                works = [work_path]
        except OSError:
            result.issues[root] = "media_source_unavailable"
            return result
        for work in works:
            try:
                result.sources.extend(self._scan_work(work, result.issues))
            except OSError:
                result.issues[work] = "media_source_unavailable"
            except ValueError as error:
                result.issues[work] = str(error)
        return result

    def _scan_work(self, work: Path, issues: dict[Path, str]) -> list[ReadingSource]:
        """Classify one work and preserve independent chapter failures.

        Args:
            work: The direct child directory of the library root.
            issues: The scan's layout and access issues, updated in place.

        Returns:
            A novel, standalone comic or collection followed by its chapters.

        Raises:
            OSError: If the work cannot be inspected completely.
            ValueError: With a stable code if the work's layout is invalid.
        """
        files, directories = list_source_entries(work)
        if self.lib_type == LibType.NOVEL:
            bodies = [
                path for path in files if path.suffix.casefold() in _NOVEL_EXTENSIONS
            ]
            if len(bodies) > 1:
                raise ValueError("ambiguous_layout")
            if bodies:
                path = bodies[0]
                return [ReadingSource(path, MediaFormat(path.suffix[1:].lower()))]
            if directories:
                raise ValueError("unsupported_layout")
            return []

        source = identify_comic_source(work, files)
        chapters, chapter_issues = [], {}
        for directory in directories:
            try:
                child_files, child_dirs = list_source_entries(directory)
                child = identify_comic_source(directory, child_files, parent_path=work)
                if child:
                    chapters.append(child)
                elif child_dirs:
                    chapter_issues[directory] = "unsupported_layout"
            except OSError:
                chapter_issues[directory] = "media_source_unavailable"
            except ValueError as error:
                chapter_issues[directory] = str(error)
        if source and chapters:
            raise ValueError("ambiguous_layout")
        issues.update(chapter_issues)
        if source and chapter_issues:
            issues[work] = (
                "media_source_unavailable"
                if "media_source_unavailable" in chapter_issues.values()
                else "ambiguous_layout"
            )
            return []
        if source:
            return [source]
        if chapters:
            return [ReadingSource(work, None), *chapters]
        return []

    def snapshot_sources(
        self,
        base_path: str,
        *,
        work_path: Path,
        targets: set[Path] | None = None,
    ) -> str:
        """Fingerprint selected source attributes without opening body files.

        Run in a worker thread. Include work-level files and directory identities
        for layout checks, but only inspect selected chapters' files. Absence is
        an observation, never authorization to delete an indexed item.

        Args:
            base_path: The absolute library root without symbolic-link ancestors.
            work_path: The visible work directory directly below the root.
            targets: Work or direct comic chapter containers; None selects the work.

        Returns:
            A digest of relevant paths, identities, sizes and write timestamps.

        Raises:
            ValueError: If the work or targets are outside the supported layout.
            ContentError: If a source cannot be inspected safely or changes type.
        """
        root = Path(base_path)
        selected = {work_path} if targets is None else targets
        if (
            work_path.parent != root
            or not selected
            or any(
                self.resolve_event_targets(
                    DirModifiedEvent(str(target)), base_path=base_path
                )
                != {work_path: {target}}
                for target in selected | {work_path}
            )
        ):
            raise ValueError("targets must select a work or its direct comic chapters")

        entries: list[tuple] = []

        def capture(directory: Path) -> list[Path]:
            """Record one container and return its visible child directories.

            Args:
                directory: The work or a selected chapter container.

            Returns:
                Child directories, or an empty list for a missing container.

            Raises:
                OSError: If the container cannot be enumerated or stat fails.
                ContentError: If an entry changes type while being inspected.
            """
            try:
                info = directory.stat(follow_symlinks=False)
            except FileNotFoundError:
                entries.append((str(directory), None))
                return []
            if not stat.S_ISDIR(info.st_mode):
                raise ContentError("media_source_unavailable")
            # directory write times include ignored files and unrelated chapter writes
            entries.append((str(directory), *file_state(info)[:2]))
            files, directories = list_source_entries(directory)
            for child in directories:
                info = child.stat(follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise ContentError("content_changed")
                entries.append((str(child), *file_state(info)[:2]))
            for path in files:
                if self.filter_event(FileModifiedEvent(str(path)), base_path=base_path):
                    info = path.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode):
                        raise ContentError("content_changed")
                    entries.append((str(path), *file_state(info)))
            return directories

        try:
            for directory in (*reversed(root.parents), root):
                info = directory.stat(follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise ContentError("media_source_unavailable")
                entries.append((str(directory), *file_state(info)[:2]))
            chapters = capture(work_path)
            if self.lib_type == LibType.COMIC:
                for chapter in sorted(chapters if work_path in selected else selected):
                    capture(chapter)
        except OSError as error:
            raise ContentError("media_source_unavailable") from error
        return hashlib.sha256(json.dumps(entries).encode()).hexdigest()

    def resolve_event_targets(
        self, event: FileSystemEvent, *, base_path: str
    ) -> dict[Path, set[Path]]:
        """Map an unchanged event to work and reading-unit containers.

        Ownership uses lexical paths so deleted and moved sources still map to
        their original work. Discovery rechecks directories without following links.

        Args:
            event: The original filesystem event, including both sides of a move.
            base_path: The absolute library root.

        Returns:
            Affected work paths mapped to their unit containers. A work-level
            target requires reconciling its layout; chapter targets remain distinct.

        Raises:
            ValueError: If the root is not an absolute normalized path.
        """
        result: dict[Path, set[Path]] = {}
        if event.event_type not in _EVENT_TYPES:
            return result
        root = Path(base_path)
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("library root must be an absolute normalized path")
        paths = [event.src_path]
        if event.event_type == EVENT_TYPE_MOVED:
            paths.append(event.dest_path)
        for value in paths:
            path = Path(os.fsdecode(value))
            try:
                parts = path.relative_to(root).parts
            except ValueError:
                continue
            if (
                not parts
                or ".." in parts
                or any(is_ignored_name(part) for part in parts)
            ):
                continue
            if not event.is_directory:
                if len(parts) < 2:
                    continue
                if self.lib_type == LibType.NOVEL:
                    accepted = (
                        path.suffix.casefold()
                        in _NOVEL_EXTENSIONS | IMAGE_EXTENSIONS | {".opf"}
                    )
                else:
                    accepted = (
                        path.suffix.casefold() in _COMIC_EXTENSIONS | IMAGE_EXTENSIONS
                        or path.name.casefold() == "comicinfo.xml"
                    )
                if not accepted:
                    continue
            work = root / parts[0]
            target = work
            if self.lib_type == LibType.COMIC and len(parts) > (
                1 if event.is_directory else 2
            ):
                target /= parts[1]
            result.setdefault(work, set()).add(target)
        return result

    def resolve_content_targets(
        self, event: FileSystemEvent, *, base_path: str
    ) -> dict[Path, set[Path]]:
        """Identify containers whose body events require an index rebuild.

        Metadata and named covers do not invalidate body indexes. Directory
        modifications only request a layout check. Creating, deleting or moving
        work and chapter containers may replace bodies while preserving their
        size and mtime; deeper auxiliary directories do not contain supported bodies.

        Args:
            event: The original filesystem event, including both sides of a move.
            base_path: The absolute library root.

        Returns:
            Work paths mapped to containers requiring an unconditional rebuild.

        Raises:
            ValueError: If the root is not an absolute normalized path.
        """
        scopes = self.resolve_event_targets(event, base_path=base_path)
        if not scopes:
            return {}
        if event.is_directory and event.event_type == EVENT_TYPE_MODIFIED:
            return {}
        paths = [event.src_path]
        if event.event_type == EVENT_TYPE_MOVED:
            paths.append(event.dest_path)
        result: dict[Path, set[Path]] = {}
        for value in paths:
            path = Path(os.fsdecode(value))
            if event.is_directory:
                for work, targets in self.resolve_event_targets(
                    DirModifiedEvent(value), base_path=base_path
                ).items():
                    if path in targets:
                        result.setdefault(work, set()).add(path)
                continue
            suffix = path.suffix.casefold()
            body = (
                suffix in _NOVEL_EXTENSIONS
                if self.lib_type == LibType.NOVEL
                else suffix in _COMIC_EXTENSIONS
                or (
                    suffix in IMAGE_EXTENSIONS
                    and path.stem.casefold() not in COVER_NAMES
                )
            )
            if body:
                for work, targets in self.resolve_event_targets(
                    FileModifiedEvent(value), base_path=base_path
                ).items():
                    result.setdefault(work, set()).update(targets)
        return result

    def filter_event(
        self, event: FileSystemEvent, *, base_path: str
    ) -> FileSystemEvent | None:
        """Accept relevant events while retaining only original move facts.

        Args:
            event: The filesystem event to classify without mutation.
            base_path: The absolute library root.

        Returns:
            The original event when it affects a work, otherwise None.
        """
        # inferred descendants are already covered by the original directory move
        if event.event_type == EVENT_TYPE_MOVED and event.is_synthetic:
            return None
        return event if self.resolve_event_targets(event, base_path=base_path) else None


_HANDLERS[LibType.NOVEL] = ReadingMediaHandler(LibType.NOVEL)
_HANDLERS[LibType.COMIC] = ReadingMediaHandler(LibType.COMIC)
