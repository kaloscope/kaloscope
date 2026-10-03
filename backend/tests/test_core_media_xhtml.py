"""Unit tests for EPUB XHTML blocks and bounded local image candidates."""

import json
import shutil
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest
from lxml import etree

from app.core.media import xhtml
from app.core.media.archive import open_archive
from app.core.media.common import ContentError
from app.core.media.epub import load_epub_package
from app.core.media.xhtml import (
    ImageBlock,
    ListBlock,
    TextBlock,
    TextRun,
    XhtmlDocument,
    read_xhtml_document,
)

_CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
    '<rootfiles><rootfile full-path="Book/book.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
_PACKAGE = (
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
    '<item id="chapter" href="Text/chapter.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="extra" href="extra.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="picture" href="Images/%E5%9B%BE%201.png" media-type="image/png"/>'
    '<item id="missing" href="Images/missing.png" media-type="image/png"/>'
    '<item id="vector" href="Images/vector.svg" media-type="image/svg+xml"/>'
    '</manifest><spine><itemref idref="chapter"/></spine></package>'
)
_IMAGE = "../Images/%E5%9B%BE%201.png"


def _source(
    tmp_path: Path,
    body: str = "<p>Text</p>",
    *,
    head: str = "",
    document: bytes | None = None,
    encryption: bytes | None = None,
) -> Path:
    """Write an isolated EPUB with one spine member and declared image candidates.

    Args:
        tmp_path: The isolated source directory.
        body: XHTML body markup; defaults to a simple paragraph.
        head: Optional head markup; empty by default.
        document: Full XHTML bytes, or None to build them from head and body.
        encryption: Optional encryption declarations; absent by default.

    Returns:
        A real EPUB path without extracting any members.
    """
    if document is None:
        document = (
            '<html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head>{head}</head><body>{body}</body></html>"
        ).encode()
    entries = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": _CONTAINER.encode(),
        "Book/book.opf": _PACKAGE.encode(),
        "Book/Text/chapter.xhtml": document,
        "Book/extra.xhtml": b"not a spine document",
        "Book/Images/图 1.png": b"image bytes are validated by the later cache builder",
        "Book/Images/vector.svg": b"vector bytes must not be read",
    }
    if encryption is not None:
        entries["META-INF/encryption.xml"] = encryption
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def _read(path: Path, resource_id: str = "chapter") -> XhtmlDocument:
    """Read one fixture document inside the stable archive context.

    Args:
        path: The isolated EPUB path.
        resource_id: A manifest ID; defaults to the fixture's spine document.

    Returns:
        Converted content after source stability validation.
    """
    with open_archive(path) as (archive, _):
        package = load_epub_package(archive)
        return read_xhtml_document(archive, package, resource_id)


def _text(document: XhtmlDocument) -> str:
    """Join visible text for assertions without including image candidates.

    Args:
        document: The converted fixture.

    Returns:
        Text in reading order, with block and list-item boundaries retained.
    """
    parts = []
    for block in document.blocks:
        if isinstance(block, TextBlock):
            parts.append("".join(run.text for run in block.runs))
        elif isinstance(block, ListBlock):
            parts.extend("".join(run.text for run in item) for item in block.items)
        else:
            parts.append(f"[{block.alt}]")
    return "|".join(parts)


def test_document_blocks(tmp_path):
    path = _source(
        tmp_path,
        "<h2>Chapter One</h2><p>Start <strong>bold <i>both</i></strong> end.</p>"
        "<blockquote><p>A quote.</p></blockquote><figure>"
        f'<p>Before<img src="{_IMAGE}" alt="插图"/>After</p>'
        "<figcaption>A caption.</figcaption></figure>",
        head="<title>Book title</title>",
    )
    before = path.read_bytes(), path.stat().st_mtime_ns
    document = _read(path)
    assert document.title == "Chapter One"
    assert [block.type for block in document.blocks] == [
        "heading",
        "paragraph",
        "quote",
        "paragraph",
        "image",
        "paragraph",
        "paragraph",
    ]
    heading, paragraph = document.blocks[:2]
    assert isinstance(heading, TextBlock) and heading.level == 2
    assert isinstance(paragraph, TextBlock)
    assert paragraph.runs == (
        TextRun("Start "),
        TextRun("bold ", ("strong",)),
        TextRun("both", ("strong", "em")),
        TextRun(" end."),
    )
    assert (
        _text(document)
        == "Chapter One|Start bold both end.|A quote.|Before|[插图]|After|A caption."
    )
    image = document.blocks[4]
    assert isinstance(image, ImageBlock) and image.asset_id is not None
    assert document.assets[image.asset_id].path == "Book/Images/图 1.png"
    assert not document.warnings
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert list(tmp_path.iterdir()) == [path]


def test_whitespace(tmp_path):
    document = _read(
        _source(
            tmp_path,
            "<p>  A\n <b> B <i>C</i></b>   D<br/> E&#160;F &amp; G </p>"
            "<pre>  first\n    second\n</pre>",
        )
    )
    assert _text(document) == "A B C D\nE\u00a0F & G|  first\n    second\n"


def test_lists(tmp_path):
    document = _read(
        _source(
            tmp_path,
            '<ol start="3"><li>One <em>item</em></li>'
            "<li><p>Two</p><p>continued</p></li></ol>"
            "<ul><li>Three</li><li>Four</li></ul>",
        )
    )
    ordered, unordered = document.blocks
    assert isinstance(ordered, ListBlock) and ordered.ordered and ordered.start == 3
    assert ordered.items == (
        (TextRun("One "), TextRun("item", ("em",))),
        (TextRun("Two\ncontinued"),),
    )
    assert isinstance(unordered, ListBlock) and not unordered.ordered
    assert unordered.start is None
    assert not document.warnings


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "<ul><li>Before<ul><li>Nested</li></ul>After</li></ul>",
            "Before|Nested|After",
        ),
        (
            f'<ol><li>Before<img src="{_IMAGE}" alt="Art"/>After</li>'
            "<li>Next</li></ol>",
            "Before|[Art]|After|Next",
        ),
        ('<ol reversed="reversed"><li>One</li><li>Two</li></ol>', "One|Two"),
        ('<ol><li value="8">One</li><li>Two</li></ol>', "One|Two"),
        ("<ul>Before<li>Item</li>After</ul>", "Before|Item|After"),
        ("<table><tr><td>Left</td><td>Right</td></tr></table>", "Left|Right"),
    ],
)
def test_complex_layout(tmp_path, body, expected):
    document = _read(_source(tmp_path, body))
    assert _text(document) == expected
    assert any(warning.code == "simplified_layout" for warning in document.warnings)
    assert {warning.block_id for warning in document.warnings} <= {
        block.id for block in document.blocks
    }


def test_markup_discarded(tmp_path):
    document = _read(
        _source(
            tmp_path,
            '<section style="background:url(https://invalid.example/style)">'
            '<p onclick="evil()">Visible<script>evil()</script> tail'
            "<style>.hidden { color:red }</style>"
            '<a href="javascript:evil()"> link</a></p>'
            "<custom><p>Unknown container</p></custom></section>",
            head='<link rel="stylesheet" href="file:///secret"/>'
            "<title>Fallback title</title>",
        )
    )
    assert document.title == "Fallback title"
    assert _text(document) == "Visible tail link|Unknown container"
    payload = json.dumps([asdict(block) for block in document.blocks])
    for value in (
        "evil",
        "javascript",
        "invalid.example",
        "secret",
        "onclick",
        "style=",
    ):
        assert value not in payload


def test_unsupported_content(tmp_path):
    document = _read(
        _source(
            tmp_path,
            '<p>Before</p><video src="https://invalid.example/video">'
            "Fallback text</video>"
            '<iframe src="file:///secret"/><p>After</p>',
        )
    )
    assert _text(document) == "Before|Fallback text||After"
    assert [warning.code for warning in document.warnings] == [
        "unsupported_content"
    ] * 2
    assert {warning.block_id for warning in document.warnings} <= {
        block.id for block in document.blocks
    }


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("../Images/missing.png", "missing_image"),
        ("../Images/not-declared.png", "missing_image"),
        ("../Images/vector.svg", "unsupported_image"),
        ("https://invalid.example/image.png", "external_image"),
        ("//invalid.example/image.png", "external_image"),
        ("data:image/png;base64,AAAA", "external_image"),
        ("file:///secret", "external_image"),
        ("../../../outside.png", "invalid_image_reference"),
        ("%2fetc/secret", "invalid_image_reference"),
        ("../Images/%FF.png", "invalid_image_reference"),
        ("", "invalid_image_reference"),
    ],
)
def test_unavailable_image(tmp_path, reference, code):
    document = _read(
        _source(tmp_path, f'<p>Before<img src="{reference}" alt="Art"/>After</p>')
    )
    assert _text(document) == "Before|[Art]|After"
    block = document.blocks[1]
    assert isinstance(block, ImageBlock) and block.asset_id is None
    assert not document.assets
    assert [(warning.code, warning.block_id) for warning in document.warnings] == [
        (code, block.id)
    ]


def test_encrypted_image(tmp_path):
    encryption = (
        b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        b'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        b'<enc:CipherData><enc:CipherReference URI="Book/Images/%E5%9B%BE%201.png"/>'
        b"</enc:CipherData></enc:EncryptedData></encryption>"
    )
    document = _read(
        _source(tmp_path, f'<img src="{_IMAGE}" alt="Art"/>', encryption=encryption)
    )
    assert not document.assets
    assert document.warnings[0].code == "encrypted_image"


def test_svg_wrapper(tmp_path):
    document = _read(
        _source(
            tmp_path,
            '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
            f'<title>Cover</title><g><image xlink:href="{_IMAGE}"/></g></svg>',
        )
    )
    block = document.blocks[0]
    assert isinstance(block, ImageBlock) and block.asset_id is not None
    assert block.alt == "Cover"
    assert not document.warnings


def test_svg_vector(tmp_path):
    document = _read(
        _source(
            tmp_path,
            '<svg xmlns="http://www.w3.org/2000/svg"><title>Vector</title>'
            '<path d="M0 0"/><script>evil()</script><text>Label</text></svg>',
        )
    )
    block = document.blocks[0]
    assert isinstance(block, ImageBlock) and block.asset_id is None
    assert block.alt == "Vector Label"
    assert document.warnings[0].code == "unsupported_image"


def test_asset_identity(tmp_path):
    source = _source(tmp_path, f'<p><img src="{_IMAGE}"/><img src="{_IMAGE}"/></p>')
    document = _read(source)
    assert len(document.assets) == 1
    assert len({block.id for block in document.blocks}) == 2
    moved = source.rename(tmp_path / "renamed.epub")
    assert _read(moved).blocks == document.blocks


def test_selected_member(tmp_path, monkeypatch):
    path = _source(tmp_path, f'<img src="{_IMAGE}"/>')
    read = xhtml.read_member
    calls = []

    def track(archive, member, limit):
        """Record members read by the XHTML converter.

        Args:
            archive: The stable fixture archive.
            member: The selected member.
            limit: Its byte budget.

        Returns:
            The unchanged bounded member bytes.
        """
        calls.append(member.filename)
        return read(archive, member, limit)

    monkeypatch.setattr(xhtml, "read_member", track)
    document = _read(path)
    assert document.assets
    assert calls == ["Book/Text/chapter.xhtml"]
    for identifier in ("extra", "../Images/图 1.png", "missing"):
        with pytest.raises(ContentError, match="not_found"):
            _read(path, identifier)


@pytest.mark.parametrize(
    ("document", "code"),
    [
        (b"<html", "invalid_epub"),
        (b"<html><body>Missing namespace</body></html>", "invalid_epub"),
        (b'<html xmlns="http://www.w3.org/1999/xhtml"><head/></html>', "invalid_epub"),
        (
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body/><body/></html>',
            "invalid_epub",
        ),
        (
            b'<html xmlns="http://www.w3.org/1999/xhtml" xml:base="../"><body/></html>',
            "unsupported_media_format",
        ),
        (
            b'<html xmlns="http://www.w3.org/1999/xhtml">'
            b'<head><base href="../"/></head><body/></html>',
            "unsupported_media_format",
        ),
        (
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b'<include xmlns="http://www.w3.org/2001/XInclude" href="file:///secret"/>'
            b"</body></html>",
            "unsupported_media_format",
        ),
        (
            b'<!DOCTYPE html [<!ENTITY text "Secret">]>'
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>&text;</body></html>',
            "unsupported_media_format",
        ),
    ],
)
def test_xml_boundary(tmp_path, document, code):
    with pytest.raises(ContentError, match=code):
        _read(_source(tmp_path, document=document))


@pytest.mark.parametrize(
    ("declaration", "body", "code"),
    [
        (b"<!DOCTYPE html>", b"Visible", None),
        (
            b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
            b'"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">',
            b"Visible",
            None,
        ),
        (b'<!DOCTYPE html SYSTEM "file:///must-not-read.dtd">', b"Visible", None),
        (
            b'<!DOCTYPE html [<!ENTITY external SYSTEM "file:///must-not-read.txt">]>',
            b"&external;",
            "unsupported_media_format",
        ),
        (
            b'<!DOCTYPE html [<!ENTITY % external SYSTEM "file:///must-not-read.dtd">'
            b"%external;]>",
            b"Visible",
            "unsupported_media_format",
        ),
        (
            b'<!DOCTYPE html [<!ENTITY alternate "Injected">]>',
            b'<img alt="&alternate;"/>',
            "unsupported_media_format",
        ),
    ],
)
def test_external_access(tmp_path, monkeypatch, declaration, body, code):
    parser = xhtml.etree.XMLParser
    calls = []

    class DenyResolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            """Fail on any attempt to access external XML resources.

            Args:
                url: The requested external location.
                public_id: Its optional identifier.
                context: The parser context.

            Raises:
                AssertionError: For every attempted external read.
            """
            calls.append(url)
            raise AssertionError("unexpected external read")

    def guarded(**kwargs):
        """Install a resolver that traps filesystem and network access.

        Args:
            kwargs: Options passed unchanged to the XML parser.

        Returns:
            The parser with an external access trap.
        """
        result = parser(**kwargs)
        result.resolvers.add(DenyResolver())
        return result

    monkeypatch.setattr(xhtml.etree, "XMLParser", guarded)
    document = (
        declaration
        + b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
        + body
        + b"</p></body></html>"
    )
    path = _source(tmp_path, document=document)
    if code is None:
        assert _text(_read(path)) == "Visible"
    else:
        with pytest.raises(ContentError, match=code):
            _read(path)
    assert not calls


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_xml_encoding(tmp_path, encoding):
    source = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<p>章<!-- hidden -->节<?test hidden?>内容</p></body></html>"
    ).encode(encoding)
    assert _text(_read(_source(tmp_path, document=source))) == "章节内容"


@pytest.mark.parametrize("limit", ["DOCUMENT_BYTES", "_MAX_NODES", "_MAX_BLOCKS"])
def test_document_limits(tmp_path, monkeypatch, limit):
    path = _source(tmp_path, "<p>One</p><p>Two</p><p>Three</p>")
    monkeypatch.setattr(xhtml, limit, 2)
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        _read(path)


def test_empty_and_title(tmp_path):
    assert not _read(_source(tmp_path, "<script>hidden</script>")).blocks
    assert (
        _read(_source(tmp_path, "<p>Body</p>", head="<title>Title</title>")).title
        == "Title"
    )
    document = _read(_source(tmp_path, f"<h1>{'章' * 150}</h1>"))
    assert document.title == "章" * 120
    assert _text(document) == "章" * 150


@pytest.mark.parametrize("action", ["replace", "delete"])
def test_source_changed(tmp_path, monkeypatch, action):
    path = _source(tmp_path)
    replacement = tmp_path / "replacement.epub"
    shutil.copyfile(path, replacement)
    read = xhtml.read_member

    def changing(archive, member, limit):
        """Change the source after the selected member has been consumed.

        Args:
            archive: The open fixture archive.
            member: The selected member.
            limit: Its byte budget.

        Returns:
            Member bytes from the original archive handle.
        """
        data = read(archive, member, limit)
        if action == "replace":
            replacement.replace(path)
        else:
            path.unlink()
        return data

    monkeypatch.setattr(xhtml, "read_member", changing)
    with pytest.raises(ContentError, match="content_changed"):
        _read(path)
