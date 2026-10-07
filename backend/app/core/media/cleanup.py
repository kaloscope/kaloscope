"""Identify owned reading files for explicit deletion and missing-body cleanup."""

import posixpath
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.core.media.common import ContentError, file_state
from app.core.media.epub.package import resolve_epub_reference
from app.core.media.handlers.reading import (
    COVER_NAMES,
    IMAGE_EXTENSIONS,
    ReadingSource,
    is_ignored_name,
    list_source_entries,
)
from app.core.media.metadata import parse_opf
from app.core.media.reader import _read_local
from app.models.media import MediaFormat


def reading_companions(
    source: ReadingSource,
    *,
    bodies: set[Path] | None = None,
    chapters: set[Path] | None = None,
) -> dict[Path, tuple[int, ...]]:
    """Find exclusive metadata and covers in an owned reading container.

    The caller validates library boundaries, ancestor identities, selected bodies and
    database ownership. Covers precede XML so interrupted cleanup retains references.
    Unknown metadata makes cover ownership uncertain; those covers stay untouched.

    Args:
        source: The validated reading source whose companions will be removed.
        bodies: Selected body files permitted in the container; None requires their
            absence for missing-body cleanup.
        chapters: Selected chapter containers permitted below a collection; None
            protects all potential chapters from companion cleanup.

    Returns:
        Ordered companion paths with snapshots, excluding links and unknown files.

    Raises:
        ContentError: If another body or a changed companion prevents safe cleanup.
        OSError: If the container or a candidate cannot be inspected.
    """
    novel = source.format in (MediaFormat.TXT, MediaFormat.EPUB)
    extensions = {".txt", ".epub"} if novel else {".cbz", ".zip"}
    names = (
        {f"{source.path.stem}.opf".casefold(), "content.opf", "metadata.opf"}
        if novel
        else {"comicinfo.xml"}
    )
    entries = list(source.directory.iterdir())
    metadata: dict[Path, tuple[int, ...]] = {}
    covers: set[Path] = set()
    shared = False
    for path in entries:
        suffix, stem = path.suffix.casefold(), path.stem.casefold()
        # include hidden and linked bodies when deciding whether companions are shared
        if path not in (bodies or ()) and (
            suffix in extensions
            or (not novel and suffix in IMAGE_EXTENSIONS and stem not in COVER_NAMES)
        ):
            raise ContentError("content_changed")
        info = path.stat(follow_symlinks=False)
        if not novel and stat.S_ISDIR(info.st_mode) and path not in (chapters or ()):
            # a former standalone archive may now contain another comic's chapters
            for child in path.iterdir():
                if child.suffix.casefold() in extensions | IMAGE_EXTENSIONS:
                    raise ContentError("content_changed")
        if path.name.casefold() in names:
            if not stat.S_ISREG(info.st_mode):
                raise ContentError("media_source_unavailable")
            metadata[path] = file_state(info)
        elif suffix in {".opf", ".xml", ".nfo"}:
            shared = True
        if (
            stat.S_ISREG(info.st_mode)
            and suffix in IMAGE_EXTENSIONS
            and stem in COVER_NAMES
        ):
            covers.add(path)
    if novel and not shared:
        for path, state in metadata.items():
            try:
                origin = _read_local(path, parse_opf)
            except ContentError as error:
                if error.code not in {"invalid_metadata", "media_limit_exceeded"}:
                    raise
                continue
            if file_state(path.stat(follow_symlinks=False)) != state:
                raise ContentError("content_changed")
            hint = origin.parsed.data.cover if origin.parsed else None
            if hint is None or hint.href is None:
                continue
            try:
                if resolve_epub_reference(path.name, hint.href) is None:
                    continue
            except ContentError:
                continue
            relative = posixpath.normpath(unquote(urlsplit(hint.href).path))
            if any(is_ignored_name(part) for part in Path(relative).parts):
                continue
            cover = source.directory / relative
            if cover.suffix.casefold() in IMAGE_EXTENSIONS:
                covers.add(cover)
    result: dict[Path, tuple[int, ...]] = {}
    for cover in sorted(covers) if not shared else ():
        try:
            parents = [
                parent
                for parent in cover.parents
                if parent.is_relative_to(source.directory)
            ]
            if not all(
                stat.S_ISDIR(parent.stat(follow_symlinks=False).st_mode)
                for parent in reversed(parents)
            ):
                continue
            info = cover.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            result[cover] = file_state(info)
    result.update(sorted(metadata.items()))
    return result


def reading_files(
    root: Path, sources: list[ReadingSource]
) -> tuple[
    dict[Path, tuple[int, ...]],
    dict[Path, tuple[int, ...]],
    dict[Path, tuple[int, ...]],
]:
    """Snapshot selected reading bodies and exclusive companions without recursion.

    Missing sources are allowed so explicit deletion can resume after partial writes.
    The library root must remain accessible. Image directories contribute visible
    direct pages; archives stay intact and collections only contribute companions.

    Args:
        root: The validated absolute library root, which must never be deleted.
        sources: Sources whose database ownership was checked under the library lock.

    Returns:
        Directory snapshots, body file snapshots and companion snapshots, with covers
        ordered before XML. Missing containers contribute no files.

    Raises:
        ContentError: If ownership, links, file types or library access are invalid.
        OSError: If a source or companion cannot be inspected.
    """
    states: dict[Path, tuple[int, ...]] = {}
    bodies: dict[Path, tuple[int, ...]] = {}
    companions: dict[Path, tuple[int, ...]] = {}
    for source in sources:
        if (
            not root.is_absolute()
            or source.directory == root
            or not source.directory.is_relative_to(root)
            or ".." in source.path.parts
        ):
            raise ContentError("media_source_unavailable")
        for directory in (*reversed(source.directory.parents), source.directory):
            try:
                info = directory.stat(follow_symlinks=False)
            except FileNotFoundError:
                if directory == root or not directory.is_relative_to(root):
                    raise ContentError("media_source_unavailable") from None
                break
            if not stat.S_ISDIR(info.st_mode):
                raise ContentError("media_source_unavailable")
            if directory.is_relative_to(root):
                current = file_state(info)
                if directory in states and states[directory] != current:
                    raise ContentError("content_changed")
                states[directory] = current
        if source.directory not in states or source.format is None:
            continue
        candidates = (
            [
                path
                for path in list_source_entries(source.directory)[0]
                if path.suffix.casefold() in IMAGE_EXTENSIONS
                and path.stem.casefold() not in COVER_NAMES
            ]
            if source.format == MediaFormat.DIR
            else [source.path]
        )
        for path in candidates:
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ContentError("media_source_unavailable")
            bodies[path] = file_state(info)
    selected_bodies = set(bodies)
    for source in sources:
        if source.directory not in states:
            continue
        companions.update(
            reading_companions(
                source,
                bodies=selected_bodies,
                chapters={
                    child.directory
                    for child in sources
                    if child.parent_path == source.path
                },
            )
        )
    for path in companions:
        for directory in reversed(path.parents):
            if directory.is_relative_to(root):
                info = directory.stat(follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise ContentError("media_source_unavailable")
                state = file_state(info)
                if directory in states and states[directory] != state:
                    raise ContentError("content_changed")
                states[directory] = state
    return (
        states,
        bodies,
        dict(
            sorted(
                companions.items(),
                key=lambda entry: (
                    entry[0].suffix.casefold() not in IMAGE_EXTENSIONS,
                    entry[0],
                ),
            )
        ),
    )
