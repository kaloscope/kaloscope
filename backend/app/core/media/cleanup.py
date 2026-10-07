"""Identify owned media files for explicit deletion and missing-body cleanup."""

import mimetypes
import os
import posixpath
import stat
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

from app.core.media.common import INDEX_BYTES, ContentError, file_state
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
from app.models.media import MediaFormat, MediaItem


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


def video_files(
    root: Path, items: list[MediaItem], others: list[MediaItem]
) -> tuple[
    dict[Path, tuple[int, ...]],
    dict[Path, tuple[int, ...]],
    dict[Path, tuple[int, ...]],
]:
    """Snapshot selected videos and their exclusive NFOs, subtitles and artwork.

    Args:
        root: The absolute library root, which must remain accessible.
        items: The selected current rows, including any selected directory parents.
        others: Other registered items used to protect shared files.

    Returns:
        Directory, body and companion snapshots, with artwork preceding NFO files.

    Raises:
        ContentError: If a selected source changes or escapes the library.
        OSError: If library contents cannot be inspected.
    """
    from app.core.media.organizer import (
        _companions,
        _reference_key,
        _safe_path,
        _shared_artwork,
    )

    states: dict[Path, tuple[int, ...]] = {}
    bodies: dict[Path, tuple[int, ...]] = {}
    selected = {Path(item.path) for item in items}
    directories = {Path(item.dir) for item in items}
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ContentError("media_source_unavailable")
    for path in selected | directories:
        try:
            _safe_path(root, path)
        except ValueError as error:
            raise ContentError("media_source_unavailable") from error
        for directory in (*reversed(path.parents), path):
            if not directory.is_relative_to(root):
                continue
            try:
                info = directory.stat(follow_symlinks=False)
            except FileNotFoundError:
                break
            if stat.S_ISDIR(info.st_mode):
                state = file_state(info)
                if directory in states and states[directory] != state:
                    raise ContentError("content_changed")
                states[directory] = state
            elif directory in directories or directory != path:
                raise ContentError("media_source_unavailable")
    for item in items:
        path, directory = Path(item.path), Path(item.dir)
        if path == directory:
            if path == root:
                raise ContentError("media_source_unavailable")
            continue
        if path.parent != directory:
            raise ContentError("media_source_unavailable")
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise ContentError("media_source_unavailable")
        bodies[path] = file_state(info)

    # inspect unindexed videos and aliases as well as registered ownership
    videos = {Path(item.path) for item in others if item.path != item.dir}
    aliases = []
    for path in root.rglob("*"):
        if path.is_symlink():
            aliases.append(path)
        if (mimetypes.guess_file_type(path)[0] or "").startswith("video/"):
            videos.add(path)
    videos -= selected
    references = {
        _reference_key(alias.parent / os.readlink(alias))
        for alias in aliases
        if alias not in selected
    }

    def is_shared(path: Path) -> bool:
        """Check whether an unselected file or directory alias refers to a path.

        Args:
            path: The proposed deletion candidate.

        Returns:
            Whether removing the candidate would break a surviving alias.
        """
        key = _reference_key(path)
        return any(key == ref or key.startswith(ref + os.sep) for ref in references)

    if any(is_shared(path) for path in bodies):
        raise ContentError("content_changed")
    protected = set()
    for item in others:
        path, directory = Path(item.path), Path(item.dir)
        name = path.name if path == directory else path.stem
        protected.add(
            Path(item.nfo_path) if item.nfo_path else directory / f"{name}.nfo"
        )
    for path in videos:
        if path.parent in directories and path.parent in states:
            protected.update(_companions(path))
    candidates: set[Path] = set()
    for item in items:
        path, directory = Path(item.path), Path(item.dir)
        if directory not in states:
            continue
        if path != directory:
            candidates.update(_companions(path))
        elif any(other.is_relative_to(directory) for other in videos):
            continue
        else:
            candidates.update(
                child
                for child in directory.iterdir()
                if child.suffix.casefold() in IMAGE_EXTENSIONS
                and child.stem.casefold() in (*COVER_NAMES, "fanart", "backdrop")
            )
        nfo = (
            Path(item.nfo_path)
            if item.nfo_path
            else directory / f"{path.name if path == directory else path.stem}.nfo"
        )
        if nfo.parent == directory and nfo.suffix.casefold() == ".nfo":
            candidates.update(
                child
                for child in directory.iterdir()
                if child.name.casefold() == nfo.name.casefold()
            )
    protected_keys = {_reference_key(path) for path in protected}
    candidates = {
        path
        for path in candidates
        if _reference_key(path) not in protected_keys and not is_shared(path)
    }

    nfos = {
        path
        for path in candidates
        if path.suffix.casefold() == ".nfo" and path.is_file() and not path.is_symlink()
    }
    for nfo in nfos:
        before = file_state(nfo.stat(follow_symlinks=False))
        try:
            with nfo.open("rb") as stream:
                data = stream.read(INDEX_BYTES + 1)
            if len(data) > INDEX_BYTES:
                continue
            tree = etree.fromstring(
                data,
                etree.XMLParser(recover=False, resolve_entities=False, no_network=True),
            )
        except etree.LxmlError:
            continue
        if file_state(nfo.stat(follow_symlinks=False)) != before:
            raise ContentError("content_changed")
        for element in (
            tree.findall("./art/poster")
            + tree.findall("./art/fanart")
            + tree.findall("./thumb")
        ):
            value = (element.text or "").strip()
            if not value or urlsplit(value).scheme or value.startswith("//"):
                continue
            path = Path(os.path.normpath(nfo.parent / value))
            if (
                path.is_relative_to(nfo.parent)
                and path.suffix.casefold() in IMAGE_EXTENSIONS
            ):
                candidates.add(path)
    artwork = {
        path for path in candidates if path.suffix.casefold() in IMAGE_EXTENSIONS
    }
    if artwork:
        candidates -= _shared_artwork(root, artwork, nfos)
    companions: dict[Path, tuple[int, ...]] = {}
    for path in sorted(
        candidates, key=lambda path: (path.suffix.casefold() == ".nfo", path)
    ):
        try:
            _safe_path(root, path)
            info = path.stat(follow_symlinks=False)
        except (FileNotFoundError, ValueError):
            continue
        if not stat.S_ISREG(info.st_mode) or is_shared(path):
            continue
        companions[path] = file_state(info)
        for parent in path.parents:
            if parent.is_relative_to(root):
                state = file_state(parent.stat(follow_symlinks=False))
                if parent in states and states[parent] != state:
                    raise ContentError("content_changed")
                states[parent] = state

    return states, bodies, companions
