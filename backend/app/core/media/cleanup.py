"""Identify owned reading companions after a body file disappears."""

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
)
from app.core.media.metadata import parse_opf
from app.core.media.reader import _read_local
from app.models.media import MediaFormat


def reading_companions(source: ReadingSource) -> dict[Path, tuple[int, ...]]:
    """Find exclusive metadata and covers in a missing body's owned container.

    The caller validates library boundaries, ancestor identities, missing body and
    database ownership. Covers precede XML so interrupted cleanup retains references.
    Unknown metadata makes cover ownership uncertain; those covers stay untouched.

    Args:
        source: The validated TXT, EPUB, CBZ or ZIP source whose body is absent.

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
        if suffix in extensions or (
            not novel and suffix in IMAGE_EXTENSIONS and stem not in COVER_NAMES
        ):
            raise ContentError("content_changed")
        info = path.stat(follow_symlinks=False)
        if not novel and stat.S_ISDIR(info.st_mode):
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
