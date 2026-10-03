"""Build text indexes and read bounded local content."""

import codecs
import hashlib
import io
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, BinaryIO, Literal, Self, TextIO

from charset_normalizer import from_bytes
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from app.core.media.common import INDEX_BYTES, ContentError, FileSnapshot, file_state
from app.core.media.epub.cache import (
    EpubContent,
    EpubIndex,
    build_epub_index,
    read_epub_chapter,
    read_epub_resource,
)
from app.models.media import MediaFormat

if TYPE_CHECKING:
    from app.core.media.handlers.reading import ReadingSource

_READ_BYTES = 64 * 1024
_READ_CHARS = 8192
_SECTION_BYTES = 256 * 1024
_MAX_CHAPTERS = 10_000
_HEADING = re.compile(
    r"^(?:第[0-9零〇一二三四五六七八九十百千万两壹贰叁肆伍陆柒捌玖拾佰仟]+[章节回卷部篇]"
    r"(?:\s.*|[：:、.．].*)?|(?:chapter|book|part)\s+(?:[0-9]+|[ivxlcdm]+)"
    r"(?:\s.*|[：:.\-].*)?|序章|序言|前言|楔子|尾声|后记|终章)$",
    re.IGNORECASE,
)
_BINARY_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class TextChapter(BaseModel):
    """Locate one bounded reading section in normalized UTF-8 text."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    title: str | None = Field(max_length=120)
    part: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


class TextIndex(BaseModel):
    """Describe a complete TXT cache without storing source paths or metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    format: Literal["txt"] = "txt"
    index_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot: FileSnapshot
    encoding: str = Field(min_length=1, max_length=64)
    text_size: int = Field(gt=0)
    chapters: tuple[TextChapter, ...] = Field(min_length=1, max_length=_MAX_CHAPTERS)

    @model_validator(mode="after")
    def check_ranges(self) -> Self:
        """Require contiguous bounded ranges and unique chapter identities.

        Returns:
            The validated index.

        Raises:
            ValueError: If ranges overlap, omit bytes or exceed section limits.
        """
        offset, identities = 0, set()
        for chapter in self.chapters:
            if (
                chapter.start != offset
                or not 0 < chapter.end - chapter.start <= _SECTION_BYTES
                or chapter.id in identities
            ):
                raise ValueError("invalid text chapter range")
            offset = chapter.end
            identities.add(chapter.id)
        if offset != self.text_size:
            raise ValueError("text cache size does not match chapter ranges")
        return self


_INDEX = TypeAdapter(Annotated[TextIndex | EpubIndex, Field(discriminator="format")])


def _text_encoding(file: BinaryIO) -> str:
    """Prefer a BOM or full strict UTF-8 validation before sampled detection.

    Args:
        file: The seekable source stream; the caller rewinds before decoding.

    Returns:
        The selected Python codec name.

    Raises:
        ContentError: If no sufficiently coherent legacy encoding is detected.
        OSError: If the source cannot be read.
    """
    prefix = file.read(_READ_BYTES)
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
        (codecs.BOM_UTF8, "utf-8-sig"),
    ):
        if prefix.startswith(bom):
            return encoding
    file.seek(0)
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        while chunk := file.read(_READ_BYTES):
            decoder.decode(chunk)
        decoder.decode(b"", final=True)
        return "utf-8"
    except UnicodeDecodeError as error:
        sample = error.object
        start = file.tell() - len(sample)
        if not prefix.isascii():
            sample, start = prefix, 0
        # an ASCII preface provides no evidence for selecting a legacy encoding
        start += next(index for index, byte in enumerate(sample) if byte >= 0x80)
        file.seek(start)
        sample = file.read(_READ_BYTES)

    # prefer a complete line; otherwise account for a truncated multibyte tail
    newline = sample.rfind(b"\n")
    if newline >= len(sample) // 2:
        sample = sample[: newline + 1]
    trims = (
        range(4)
        if len(sample) == _READ_BYTES and not sample.endswith(b"\n")
        else range(1)
    )
    matches = []
    for trim in trims:
        candidates = from_bytes(
            sample[: len(sample) - trim],
            threshold=0.1,
            enable_fallback=False,
            preemptive_behaviour=False,
        )
        match = candidates.best()
        if match is None or match.coherence < 0.1:
            continue
        # the detector groups codecs that produce identical text into one match
        ambiguous = any(
            candidate.encoding != match.encoding
            and abs(candidate.chaos - match.chaos) < 0.005
            and abs(candidate.coherence - match.coherence) < 0.02
            for candidate in candidates
        )
        matches.append((match, ambiguous))
    if not matches:
        raise ContentError("text_decode_failed")
    match, ambiguous = max(
        matches, key=lambda entry: (entry[0].coherence, -entry[0].chaos)
    )
    if ambiguous:
        raise ContentError("text_decode_failed")
    return match.encoding


def _write_text(text: TextIO, output: BinaryIO) -> tuple[TextChapter, ...]:
    """Preserve normalized text while splitting chapters into bounded sections.

    Args:
        text: A strictly decoded stream using universal newline translation.
        output: The new binary UTF-8 cache stream.

    Returns:
        Ordered chapter ranges covering every written byte exactly once.

    Raises:
        ContentError: If content is empty, binary or exceeds the chapter limit.
        UnicodeDecodeError: If the selected encoding cannot decode the whole file.
        OSError: If source reads or cache writes fail.
    """
    chapters = []
    buffer = bytearray()
    title, part = None, 1
    line_start, has_text = True, False

    def emit(size: int):
        """Write and index the next complete UTF-8 prefix.

        Args:
            size: The number of buffered bytes to emit.
        """
        nonlocal part
        if len(chapters) >= _MAX_CHAPTERS:
            raise ContentError("media_limit_exceeded")
        start = output.tell()
        output.write(buffer[:size])
        del buffer[:size]
        chapters.append(
            TextChapter(
                id=hashlib.sha256(f"txt:{start}".encode()).hexdigest()[:32],
                title=title,
                part=part,
                start=start,
                end=output.tell(),
            )
        )
        part += 1

    while line := text.readline(_READ_CHARS):
        if _BINARY_CONTROLS.search(line):
            raise ContentError("text_decode_failed")
        stripped = line.strip()
        complete = line.endswith("\n") or len(line) < _READ_CHARS
        if (
            line_start
            and complete
            and len(stripped) <= 120
            and _HEADING.fullmatch(stripped)
        ):
            if buffer and buffer.decode("utf-8").strip():
                emit(len(buffer))
            title, part = stripped, 1
        buffer.extend(line.encode("utf-8"))
        has_text |= bool(stripped)
        line_start = line.endswith("\n")
        while len(buffer) > _SECTION_BYTES:
            cut = buffer.rfind(b"\n\n", 0, _SECTION_BYTES)
            if cut >= 0:
                cut += 2
            else:
                cut = buffer.rfind(b"\n", 0, _SECTION_BYTES) + 1
            if not cut:
                cut = _SECTION_BYTES
                while buffer[cut] & 0xC0 == 0x80:
                    cut -= 1
            emit(cut)
    if not has_text:
        raise ContentError("empty_content")
    if buffer:
        emit(len(buffer))
    return tuple(chapters)


def build_text_index(source: "ReadingSource", cache_dir: Path) -> TextIndex | EpubIndex:
    """Build a TXT or EPUB cache in a new caller-owned staging directory.

    Run this synchronous operation in a worker thread. The caller validates the
    library boundary and publishes the completed directory under the library lock.
    Existing directories are never overwritten; failed new builds are removed.

    Args:
        source: The novel reading source discovered by its library handler.
        cache_dir: A nonexistent cache directory whose parent already exists.

    Returns:
        The index saved beside content.txt for TXT or content.jsonl for EPUB.

    Raises:
        ContentError: If the source is unsafe, unsupported, empty, unstable,
            undecodable or over limits.
        OSError: If files cannot be accessed, or the cache directory already exists.
    """
    if source.format == MediaFormat.EPUB:
        return build_epub_index(source.path, cache_dir)
    if source.format != MediaFormat.TXT:
        raise ContentError("unsupported_media_format")
    cache_dir.mkdir()
    try:
        before = source.path.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or source.path.parent.is_symlink():
            raise ContentError("media_source_unavailable")
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(os.open(source.path, flags), "rb") as file:
            if file_state(os.fstat(file.fileno())) != file_state(before):
                raise ContentError("content_changed")
            encoding = _text_encoding(file)
            file.seek(0)
            with (
                io.TextIOWrapper(
                    file, encoding=encoding, errors="strict", newline=None
                ) as text,
                (cache_dir / "content.txt").open("xb") as output,
            ):
                try:
                    chapters = _write_text(text, output)
                except UnicodeDecodeError as error:
                    raise ContentError("text_decode_failed") from error
                text_size = output.tell()
        try:
            current = source.path.stat(follow_symlinks=False)
        except FileNotFoundError as error:
            raise ContentError("content_changed") from error
        if file_state(current) != file_state(before):
            raise ContentError("content_changed")
        index = TextIndex(
            index_version=secrets.token_hex(32),
            source_snapshot=FileSnapshot(
                size=before.st_size, mtime_ns=before.st_mtime_ns
            ),
            encoding=encoding,
            text_size=text_size,
            chapters=chapters,
        )
        data = index.model_dump_json().encode("utf-8")
        if len(data) > INDEX_BYTES:
            raise ContentError("media_limit_exceeded")
        (cache_dir / "index.json").write_bytes(data)
        return index
    except BaseException:
        shutil.rmtree(cache_dir)
        raise


def load_text_index(cache_dir: Path) -> TextIndex | EpubIndex:
    """Validate a persisted novel index and the size of its complete body cache.

    Args:
        cache_dir: A completed internal cache directory selected by the caller.

    Returns:
        The validated index without reading the entire cached body.

    Raises:
        ContentError: If the cache is missing, corrupt or from an unsupported schema.
    """
    try:
        with (cache_dir / "index.json").open("rb") as file:
            data = file.read(INDEX_BYTES + 1)
        if len(data) > INDEX_BYTES:
            raise ContentError("content_not_ready")
        index = _INDEX.validate_json(data)
        filename, size = (
            ("content.txt", index.text_size)
            if isinstance(index, TextIndex)
            else ("content.jsonl", index.content_size)
        )
        if (cache_dir / filename).stat().st_size != size:
            raise ContentError("content_not_ready")
        return index
    except (OSError, ValidationError) as error:
        raise ContentError("content_not_ready") from error


def read_text_chapter(cache_dir: Path, chapter_id: str) -> list[str] | EpubContent:
    """Read one indexed novel section without interpreting its ID as a path.

    Args:
        cache_dir: A completed internal cache directory selected by the caller.
        chapter_id: An exact chapter ID from that cache's index.

    Returns:
        TXT paragraph strings, joined losslessly with two newlines, or validated
        EPUB blocks and warnings. Neither form contains resource URLs.

    Raises:
        ContentError: If the chapter is unknown or the cache is no longer readable.
    """
    index = load_text_index(cache_dir)
    if isinstance(index, EpubIndex):
        return read_epub_chapter(cache_dir, index, chapter_id)
    chapter = next((entry for entry in index.chapters if entry.id == chapter_id), None)
    if chapter is None:
        raise ContentError("not_found")
    try:
        with (cache_dir / "content.txt").open("rb") as file:
            file.seek(chapter.start)
            data = file.read(chapter.end - chapter.start)
        if len(data) != chapter.end - chapter.start:
            raise ContentError("content_not_ready")
        return data.decode("utf-8").split("\n\n")
    except (OSError, UnicodeDecodeError) as error:
        raise ContentError("content_not_ready") from error


def read_text_resource(
    source_path: Path, cache_dir: Path, resource_id: str
) -> tuple[bytes, str]:
    """Read a novel's indexed raster image in a caller-authorized worker.

    Args:
        source_path: The current source inside the validated library boundary.
        cache_dir: The completed internal cache directory for that source version.
        resource_id: An exact image ID from the current EPUB index.

    Returns:
        Image bytes and their validated MIME type.

    Raises:
        ContentError: If the source has no such image, changed or cannot be read.
    """
    index = load_text_index(cache_dir)
    if not isinstance(index, EpubIndex):
        raise ContentError("not_found")
    return read_epub_resource(source_path, index, resource_id)
