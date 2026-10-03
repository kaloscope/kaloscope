"""Unit tests for EPUB chapter caches and bounded raster resource reads."""

import hashlib
import io
import json
import os
import shutil
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from app.core.media import epub, epub_cache
from app.core.media.common import ContentError
from app.core.media.epub_cache import EpubContent, EpubIndex
from app.core.media.handlers.reading import ReadingSource
from app.core.media.text import (
    build_text_index,
    load_text_index,
    read_text_chapter,
    read_text_resource,
)
from app.core.media.xhtml import ImageBlock, ListBlock, TextBlock
from app.models.media import MediaFormat

_PNG = b"\x89PNG\r\n\x1a\nimage payload"
_IMAGE = "Book/images/picture"
_XHTML = '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fallback</title></head><body>{}</body></html>'
_NAV = (
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc">'
    '<a href="Text/1.xhtml">Second</a><a href="Text/0.xhtml#start"> First  chapter </a>'
    '<a href="Text/0.xhtml#later">Later label</a>'
    '<a href="https://example.test/file">External</a>'
    '<a href="../../../outside">Outside</a>'
    '</nav><nav epub:type="landmarks"><a href="Text/1.xhtml">Ignored</a>'
    "</nav></body></html>"
)
_NCX = (
    '<!DOCTYPE ncx SYSTEM "https://example.test/ncx.dtd">'
    '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
    '<navPoint><navLabel><text>Second</text></navLabel><content src="Text/1.xhtml"/>'
    "<navPoint><navLabel><text>First chapter</text></navLabel>"
    '<content src="Text/0.xhtml#start"/></navPoint></navPoint></navMap></ncx>'
)


def _source(
    tmp_path: Path,
    bodies: tuple[str, ...] = (
        '<p>Before<img src="../images/picture" alt="Art"/>After</p>',
    ),
    *,
    image: bytes = _PNG,
    navigation: str | None = None,
    ncx: bool = False,
) -> ReadingSource:
    """Write a real isolated EPUB with ordered bodies and an extensionless image.

    Args:
        tmp_path: The isolated test directory.
        bodies: Spine bodies; defaults to one mixed text and image paragraph.
        image: Image member bytes; defaults to a supported PNG signature.
        navigation: Optional navigation document; absent by default.
        ncx: Use EPUB 2 NCX when true; otherwise use EPUB 3 navigation.

    Returns:
        The EPUB reading source without starting application services.
    """
    manifest = "".join(
        f'<item id="c{i}" href="Text/{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(bodies))
    )
    manifest += '<item id="image" href="images/picture" media-type="image/png"/>'
    manifest += '<item id="unused" href="images/unused" media-type="image/png"/>'
    manifest += '<item id="missing" href="images/missing" media-type="image/png"/>'
    if navigation is not None:
        mime = "application/x-dtbncx+xml" if ncx else "application/xhtml+xml"
        properties = "" if ncx else ' properties="nav"'
        manifest += (
            f'<item id="nav" href="navigation" media-type="{mime}"{properties}/>'
        )
    version = "2.0" if ncx else "3.0"
    package = (
        f'<package xmlns="http://www.idpf.org/2007/opf" version="{version}">'
        f'<manifest>{manifest}</manifest><spine toc="nav">'
        + "".join(f'<itemref idref="c{i}"/>' for i in range(len(bodies)))
        + "</spine></package>"
    )
    entries = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": (
            b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            b'version="1.0"><rootfiles><rootfile full-path="Book/book.opf" '
            b'media-type="application/oebps-package+xml"/></rootfiles></container>'
        ),
        "Book/book.opf": package.encode(),
        _IMAGE: image,
        "Book/images/unused": b"not an image and must not be consumed",
        **{
            f"Book/Text/{i}.xhtml": _XHTML.format(body).encode()
            for i, body in enumerate(bodies)
        },
    }
    if navigation is not None:
        entries["Book/navigation"] = navigation.encode()
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return ReadingSource(path, MediaFormat.EPUB)


def _index(source: ReadingSource, cache: Path) -> EpubIndex:
    """Build EPUB through the public novel entry point.

    Args:
        source: The isolated EPUB fixture.
        cache: The new cache directory.

    Returns:
        The validated EPUB index.
    """
    index = build_text_index(source, cache)
    assert isinstance(index, EpubIndex)
    assert load_text_index(cache) == index
    return index


def _content(cache: Path, chapter_id: str) -> EpubContent:
    """Read one EPUB section through the public novel entry point.

    Args:
        cache: The completed EPUB cache directory.
        chapter_id: An exact indexed section ID.

    Returns:
        Validated blocks and warnings.
    """
    content = read_text_chapter(cache, chapter_id)
    assert isinstance(content, EpubContent)
    return content


def _text(contents: list[EpubContent]) -> str:
    """Reconstruct text across sections to detect omissions or duplication.

    Args:
        contents: Cached sections in reading order.

    Returns:
        All non-image text without inserted separators.
    """
    parts = []
    for content in contents:
        for block in content.blocks:
            if isinstance(block, TextBlock):
                parts.extend(run.text for run in block.runs)
            elif isinstance(block, ListBlock):
                parts.extend(run.text for item in block.items for run in item)
    return "".join(parts)


def test_epub_roundtrip(tmp_path, monkeypatch):
    source = _source(
        tmp_path,
        (
            "<h2>Start</h2><p>Before<strong>Bold</strong>"
            '<img src="../images/picture" alt="Art"/>After</p>',
            '<p>Last<img src="../images/picture"/></p>',
        ),
    )
    original = source.path.read_bytes()
    reads = []
    read_member = epub_cache.read_member

    def record(archive, member, limit, *, prefix_bytes=None):
        """Record image reads while retaining the real archive validation.

        Args:
            archive: The validated fixture archive.
            member: The member being consumed.
            limit: The uncompressed byte budget.
            prefix_bytes: Retained prefix length, or None for all bytes.

        Returns:
            The bounded member bytes.
        """
        reads.append((member.filename, prefix_bytes))
        return read_member(archive, member, limit, prefix_bytes=prefix_bytes)

    monkeypatch.setattr(epub_cache, "read_member", record)
    cache = tmp_path / "cache"
    index = _index(source, cache)
    assert reads == [(_IMAGE, 12)]
    assert len(index.assets) == 1
    assert [entry.title for entry in index.chapters] == ["Start", "Fallback"]
    contents = [_content(cache, entry.id) for entry in index.chapters]
    assert _text(contents) == "StartBeforeBoldAfterLast"
    first = contents[0].blocks
    assert [block.type for block in first] == [
        "heading",
        "paragraph",
        "image",
        "paragraph",
    ]
    assert isinstance(first[1], TextBlock) and first[1].runs[1].marks == ("strong",)
    assert all(not content.warnings for content in contents)
    assert set(path.name for path in cache.iterdir()) == {"index.json", "content.jsonl"}
    assert str(tmp_path) not in (cache / "index.json").read_text()
    assert "Book/images" not in (cache / "content.jsonl").read_text()
    assert read_text_resource(source.path, cache, index.assets[0].id) == (
        _PNG,
        "image/png",
    )
    assert reads[-1] == (_IMAGE, None)
    assert source.path.read_bytes() == original


@pytest.mark.parametrize(("navigation", "ncx"), [(_NAV, False), (_NCX, True)])
def test_navigation_titles(tmp_path, navigation, ncx):
    index = _index(
        _source(
            tmp_path, ("<p>First</p>", "<h1>Other</h1>"), navigation=navigation, ncx=ncx
        ),
        tmp_path / "cache",
    )
    assert [entry.title for entry in index.chapters] == ["First chapter", "Second"]
    assert (
        _text([_content(tmp_path / "cache", entry.id) for entry in index.chapters])
        == "FirstOther"
    )


@pytest.mark.parametrize(
    "navigation",
    [
        "<broken",
        _NAV.replace('epub:type="toc"', 'epub:type="landmarks"'),
        _NAV.replace("<body>", '<body xml:base="https://example.test/">'),
        '<!DOCTYPE html [<!ENTITY secret SYSTEM "file:///not-a-real-file">]>'
        + _NAV.replace("Second", "&secret;"),
    ],
)
def test_navigation_fallback(tmp_path, navigation):
    index = _index(
        _source(tmp_path, ("<h1>Heading</h1>",), navigation=navigation),
        tmp_path / "cache",
    )
    assert index.chapters[0].title == "Heading"


def test_navigation_external(tmp_path, monkeypatch):
    accesses = []
    parser = etree.XMLParser

    class RejectResolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            """Fail if any XML parser attempts external resolution.

            Args:
                url: The requested external location.
                public_id: The optional DTD public identifier.
                context: The active parser context.
            """
            accesses.append(url)
            raise AssertionError("external XML access")

    def guarded_parser(*args, **kwargs):
        """Install a resolver that detects external file and network reads.

        Args:
            args: Positional parser arguments.
            kwargs: Keyword parser arguments.

        Returns:
            The real XML parser with an access trap.
        """
        result = parser(*args, **kwargs)
        result.resolvers.add(RejectResolver())
        return result

    monkeypatch.setattr(etree, "XMLParser", guarded_parser)
    _index(_source(tmp_path, navigation=_NCX, ncx=True), tmp_path / "cache")
    assert accesses == []


def test_navigation_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(epub, "_XML_BYTES", 2048)
    source = _source(tmp_path, navigation=_NAV + " " * 2048)
    index = _index(source, tmp_path / "cache")
    assert index.chapters[0].title == "Fallback"


def test_epub_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(epub_cache, "_SECTION_BYTES", 4096)
    long_text = "中文字😀&lt;quote&gt;" * 1200
    items = "".join(f"<li><em>{i:03d}" + "x" * 45 + "</em></li>" for i in range(120))
    source = _source(
        tmp_path,
        (
            f"<blockquote><strong>{long_text}</strong></blockquote>",
            f'<ol start="0">{items}</ol>',
        ),
        navigation=_NAV,
    )
    cache = tmp_path / "cache"
    index = _index(source, cache)
    contents = [_content(cache, entry.id) for entry in index.chapters]
    assert len(contents) > 5
    assert all(entry.end - entry.start <= 4096 for entry in index.chapters)
    assert _text(contents) == "中文字😀<quote>" * 1200 + "".join(
        f"{i:03d}" + "x" * 45 for i in range(120)
    )
    blocks = [block for content in contents for block in content.blocks]
    assert len({block.id for block in blocks}) == len(blocks)
    count = 0
    for block in blocks:
        if isinstance(block, TextBlock):
            assert block.type == "quote" and all(
                run.marks == ("strong",) for run in block.runs
            )
        else:
            assert isinstance(block, ListBlock)
            assert block.start == count
            count += len(block.items)
    assert count == 120
    for title in ("First chapter", "Second"):
        parts = [entry.part for entry in index.chapters if entry.title == title]
        assert parts == list(range(1, len(parts) + 1))


def test_large_item_and_alt(tmp_path, monkeypatch):
    monkeypatch.setattr(epub_cache, "_SECTION_BYTES", 4096)
    value = "😀" * 4000
    source = _source(
        tmp_path,
        (f'<ul><li>{value}</li></ul><img src="../images/picture" alt="{value}"/>',),
    )
    cache = tmp_path / "cache"
    index = _index(source, cache)
    contents = [_content(cache, entry.id) for entry in index.chapters]
    assert _text(contents) == value * 2
    assert (
        sum(
            isinstance(block, ImageBlock)
            for content in contents
            for block in content.blocks
        )
        == 1
    )
    assert all(content.warnings for content in contents)
    assert {warning.code for content in contents for warning in content.warnings} == {
        "simplified_layout"
    }


@pytest.mark.parametrize(
    ("image", "limit", "reason"),
    [
        (b"not an image", 64 * 1024 * 1024, "invalid_image"),
        (_PNG, 4, "image_limit_exceeded"),
    ],
)
def test_image_placeholder(tmp_path, monkeypatch, image, limit, reason):
    monkeypatch.setattr(epub_cache, "IMAGE_BYTES", limit)
    cache = tmp_path / "cache"
    source = _source(tmp_path, image=image)
    index = _index(source, cache)
    content = _content(cache, index.chapters[0].id)
    block = next(block for block in content.blocks if isinstance(block, ImageBlock))
    assert block.asset_id is None and block.alt == "Art"
    assert [warning.code for warning in content.warnings] == [reason]
    assert not index.assets
    assert _text([content]) == "BeforeAfter"


def test_existing_warnings(tmp_path):
    source = _source(
        tmp_path,
        (
            '<p>Text<img src="../images/missing" alt="Missing"/><img src="https://example.test/remote"/></p>',
        ),
    )
    cache = tmp_path / "cache"
    index = _index(source, cache)
    assert {
        warning.code for warning in _content(cache, index.chapters[0].id).warnings
    } == {"missing_image", "external_image"}


def test_image_only(tmp_path):
    cache = tmp_path / "cache"
    index = _index(_source(tmp_path, ("", '<img src="../images/picture"/>')), cache)
    assert len(index.chapters) == len(index.assets) == 1
    assert len(_content(cache, index.chapters[0].id).blocks) == 1


@pytest.mark.parametrize(
    "body", ["", "<p>  </p>", '<img src="../images/missing" alt="Unavailable"/>']
)
def test_empty_book(tmp_path, body):
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="empty_content"):
        build_text_index(_source(tmp_path, (body,)), cache)
    assert not cache.exists()


def test_epub_versions(tmp_path):
    source = _source(tmp_path)
    first = _index(source, tmp_path / "first")
    moved = tmp_path / "moved.epub"
    source.path.rename(moved)
    second = _index(ReadingSource(moved, MediaFormat.EPUB), tmp_path / "second")
    assert first.index_version != second.index_version
    assert first.chapters == second.chapters
    assert first.assets == second.assets
    assert first.source_snapshot == second.source_snapshot
    with pytest.raises(FileExistsError):
        build_text_index(ReadingSource(moved, MediaFormat.EPUB), tmp_path / "first")
    assert load_text_index(tmp_path / "first") == first


def test_chapter_range(tmp_path, monkeypatch):
    source = _source(tmp_path, ("<p>First</p>", "<p>Second</p>"))
    cache = tmp_path / "cache"
    index = _index(source, cache)
    body_path = cache / "content.jsonl"
    data = body_path.read_bytes()
    target = index.chapters[1]
    data = b"!" * target.start + data[target.start :]
    reads = []
    open_path = Path.open

    class ChapterStream(io.BytesIO):
        def read(self, size=-1):
            """Record and bound the selected chapter's byte range.

            Args:
                size: The byte count; unbounded reads are rejected.

            Returns:
                The requested bytes from the isolated cache.
            """
            assert 0 <= size <= epub_cache._SECTION_BYTES
            reads.append((self.tell(), size))
            return super().read(size)

    def open_cache(path, *args, **kwargs):
        """Serve a guarded cache stream and reject source reads.

        Args:
            path: The file requested by the reader.
            args: Positional open arguments.
            kwargs: Keyword open arguments.

        Returns:
            The guarded chapter body or the real file stream.
        """
        assert path != source.path
        if path == body_path:
            return ChapterStream(data)
        return open_path(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_cache)
    assert _text([_content(cache, target.id)]) == "Second"
    assert reads == [(target.start, target.end - target.start)]


@pytest.mark.parametrize("operation", ["chapter", "image"])
def test_resource_membership(tmp_path, operation):
    cache = tmp_path / "cache"
    source = _source(tmp_path)
    _index(source, cache)
    for identifier in ("unknown", "../content.jsonl", _IMAGE, str(source.path)):
        with pytest.raises(ContentError, match="not_found"):
            if operation == "chapter":
                read_text_chapter(cache, identifier)
            else:
                read_text_resource(source.path, cache, identifier)


@pytest.mark.parametrize(
    "damage",
    [
        "index",
        "body",
        "size",
        "same_size",
        "schema",
        "range",
        "duplicate",
        "member",
        "oversize",
    ],
)
def test_cache_damage(tmp_path, damage):
    cache = tmp_path / "cache"
    index = _index(
        _source(
            tmp_path, ("<p>First</p>", '<p>Second<img src="../images/picture"/></p>')
        ),
        cache,
    )
    index_path, body_path = cache / "index.json", cache / "content.jsonl"
    data = json.loads(index_path.read_text())
    if damage == "index":
        index_path.unlink()
    elif damage == "body":
        body_path.unlink()
    elif damage == "size":
        body_path.write_bytes(b"short")
    elif damage == "same_size":
        body_path.write_bytes(body_path.read_bytes().replace(b"First", b"Wrong"))
    elif damage == "oversize":
        index_path.write_bytes(b" " * (epub_cache.INDEX_BYTES + 1))
    else:
        if damage == "schema":
            data["schema_version"] = 2
        elif damage == "range":
            data["chapters"][0]["end"] += 1
        elif damage == "duplicate":
            data["chapters"][1]["id"] = data["chapters"][0]["id"]
        else:
            data["assets"][0]["member"] = "../escape"
        index_path.write_text(json.dumps(data))
    with pytest.raises(ContentError, match="content_not_ready"):
        read_text_chapter(cache, index.chapters[0].id)


@pytest.mark.parametrize(
    "damage", ["unknown_field", "mark", "heading", "duplicate", "warning", "asset"]
)
def test_block_validation(tmp_path, damage):
    cache = tmp_path / "cache"
    index = _index(_source(tmp_path), cache)
    index_path, body_path = cache / "index.json", cache / "content.jsonl"
    data = json.loads(body_path.read_text())
    if damage == "unknown_field":
        data["blocks"][0]["onclick"] = "alert(1)"
    elif damage == "mark":
        data["blocks"][0]["runs"][0]["marks"] = ["script"]
    elif damage == "heading":
        data["blocks"][0]["type"] = "heading"
        data["blocks"][0]["level"] = 7
    elif damage == "duplicate":
        data["blocks"][1]["id"] = data["blocks"][0]["id"]
    elif damage == "warning":
        data["warnings"] = [{"code": "invalid_image", "block_id": "0" * 32}]
    else:
        data["blocks"][1]["asset_id"] = "0" * 32
    body = json.dumps(data).encode()
    body_path.write_bytes(body)
    metadata = json.loads(index_path.read_text())
    metadata["content_size"] = len(body)
    metadata["chapters"][0].update(
        end=len(body), digest=hashlib.sha256(body).hexdigest()
    )
    index_path.write_text(json.dumps(metadata))
    with pytest.raises(ContentError, match="content_not_ready"):
        read_text_chapter(cache, index.chapters[0].id)


@pytest.mark.parametrize("limit", ["chapters", "index"])
def test_cache_limits(tmp_path, monkeypatch, limit):
    source = _source(tmp_path, ("<p>First</p>", "<p>Second</p>"))
    monkeypatch.setattr(
        epub_cache, "_MAX_CHAPTERS" if limit == "chapters" else "INDEX_BYTES", 1
    )
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        build_text_index(source, cache)
    assert not cache.exists()


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_build_changed(tmp_path, monkeypatch, change):
    source = _source(tmp_path)
    read_document = epub_cache.read_xhtml_document

    def changed(*args, **kwargs):
        """Change the source after consuming a spine member.

        Args:
            args: Positional document reader arguments.
            kwargs: Keyword document reader arguments.

        Returns:
            The original converted document before the source changed.
        """
        document = read_document(*args, **kwargs)
        if change == "delete":
            source.path.unlink()
        else:
            replacement = tmp_path / "replacement.epub"
            shutil.copy2(source.path, replacement)
            replacement.replace(source.path)
        return document

    monkeypatch.setattr(epub_cache, "read_xhtml_document", changed)
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="content_changed"):
        build_text_index(source, cache)
    assert not cache.exists()


@pytest.mark.parametrize("change", ["delete", "mtime", "crc", "during_read"])
def test_resource_changed(tmp_path, monkeypatch, change):
    source = _source(tmp_path)
    cache = tmp_path / "cache"
    index = _index(source, cache)
    before = source.path.stat()
    if change == "delete":
        source.path.unlink()
    elif change == "mtime":
        os.utime(source.path, ns=(before.st_atime_ns, before.st_mtime_ns + 10_000))
    elif change == "crc":
        with zipfile.ZipFile(source.path) as archive:
            entries = {
                member.filename: archive.read(member) for member in archive.infolist()
            }
        entries[_IMAGE] = _PNG[:-1] + b"!"
        with zipfile.ZipFile(source.path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        assert source.path.stat().st_size == before.st_size
        os.utime(source.path, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        read_member = epub_cache.read_member

        def changed(*args, **kwargs):
            """Delete the source after the selected image is read.

            Args:
                args: Positional member reader arguments.
                kwargs: Keyword member reader arguments.

            Returns:
                The original image bytes.
            """
            data = read_member(*args, **kwargs)
            source.path.unlink()
            return data

        monkeypatch.setattr(epub_cache, "read_member", changed)
    with pytest.raises(ContentError, match="content_changed"):
        read_text_resource(source.path, cache, index.assets[0].id)


@pytest.mark.parametrize("member", [_IMAGE, "Book/navigation"])
def test_member_crc(tmp_path, member):
    source = _source(tmp_path, navigation=_NAV)
    with zipfile.ZipFile(source.path) as archive:
        info = archive.getinfo(member)
        offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    with source.path.open("r+b") as file:
        file.seek(offset)
        file.write(b"!")
    cache = tmp_path / "cache"
    with pytest.raises(ContentError, match="invalid_archive"):
        build_text_index(source, cache)
    assert not cache.exists()
