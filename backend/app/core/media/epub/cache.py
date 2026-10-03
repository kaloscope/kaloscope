"""Build EPUB chapter caches and read bounded chapters and raster resources."""

import hashlib
import re
import secrets
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Literal, Self
from zipfile import ZipFile

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.media.archive import normalize_member_path, open_archive, read_member
from app.core.media.common import INDEX_BYTES, ContentError, FileSnapshot
from app.core.media.epub.package import (
    EpubResource,
    load_epub_package,
    read_epub_titles,
)
from app.core.media.epub.xhtml import (
    ContentBlock,
    ContentWarning,
    ImageBlock,
    ListBlock,
    TextBlock,
    TextRun,
    WarningCode,
    read_xhtml_document,
)
from app.core.media.raster import IMAGE_BYTES, ImageMime, image_mime

_SECTION_BYTES = 256 * 1024
_MAX_CHAPTERS = 10_000
_ID = re.compile(r"^[0-9a-f]{32}$")
_BLOCK = TypeAdapter(ContentBlock)
_WARNINGS = TypeAdapter(tuple[ContentWarning, ...])


class EpubChapter(BaseModel):
    """Locate one bounded section of serialized reading blocks."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    title: str | None = Field(max_length=120)
    part: int = Field(ge=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EpubAsset(BaseModel):
    """Locate verified image bytes without copying them into the cache."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    member: str = Field(min_length=1, max_length=4096)
    mime_type: ImageMime
    size: int = Field(gt=0, le=IMAGE_BYTES)
    crc: int = Field(ge=0, le=0xFFFFFFFF)

    @field_validator("member")
    @classmethod
    def check_member(cls, member: str) -> str:
        """Keep the exact ZIP name after checking its normalized boundary.

        Args:
            member: The cached archive member name, including its original spelling.

        Returns:
            The unchanged member name for exact ZIP lookup.

        Raises:
            ValueError: If the member path is unsafe.
        """
        normalize_member_path(member)
        return member


class EpubIndex(BaseModel):
    """Keep spine sections and validated image locations without book metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    format: Literal["epub"] = "epub"
    index_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot: FileSnapshot
    content_size: int = Field(gt=0)
    chapters: tuple[EpubChapter, ...] = Field(min_length=1, max_length=_MAX_CHAPTERS)
    assets: tuple[EpubAsset, ...] = Field(max_length=20_000)

    @model_validator(mode="after")
    def check_entries(self) -> Self:
        """Require contiguous ranges and unique section and image identities.

        Returns:
            The validated index.

        Raises:
            ValueError: If ranges or resource identities are inconsistent.
        """
        offset, identities = 0, set()
        for chapter in self.chapters:
            if (
                chapter.start != offset
                or not 0 < chapter.end - chapter.start <= _SECTION_BYTES
                or chapter.id in identities
            ):
                raise ValueError("invalid EPUB chapter range")
            offset = chapter.end
            identities.add(chapter.id)
        if offset != self.content_size:
            raise ValueError("EPUB cache size does not match chapter ranges")
        paths = set()
        for asset in self.assets:
            path = normalize_member_path(asset.member)
            identifier = hashlib.sha256(f"epub:{path}".encode()).hexdigest()[:32]
            if asset.id != identifier or path in paths:
                raise ValueError("invalid EPUB image identity")
            paths.add(path)
        return self


class EpubContent(BaseModel):
    """Keep validated chapter blocks and warnings without resource paths or URLs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    blocks: tuple[ContentBlock, ...] = Field(min_length=1)
    warnings: tuple[ContentWarning, ...] = ()

    @model_validator(mode="after")
    def check_blocks(self) -> Self:
        """Validate block identities, conditional fields and warning references.

        Returns:
            The validated section.

        Raises:
            ValueError: If block fields or warning references are inconsistent.
        """
        identities = set()
        for block in self.blocks:
            if not _ID.fullmatch(block.id) or block.id in identities:
                raise ValueError("invalid EPUB block identity")
            identities.add(block.id)
            runs: tuple[TextRun, ...] = ()
            if isinstance(block, TextBlock):
                if (block.type == "heading" and block.level not in range(1, 7)) or (
                    block.type != "heading" and block.level is not None
                ):
                    raise ValueError("invalid EPUB heading level")
                runs = block.runs
            elif isinstance(block, ListBlock):
                if not block.items or (not block.ordered and block.start is not None):
                    raise ValueError("invalid EPUB list")
                runs = tuple(run for item in block.items for run in item)
            elif block.asset_id is not None and not _ID.fullmatch(block.asset_id):
                raise ValueError("invalid EPUB image reference")
            if any(
                not run.text or len(set(run.marks)) != len(run.marks) for run in runs
            ):
                raise ValueError("invalid EPUB text runs")
        seen = set()
        for warning in self.warnings:
            key = (warning.code, warning.block_id)
            if warning.block_id not in identities or key in seen:
                raise ValueError("invalid EPUB warning reference")
            seen.add(key)
        return self


def _split_runs(runs: tuple[TextRun, ...]) -> tuple[tuple[TextRun, ...], ...]:
    """Bisect text without losing characters or emphasis spans.

    Args:
        runs: Nonempty spans from a block that exceeds the byte budget.

    Returns:
        Two nonempty groups covering the original text exactly once.

    Raises:
        ContentError: If even one character cannot fit the configured budget.
    """
    cut = sum(len(run.text) for run in runs) // 2
    if not cut:
        raise ContentError("media_limit_exceeded")
    left, right = [], []
    for run in runs:
        position = min(cut, len(run.text))
        if position:
            left.append(TextRun(run.text[:position], run.marks))
        if position < len(run.text):
            right.append(TextRun(run.text[position:], run.marks))
        cut -= position
    return tuple(left), tuple(right)


def _split_block(block: ContentBlock) -> Iterator[tuple[ContentBlock, bool]]:
    """Split oversized blocks while preserving order and marking layout fallbacks.

    Args:
        block: A converted block whose ID is assigned after splitting.

    Yields:
        Bounded blocks paired with whether their list or alternate-text layout
        was simplified. Ordered lists continue numbering between complete items.

    Raises:
        ContentError: If the configured limit cannot hold a minimal block.
    """
    if len(_BLOCK.dump_json(block)) <= _SECTION_BYTES // 2:
        yield block, False
    elif isinstance(block, TextBlock):
        for runs in _split_runs(block.runs):
            yield from _split_block(replace(block, runs=runs))
    elif isinstance(block, ListBlock) and len(block.items) > 1:
        cut = len(block.items) // 2
        yield from _split_block(replace(block, items=block.items[:cut]))
        start = (1 if block.start is None else block.start) + cut
        yield from _split_block(
            replace(
                block, items=block.items[cut:], start=start if block.ordered else None
            )
        )
    else:
        # a huge list item or image description becomes marked paragraph text
        if isinstance(block, ImageBlock):
            yield replace(block, alt=""), True
            runs = (TextRun(block.alt),)
        else:
            if not block.items:
                raise ContentError("invalid_epub")
            runs = block.items[0]
        for part, _ in _split_block(TextBlock(block.id, "paragraph", runs)):
            yield part, True


def _validate_asset(
    archive: ZipFile, identifier: str, resource: EpubResource
) -> EpubAsset | WarningCode:
    """Validate an image candidate once without retaining its complete bytes.

    Args:
        archive: The archive yielded by open_archive.
        identifier: The opaque ID assigned by the XHTML converter.
        resource: An existing, unencrypted manifest image candidate.

    Returns:
        A validated image location or a controlled placeholder reason.

    Raises:
        ContentError: If the archive is inconsistent.
        OSError: If source bytes cannot be read.
    """
    member = resource.member
    if member is None:
        return "missing_image"
    try:
        prefix = read_member(archive, member, IMAGE_BYTES, prefix_bytes=12)
        mime = image_mime(prefix)
    except ContentError as error:
        if error.code == "invalid_image":
            return "invalid_image"
        if error.code == "media_limit_exceeded":
            return "image_limit_exceeded"
        raise
    return EpubAsset(
        id=identifier,
        member=member.filename,
        mime_type=mime,
        size=member.file_size,
        crc=member.CRC,
    )


def _write_chapter(
    output: BinaryIO,
    chapters: list[EpubChapter],
    path: str,
    title: str | None,
    part: int,
    blocks: list[bytes],
    warnings: list[bytes],
):
    """Write one bounded section and append its checksum and byte range.

    Args:
        output: The new binary chapter-cache stream.
        chapters: The book's chapter index being assembled.
        path: The normalized spine member path used for stable section IDs.
        title: The optional navigation or document title.
        part: The one-based continuation within this spine document.
        blocks: Already serialized blocks in reading order.
        warnings: Already serialized warning entries for those blocks.

    Raises:
        ContentError: If the section or chapter count exceeds its limit.
        OSError: If the cache cannot be written.
    """
    data = (
        b'{"blocks":['
        + b",".join(blocks)
        + b'],"warnings":['
        + b",".join(warnings)
        + b"]}\n"
    )
    if len(data) > _SECTION_BYTES or len(chapters) >= _MAX_CHAPTERS:
        raise ContentError("media_limit_exceeded")
    start = output.tell()
    output.write(data)
    chapters.append(
        EpubChapter(
            id=hashlib.sha256(f"epub-chapter:{path}:{part}".encode()).hexdigest()[:32],
            title=title,
            part=part,
            start=start,
            end=output.tell(),
            digest=hashlib.sha256(data).hexdigest(),
        )
    )


def build_epub_index(source_path: Path, cache_dir: Path) -> EpubIndex:
    """Build a complete EPUB cache in a fresh caller-owned staging directory.

    Run in a worker after validating library access and the full source boundary.
    The caller publishes the result under its library lock. No source files are
    modified or extracted; a failed build removes only its own new directory.

    Args:
        source_path: The EPUB source within the validated library boundary.
        cache_dir: A nonexistent cache directory whose parent already exists.

    Returns:
        The index saved beside the serialized chapter body.

    Raises:
        ContentError: If the source is invalid, empty, unsupported, unstable
            or over limits.
        OSError: If source or cache access fails, or the cache already exists.
    """
    cache_dir.mkdir()
    try:
        chapters: list[EpubChapter] = []
        assets: dict[str, EpubAsset | WarningCode] = {}
        has_content = False
        with (
            open_archive(source_path) as (archive, snapshot),
            (cache_dir / "content.jsonl").open("xb") as output,
        ):
            package = load_epub_package(archive)
            titles = read_epub_titles(archive, package)
            for resource in package.spine:
                document = read_xhtml_document(archive, package, resource.id)
                if resource.path is None:
                    raise ContentError("invalid_epub")
                title = titles.get(resource.path, document.title)
                reasons: dict[str, list[WarningCode]] = {}
                for warning in document.warnings:
                    reasons.setdefault(warning.block_id, []).append(warning.code)
                blocks: list[bytes] = []
                warnings: list[bytes] = []
                size, part = 32, 1
                for block in document.blocks:
                    codes = reasons.get(block.id, []).copy()
                    if isinstance(block, ImageBlock) and block.asset_id is not None:
                        if block.asset_id not in assets:
                            assets[block.asset_id] = _validate_asset(
                                archive, block.asset_id, document.assets[block.asset_id]
                            )
                        asset = assets[block.asset_id]
                        if not isinstance(asset, EpubAsset):
                            codes.append(asset)
                            block = replace(block, asset_id=None)
                    if isinstance(block, ImageBlock):
                        has_content |= block.asset_id is not None
                    elif isinstance(block, TextBlock):
                        has_content |= any(run.text.strip() for run in block.runs)
                    else:
                        has_content |= any(
                            run.text.strip() for item in block.items for run in item
                        )
                    for number, (fragment, simplified) in enumerate(
                        _split_block(block)
                    ):
                        identifier = (
                            block.id
                            if not number
                            else hashlib.sha256(
                                f"{block.id}:part:{number}".encode()
                            ).hexdigest()[:32]
                        )
                        fragment = replace(fragment, id=identifier)
                        block_data = _BLOCK.dump_json(fragment)
                        fragment_codes = codes.copy()
                        if simplified:
                            fragment_codes.append("simplified_layout")
                        warning_data = _WARNINGS.dump_json(
                            tuple(
                                ContentWarning(code, identifier)
                                for code in dict.fromkeys(fragment_codes)
                            )
                        )[1:-1]
                        added = len(block_data) + len(warning_data) + 2
                        if blocks and size + added > _SECTION_BYTES:
                            _write_chapter(
                                output,
                                chapters,
                                resource.path,
                                title,
                                part,
                                blocks,
                                warnings,
                            )
                            blocks, warnings, size, part = [], [], 32, part + 1
                        blocks.append(block_data)
                        if warning_data:
                            warnings.append(warning_data)
                        size += added
                if blocks:
                    _write_chapter(
                        output, chapters, resource.path, title, part, blocks, warnings
                    )
            content_size = output.tell()
            if not has_content:
                raise ContentError("empty_content")
        index = EpubIndex(
            index_version=secrets.token_hex(32),
            source_snapshot=snapshot,
            content_size=content_size,
            chapters=tuple(chapters),
            assets=tuple(
                asset for asset in assets.values() if isinstance(asset, EpubAsset)
            ),
        )
        data = index.model_dump_json().encode()
        if len(data) > INDEX_BYTES:
            raise ContentError("media_limit_exceeded")
        (cache_dir / "index.json").write_bytes(data)
        return index
    except BaseException:
        shutil.rmtree(cache_dir)
        raise


def read_epub_chapter(
    cache_dir: Path, index: EpubIndex, chapter_id: str
) -> EpubContent:
    """Read and validate one cached section without opening the source EPUB.

    Args:
        cache_dir: The completed internal directory selected by the caller.
        index: The validated index loaded from that directory.
        chapter_id: An exact chapter ID from the current index.

    Returns:
        Safe blocks and warnings with opaque image references.

    Raises:
        ContentError: If the chapter is unknown or its cache is missing or corrupt.
    """
    chapter = next((entry for entry in index.chapters if entry.id == chapter_id), None)
    if chapter is None:
        raise ContentError("not_found")
    try:
        with (cache_dir / "content.jsonl").open("rb") as file:
            file.seek(chapter.start)
            data = file.read(chapter.end - chapter.start)
        if hashlib.sha256(data).hexdigest() != chapter.digest:
            raise ContentError("content_not_ready")
        content = EpubContent.model_validate_json(data)
        assets = {asset.id for asset in index.assets}
        if any(
            isinstance(block, ImageBlock)
            and block.asset_id is not None
            and block.asset_id not in assets
            for block in content.blocks
        ):
            raise ContentError("content_not_ready")
        return content
    except (OSError, ValidationError) as error:
        raise ContentError("content_not_ready") from error


def read_epub_resource(
    source_path: Path, index: EpubIndex, resource_id: str
) -> tuple[bytes, str]:
    """Read one validated image by membership in the current EPUB index.

    Run in a worker after checking library access, source boundaries and version.

    Args:
        source_path: The source EPUB currently owned by the authorized item.
        index: The validated index of that source and content version.
        resource_id: An exact image ID from the current index.

    Returns:
        The selected bytes and their validated MIME type.

    Raises:
        ContentError: If the ID is unknown, the source changed or reading fails.
    """
    resource = next((asset for asset in index.assets if asset.id == resource_id), None)
    if resource is None:
        raise ContentError("not_found")
    try:
        with open_archive(source_path, index.source_snapshot) as (archive, _):
            try:
                member = archive.getinfo(resource.member)
            except KeyError as error:
                raise ContentError("content_changed") from error
            if (member.file_size, member.CRC) != (resource.size, resource.crc):
                raise ContentError("content_changed")
            data = read_member(archive, member, IMAGE_BYTES)
            if image_mime(data) != resource.mime_type:
                raise ContentError("content_changed")
        return data, resource.mime_type
    except FileNotFoundError as error:
        raise ContentError("content_changed") from error
    except OSError as error:
        raise ContentError("media_source_unavailable") from error
