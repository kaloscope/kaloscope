"""Read current local metadata sources without persisting details or serving assets."""

import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Literal

from app.core.media.archive import normalize_member_path, open_archive, read_member
from app.core.media.common import ContentError, file_state
from app.core.media.epub.package import read_epub_opf
from app.core.media.handlers.reading import ReadingSource, is_ignored_name
from app.core.media.metadata import (
    METADATA_BYTES,
    PARENT_FIELDS,
    ParsedMetadata,
    ReadingMetadata,
    merge_metadata,
    parse_comicinfo,
    parse_opf,
)
from app.models.media import MediaFormat


@dataclass(frozen=True)
class MetadataSource:
    """Keep one current source, its parse result or its controlled read error.

    Paths and archive members are internal ownership information, never public
    resource URLs. The signature describes XML bytes, not the containing archive.
    """

    path: Path
    member: str | None = None
    parsed: ParsedMetadata | None = None
    signature: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class MetadataRead:
    """Keep details and independent source outcomes for the current operation."""

    data: ReadingMetadata
    external: MetadataSource | None
    embedded: MetadataSource | None
    parent: MetadataSource | None

    @property
    def state(self) -> Literal["ready", "missing", "error"]:
        """Describe this read without hiding a failed source behind fallback fields.

        Returns:
            Error for any failed read, ready for parsed sources, otherwise missing.
        """
        sources = (self.external, self.embedded, self.parent)
        if any(source and source.error for source in sources):
            return "error"
        return (
            "ready"
            if any(source and source.parsed is not None for source in sources)
            else "missing"
        )

    @property
    def has_local_metadata(self) -> bool:
        """Check for valid item-owned metadata independently of parent inheritance.

        Returns:
            Whether either item source parsed, even with missing optional fields.
        """
        return any(
            source and source.parsed is not None
            for source in (self.external, self.embedded)
        )

    @property
    def cover_source(self) -> MetadataSource | None:
        """Retain the owning source for the selected, unresolved cover hint.

        Returns:
            The external or embedded source supplying a cover, or None if absent.
        """
        return next(
            (
                source
                for source in (self.external, self.embedded)
                if source and source.parsed and source.parsed.data.cover is not None
            ),
            None,
        )

    def summary(self) -> dict[str, str | int | Decimal | None]:
        """Extract the supported list columns without storing complete details.

        Missing values remain explicit so successful synchronization can clear old
        summaries. The caller handles failed reads and resolves posters separately.

        Returns:
            Title bounded to the existing column, year and rating only.
        """
        return {
            "title": self.data.title[:255] if self.data.title is not None else None,
            "year": self.data.year,
            "rating": self.data.rating,
        }


def _find_external(directory: Path, names: tuple[str, ...]) -> Path | None:
    """Select one direct metadata file by filename priority without following links.

    Args:
        directory: The already validated work or chapter container.
        names: Preferred case-insensitive filenames in priority order.

    Returns:
        The selected path, or None when all candidates are absent.

    Raises:
        ContentError: If the selected filename has ambiguous case variants.
        OSError: If the directory cannot be enumerated.
    """
    candidates: dict[str, list[Path]] = {}
    with os.scandir(directory) as entries:
        for entry in entries:
            name = entry.name.casefold()
            if name in names:
                candidates.setdefault(name, []).append(Path(entry.path))
    for name in names:
        matches = candidates.get(name, [])
        if len(matches) > 1:
            raise ContentError("ambiguous_metadata")
        if matches:
            return matches[0]
    return None


def _read_local(
    path: Path, parser: Callable[[bytes], ParsedMetadata]
) -> MetadataSource:
    """Read and parse bounded XML while checking the open file and current path.

    Args:
        path: The selected metadata file in a caller-validated container.
        parser: The OPF or ComicInfo byte parser.

    Returns:
        Current parsed fields and the full XML byte signature.

    Raises:
        ContentError: If the file is unsafe, oversized, changing or invalid.
        OSError: If source inspection or reading fails.
    """
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ContentError("media_source_unavailable")
    if before.st_size > METADATA_BYTES:
        raise ContentError("media_limit_exceeded")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        if file_state(os.fstat(file.fileno())) != file_state(before):
            raise ContentError("content_changed")
        try:
            data = file.read(METADATA_BYTES + 1)
            if len(data) > METADATA_BYTES:
                raise ContentError("media_limit_exceeded")
            parsed = parser(data)
            return MetadataSource(
                path=path,
                parsed=parsed,
                signature=hashlib.sha256(data).hexdigest(),
            )
        finally:
            try:
                after = path.stat(follow_symlinks=False)
            except FileNotFoundError as error:
                raise ContentError("content_changed") from error
            if file_state(after) != file_state(before) or file_state(
                os.fstat(file.fileno())
            ) != file_state(before):
                raise ContentError("content_changed")


def _read_external(
    directory: Path, names: tuple[str, ...], parser: Callable[[bytes], ParsedMetadata]
) -> MetadataSource | None:
    """Discover current external XML and retry one concurrent change.

    Args:
        directory: A caller-validated work or chapter container.
        names: Preferred filenames in casefolded form.
        parser: The parser for all candidate files.

    Returns:
        A parsed source, a controlled error or None for a stable absence.
    """
    for attempt in range(2):
        path = directory / names[0]
        before = None
        try:
            before = directory.stat(follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise ContentError("media_source_unavailable")
            try:
                selected = _find_external(directory, names)
                if selected is None:
                    return None
                path = selected
                result = _read_local(path, parser)
            finally:
                if file_state(directory.stat(follow_symlinks=False)) != file_state(
                    before
                ):
                    raise ContentError("content_changed")
            return result
        except (ContentError, OSError) as error:
            code = (
                error.code
                if isinstance(error, ContentError)
                else "content_changed"
                if isinstance(error, FileNotFoundError) and before is not None
                else "media_source_unavailable"
            )
            if code == "content_changed" and attempt == 0:
                continue
            return MetadataSource(path=path, error=code)
    raise AssertionError("metadata read attempts exhausted")


def _read_embedded(source: ReadingSource) -> MetadataSource | None:
    """Read only the selected package metadata with one retry for source changes.

    Args:
        source: A caller-validated EPUB, CBZ or ZIP source.

    Returns:
        Parsed metadata, a controlled error or None for absent comic metadata.
    """
    for attempt in range(2):
        member = None
        try:
            with open_archive(source.path) as (archive, _):
                if source.format == MediaFormat.EPUB:
                    member, data = read_epub_opf(archive)
                    parsed = parse_opf(data)
                else:
                    candidates = []
                    for info in archive.infolist():
                        path = PurePosixPath(normalize_member_path(info.filename))
                        if (
                            not info.is_dir()
                            and path.name.casefold() == "comicinfo.xml"
                            and not any(is_ignored_name(part) for part in path.parts)
                        ):
                            candidates.append(info)
                    roots = [
                        info
                        for info in candidates
                        if "/" not in normalize_member_path(info.filename)
                    ]
                    candidates = roots or candidates
                    if len(candidates) > 1:
                        raise ContentError("ambiguous_metadata")
                    if not candidates:
                        return None
                    member = candidates[0].filename
                    data = read_member(archive, candidates[0], METADATA_BYTES)
                    parsed = parse_comicinfo(data)
                return MetadataSource(
                    path=source.path,
                    member=member,
                    parsed=parsed,
                    signature=hashlib.sha256(data).hexdigest(),
                )
        except (ContentError, OSError) as error:
            code = (
                error.code
                if isinstance(error, ContentError)
                else "media_source_unavailable"
            )
            if code == "content_changed" and attempt == 0:
                continue
            return MetadataSource(path=source.path, member=member, error=code)
    raise AssertionError("metadata read attempts exhausted")


def read_metadata(source: ReadingSource) -> MetadataRead:
    """Read current source files, fill missing fields and retain independent errors.

    Run this synchronous operation in a worker after checking library permission,
    visibility, layout and the complete path boundary, including ancestor symlinks.
    Parent ownership must also be verified against the same library. No index or
    previous metadata result is consulted, and no source or cache is written.

    Args:
        source: The current item source with an optional verified direct comic parent.

    Returns:
        Fresh transient details, a local-title fallback and each attempted source.
        Cover references remain unresolved and must not be served directly.

    Raises:
        ValueError: If paths are not absolute, contain traversal or have an invalid
            parent relationship for the supplied reading format.
    """
    directory = source.directory
    novel = source.format in (MediaFormat.TXT, MediaFormat.EPUB)
    if (
        not source.path.is_absolute()
        or ".." in source.path.parts
        or (
            source.parent_path is not None
            and (
                novel or source.format is None or source.parent_path != directory.parent
            )
        )
    ):
        raise ValueError("invalid reading source ownership")
    names = (
        tuple(
            dict.fromkeys(
                (f"{source.path.stem}.opf".casefold(), "content.opf", "metadata.opf")
            )
        )
        if novel
        else ("comicinfo.xml",)
    )
    external = _read_external(directory, names, parse_opf if novel else parse_comicinfo)
    external_data = external.parsed.data if external and external.parsed else None
    embedded = None
    if source.format in (MediaFormat.EPUB, MediaFormat.CBZ, MediaFormat.ZIP) and (
        external_data is None
        or any(
            getattr(external_data, name) in (None, ())
            for name in ReadingMetadata.model_fields
        )
    ):
        embedded = _read_embedded(source)
    embedded_data = embedded.parsed.data if embedded and embedded.parsed else None
    data = merge_metadata(external_data, embedded_data)
    parent = None
    if source.parent_path is not None and any(
        getattr(data, name) in (None, ()) for name in PARENT_FIELDS
    ):
        parent = _read_external(source.parent_path, ("comicinfo.xml",), parse_comicinfo)
        data = merge_metadata(
            external_data,
            embedded_data,
            parent=parent.parsed.data if parent and parent.parsed else None,
        )
    if data.title is None:
        title = (
            source.path.stem
            if source.format not in (None, MediaFormat.DIR)
            else source.path.name
        )
        data = data.model_copy(update={"title": title})
    return MetadataRead(data=data, external=external, embedded=embedded, parent=parent)
