"""Publish external reading metadata and describe its recoverable write task."""

import hashlib
import os
import stat
from contextlib import suppress
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.core.media.common import ContentError, file_state
from app.core.media.handlers.reading import ReadingSource
from app.core.media.metadata import ReadingMetadata, parse_comicinfo, parse_opf
from app.core.media.reader import _read_local, read_metadata
from app.models.media import MediaFormat
from app.utils.disk import rename_exclusive


class MetadataWrite(BaseModel):
    """Keep one server-authorized candidate until its file and summary are saved."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    item_id: int = Field(gt=0)
    metadata: ReadingMetadata
    overwrite: bool
    identifier: UUID
    published: bool = False
    error_code: str | None = Field(default=None, min_length=1, max_length=64)


def _metadata_paths(source: ReadingSource) -> tuple[Path, list[Path]]:
    """Resolve a canonical sidecar and higher-priority OPFs owned by the source.

    Args:
        source: The source whose container has already been validated by the caller.

    Returns:
        The fixed-format destination and existing OPFs superseded by publication.

    Raises:
        ContentError: If a metadata name is ambiguous or is not a regular file.
        OSError: If the container cannot be inspected.
    """
    novel = source.format in (MediaFormat.TXT, MediaFormat.EPUB)
    name = "metadata.opf" if novel else "ComicInfo.xml"
    names = (
        {f"{source.path.stem}.opf".casefold(), "content.opf", "metadata.opf"}
        if novel
        else {name.casefold()}
    )
    matches: dict[str, Path] = {}
    for path in source.directory.iterdir():
        key = path.name.casefold()
        if key not in names:
            continue
        if key in matches:
            raise ContentError("ambiguous_metadata")
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise ContentError("media_source_unavailable")
        matches[key] = path
    target = matches.pop(name.casefold(), source.directory / name)
    return target, list(matches.values())


def write_metadata(
    source: ReadingSource,
    data: bytes,
    states: dict[Path, tuple[int, ...]],
    *,
    overwrite: bool,
) -> bool:
    """Publish complete XML and retire superseded OPFs under the caller's library lock.

    The caller validates source ownership and checks the mutable snapshots after
    writing. Its container timestamp is advanced after writing or accepting valid
    local metadata; body and ancestor snapshots still require a match.

    Args:
        source: The current validated source, never a client-supplied output path.
        data: Complete XML bytes generated and round-trip validated from the candidate.
        states: Source and ancestor snapshots updated after this writer's own changes.
        overwrite: True for an explicit manual save; False only creates absent metadata.

    Returns:
        True after publication or recovery of identical bytes; False when an automatic
        task yields to valid existing external or embedded metadata.

    Raises:
        ContentError: If ownership is ambiguous or reading or publication fails.
    """
    parser = (
        parse_opf
        if source.format in (MediaFormat.TXT, MediaFormat.EPUB)
        else parse_comicinfo
    )
    signature = hashlib.sha256(data).hexdigest()
    temporary = source.directory / f".metadata-{uuid4().hex}.tmp"
    created = False

    def existing() -> bool:
        """Check automatic-save eligibility against current item-owned metadata.

        Returns:
            Whether valid external or embedded metadata should be used instead.

        Raises:
            ContentError: If an item-owned source is present but cannot be parsed.
        """
        metadata = read_metadata(source)
        if metadata.has_local_metadata:
            # the caller rechecks body ownership after accepting newly added metadata
            states[source.directory] = file_state(source.directory.stat())
            return True
        for origin in (metadata.external, metadata.embedded):
            if origin is not None and origin.error:
                raise ContentError(origin.error)
        return False

    try:
        target, superseded = _metadata_paths(source)
        if not overwrite and existing():
            return False
        cover = parser(data).data.cover
        if cover is not None and cover.href is not None:
            from app.core.media.cover import _read_local_cover, _resolve_cover

            relative = _resolve_cover(target.name, cover.href)
            if (
                relative is None
                or _read_local_cover(source.directory, relative) is None
            ):
                raise ContentError("invalid_metadata")
        identical = False
        if target.exists():
            try:
                identical = _read_local(target, parser).signature == signature
            except ContentError as error:
                if error.code not in {"invalid_metadata", "media_limit_exceeded"}:
                    raise
        if not identical:
            with temporary.open("xb") as stream:
                created = True
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            states[source.directory] = file_state(source.directory.stat())
            _read_local(temporary, parser)
            if not overwrite and existing():
                return False
            if any(
                file_state(path.stat(follow_symlinks=False)) != state
                for path, state in states.items()
            ):
                raise ContentError("content_changed")
            target, superseded = _metadata_paths(source)
            if not overwrite and existing():
                return False
            if overwrite:
                if target.exists():
                    temporary.chmod(target.stat(follow_symlinks=False).st_mode & 0o777)
                os.replace(temporary, target)
            else:
                try:
                    rename_exclusive(temporary, target)
                except FileExistsError:
                    if existing():
                        return False
                    raise
            states[source.directory] = file_state(source.directory.stat())
        # publishing first keeps recoverable metadata if removing an older OPF fails
        if overwrite:
            for path in superseded:
                if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    raise ContentError("media_source_unavailable")
                path.unlink()
                states[source.directory] = file_state(source.directory.stat())
        if _read_local(target, parser).signature != signature:
            raise ContentError("metadata_write_failed")
        return True
    except OSError as error:
        raise ContentError("metadata_write_failed") from error
    finally:
        if created and temporary.exists():
            with suppress(OSError):
                temporary.unlink()
                states[source.directory] = file_state(source.directory.stat())
