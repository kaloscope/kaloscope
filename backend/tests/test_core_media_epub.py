"""Unit tests for EPUB package structure and bounded local resources."""

import os
import struct
import zipfile
from pathlib import Path

import pytest
from lxml import etree

from app.core.media import epub
from app.core.media.archive import open_archive, read_member
from app.core.media.common import ContentError
from app.core.media.epub import EpubPackage, load_epub_package, resolve_epub_reference

_CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
    '<rootfiles><rootfile full-path="OEBPS/book.opf" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)
_ITEMS = (
    '<item id="one" href="Text/1.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="two" href="Text/2.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="cover" href="Images/cover.png" media-type="image/png" '
    'properties="cover-image"/>'
    '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
    'properties="nav"/>'
)
_SPINE = '<itemref idref="two"/><itemref idref="one" linear="no"/>'
_BODY = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Text</p></body></html>'


def _package(
    *,
    version: str = "3.0",
    items: str = _ITEMS,
    spine: str = _SPINE,
    metadata: str = "",
) -> bytes:
    """Build the package XML used by isolated fixtures.

    Args:
        version: The package version; defaults to EPUB 3.
        items: Manifest entries; defaults to two chapters, cover and navigation.
        spine: Ordered references; defaults to chapter two then auxiliary chapter one.
        metadata: Optional rendering or cover declarations; empty by default.

    Returns:
        Encoded package XML without requiring a complete descriptive metadata record.
    """
    return (
        f'<package xmlns="http://www.idpf.org/2007/opf" version="{version}">'
        f"<metadata>{metadata}</metadata><manifest>{items}</manifest>"
        f"<spine>{spine}</spine></package>"
    ).encode()


def _source(
    tmp_path: Path,
    files: dict[str, bytes | None] | None = None,
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    """Write a real EPUB with optional replacements and missing members.

    Args:
        tmp_path: The isolated test directory.
        files: Member overrides; None uses defaults and None values remove members.
        compression: The method for content and XML; defaults to Deflate.

    Returns:
        The source path with an uncompressed leading mimetype entry.
    """
    entries: dict[str, bytes | None] = {
        "mimetype": b"application/epub+zip",
        "META-INF/container.xml": _CONTAINER.encode(),
        "OEBPS/book.opf": _package(),
        "OEBPS/Text/1.xhtml": _BODY,
        "OEBPS/Text/2.xhtml": _BODY,
        "OEBPS/Images/cover.png": b"image bytes are not decoded by package inspection",
        "OEBPS/nav.xhtml": _BODY,
    }
    entries.update(files or {})
    path = tmp_path / "book.epub"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            if data is not None:
                archive.writestr(
                    name,
                    data,
                    compress_type=zipfile.ZIP_STORED
                    if name == "mimetype"
                    else compression,
                )
    return path


def _load(path: Path) -> EpubPackage:
    """Inspect a fixture inside the shared stable-archive context.

    Args:
        path: The isolated EPUB source.

    Returns:
        Its package structure after source stability checks have completed.
    """
    with open_archive(path) as (archive, _):
        return load_epub_package(archive)


@pytest.mark.parametrize("version", ["2.0", "3.0"])
@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_package_structure(tmp_path, version, compression):
    path = _source(
        tmp_path, {"OEBPS/book.opf": _package(version=version)}, compression=compression
    )
    original, before = path.read_bytes(), path.stat()
    package = _load(path)
    assert package.path == "OEBPS/book.opf"
    assert [item.id for item in package.spine] == ["two", "one"]
    assert package.spine[0] is package.resources["two"]
    assert package.navigation is package.resources["nav"]
    assert package.cover is package.resources["cover"]
    assert package.resources["one"].path == "OEBPS/Text/1.xhtml"
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert list(tmp_path.iterdir()) == [path]


def test_legacy_navigation(tmp_path):
    items = _ITEMS.replace(' properties="nav"', "").replace(
        ' properties="cover-image"', ""
    )
    items += '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
    package = _package(
        version="2.0", items=items, metadata='<meta name="cover" content="cover"/>'
    ).replace(b"<spine>", b'<spine toc="ncx">')
    result = _load(
        _source(tmp_path, {"OEBPS/book.opf": package, "OEBPS/toc.ncx": b"NCX"})
    )
    assert result.navigation is result.resources["ncx"]
    assert result.cover is result.resources["cover"]


def test_resource_locations(tmp_path):
    items = _ITEMS.replace("Text/1.xhtml", "Text/%E7%AB%A0%20%E4%B8%80.xhtml")
    items = items.replace("Text/2.xhtml", "Text/caf%C3%A9.xhtml")
    items += (
        '<item id="missing" href="Images/missing.png" media-type="image/png"/>'
        '<item id="remote" href="https://invalid.example/image.png" '
        'media-type="image/png"/>'
    )
    path = _source(
        tmp_path,
        {
            "OEBPS/book.opf": _package(items=items),
            "OEBPS/Text/1.xhtml": None,
            "OEBPS/Text/2.xhtml": None,
            "./OEBPS//Text/章 一.xhtml": _BODY,
            "OEBPS/Text/cafe\u0301.xhtml": _BODY,
        },
    )
    with open_archive(path) as (archive, _):
        result = load_epub_package(archive)
        resource = result.resources["one"]
        assert resource.path == "OEBPS/Text/章 一.xhtml"
        assert resource.member is not None
        assert resource.member.filename == "./OEBPS//Text/章 一.xhtml"
        assert read_member(archive, resource.member, epub.DOCUMENT_BYTES) == _BODY
    assert result.resources["missing"].path == "OEBPS/Images/missing.png"
    assert result.resources["missing"].member is None
    assert result.resources["remote"].path is None
    assert result.resources["remote"].member is None


@pytest.mark.parametrize(
    ("base", "reference", "expected"),
    [
        ("", "OEBPS/book.opf", "OEBPS/book.opf"),
        ("OEBPS/Text/1.xhtml", "../Images/1.png#view", "OEBPS/Images/1.png"),
        ("OEBPS/book.opf", "./Text//1.xhtml", "OEBPS/Text/1.xhtml"),
        ("OEBPS/Text/1.xhtml", "#paragraph", "OEBPS/Text/1.xhtml"),
        ("OEBPS/book.opf", "../cover.png", "cover.png"),
        ("OEBPS/book.opf", "Images/%252e%252e.png", "OEBPS/Images/%2e%2e.png"),
        ("OEBPS/book.opf", "Images/a%23b%3Fc.png", "OEBPS/Images/a#b?c.png"),
        ("OEBPS/book.opf", "https://invalid.example/a.png", None),
        ("OEBPS/book.opf", "//invalid.example/a.png", None),
        ("OEBPS/book.opf", "data:image/png;base64,AA==", None),
        ("OEBPS/book.opf", "file:///outside.png", None),
    ],
)
def test_reference(base, reference, expected):
    assert resolve_epub_reference(base, reference) == expected


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "../../outside.png",
        "%2e%2e/%2e%2e/outside.png",
        "/outside.png",
        "%2Foutside.png",
        "%43%3A/outside.png",
        r"Images\1.png",
        "Images/%5c1.png",
        "Images/%00.png",
        "Images/%ff.png",
        "Images/%.png",
        "Images/%xz.png",
        " Images/1.png",
        "Images/1.png\n",
        "Images/1.png?v=1",
        "../..",
    ],
)
def test_unsafe_reference(reference):
    with pytest.raises(ContentError, match="invalid_epub"):
        resolve_epub_reference("OEBPS/book.opf", reference)


@pytest.mark.parametrize(
    "member",
    ["mimetype", "META-INF/container.xml", "OEBPS/book.opf", "OEBPS/Text/1.xhtml"],
)
def test_required_member(tmp_path, member):
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {member: None}))


@pytest.mark.parametrize("mime", [b"application/not-epub", b"application/epub+zip\n"])
def test_mimetype(tmp_path, mime):
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {"mimetype": mime}))


@pytest.mark.parametrize("kind", ["compressed", "not_first"])
def test_mimetype_layout(tmp_path, kind):
    path = _source(tmp_path)
    with zipfile.ZipFile(path) as archive:
        entries = {info.filename: archive.read(info) for info in archive.infolist()}
    mime = entries.pop("mimetype")
    with zipfile.ZipFile(path, "w") as archive:
        if kind == "compressed":
            archive.writestr("mimetype", mime, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in entries.items():
            archive.writestr(name, data)
        if kind == "not_first":
            archive.writestr("mimetype", mime)
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(path)


@pytest.mark.parametrize(
    "container",
    [
        "<broken",
        _CONTAINER.replace('version="1.0"', 'version="2.0"'),
        _CONTAINER.replace("OEBPS/book.opf", "../book.opf"),
        _CONTAINER.replace("OEBPS/book.opf", "https://invalid.example/book.opf"),
        _CONTAINER.replace("application/oebps-package+xml", "text/plain"),
        _CONTAINER.replace("</container>", "<rootfiles/></container>"),
    ],
)
def test_container_invalid(tmp_path, container):
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {"META-INF/container.xml": container.encode()}))


def test_first_rendition(tmp_path):
    container = _CONTAINER.replace(
        "</rootfiles>",
        '<rootfile full-path="Other/book.opf" '
        'media-type="application/oebps-package+xml"/>'
        "</rootfiles>",
    )
    result = _load(_source(tmp_path, {"META-INF/container.xml": container.encode()}))
    assert result.path == "OEBPS/book.opf"


@pytest.mark.parametrize(
    "items",
    [
        _ITEMS + '<item id="one" href="x.png" media-type="image/png"/>',
        _ITEMS
        + '<item id="alias" href="Text/./1.xhtml" media-type="application/xhtml+xml"/>',
        _ITEMS + '<item href="x.png" media-type="image/png"/>',
        _ITEMS + '<item id="x" media-type="image/png"/>',
        _ITEMS + '<item id="x" href="x.png"/>',
        _ITEMS.replace('properties="nav"', 'properties="nav cover-image"'),
        _ITEMS.replace('properties="cover-image"', 'properties="cover-image nav"'),
    ],
)
def test_manifest_invalid(tmp_path, items):
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {"OEBPS/book.opf": _package(items=items)}))


@pytest.mark.parametrize(
    "spine",
    [
        "",
        '<itemref idref="absent"/>',
        '<itemref idref="one" linear="maybe"/>',
        '<itemref idref="one" linear="no"/>',
        '<itemref idref="one"/><itemref idref="one"/>',
    ],
)
def test_spine_invalid(tmp_path, spine):
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {"OEBPS/book.opf": _package(spine=spine)}))


@pytest.mark.parametrize("part", [b"manifest", b"spine"])
def test_duplicate_section(tmp_path, part):
    package = _package().replace(b"</package>", b"<" + part + b"/></package>")
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {"OEBPS/book.opf": package}))


@pytest.mark.parametrize(
    "package",
    [
        _package(version="4.0"),
        _package(
            metadata='<meta property="rendition:layout">\n pre-paginated\n</meta>'
        ),
        _package(metadata='<meta name="fixed-layout" content="true"/>'),
        _package(
            spine='<itemref idref="one" properties="rendition:layout-pre-paginated"/>'
        ),
        _package(
            items=_ITEMS.replace("Text/1.xhtml", "https://invalid.example/chapter")
        ),
        _package(
            items=_ITEMS.replace(
                'id="one" href="Text/1.xhtml" media-type="application/xhtml+xml"',
                'id="one" href="Text/1.xhtml" media-type="image/svg+xml"',
            )
        ),
    ],
)
def test_package_unsupported(tmp_path, package):
    with pytest.raises(ContentError, match="unsupported_media_format"):
        _load(_source(tmp_path, {"OEBPS/book.opf": package}))


@pytest.mark.parametrize("member", ["META-INF/container.xml", "OEBPS/book.opf"])
@pytest.mark.parametrize("kind", ["syntax", "namespace", "base", "xinclude"])
def test_xml_boundary(tmp_path, member, kind):
    data = _CONTAINER.encode() if member.startswith("META-INF") else _package()
    code = "invalid_epub"
    if kind == "syntax":
        data = data[:-5]
    elif kind == "namespace":
        data = data.replace(b"xmlns=", b"other=")
    elif kind == "base":
        data = data.replace(b"xmlns=", b'xml:base="https://invalid.example/" xmlns=', 1)
        code = "unsupported_media_format"
    else:
        index = data.rfind(b"</")
        data = (
            data[:index]
            + b'<include xmlns="http://www.w3.org/2001/XInclude" href="file:///outside"/>'
            + data[index:]
        )
        code = "unsupported_media_format"
    with pytest.raises(ContentError, match=code):
        _load(_source(tmp_path, {member: data}))


@pytest.mark.parametrize("member", ["META-INF/container.xml", "OEBPS/book.opf"])
@pytest.mark.parametrize(
    "declaration",
    [
        "",
        " []",
        ' [<!ENTITY label "Book">]',
        ' SYSTEM "file:///must-not-be-read.dtd"',
        ' PUBLIC "-//EXAMPLE//DTD Book//EN" "https://invalid.example/book.dtd"',
    ],
)
def test_xml_doctype(tmp_path, member, declaration):
    is_container = member.startswith("META-INF")
    name = "container" if is_container else "package"
    data = _CONTAINER.encode() if is_container else _package()
    data = f"<!DOCTYPE {name}{declaration}>".encode() + data
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {member: data}))


@pytest.mark.parametrize(
    "target",
    [
        "OEBPS/Text/1.xhtml",
        "OEBPS/book.opf",
        "OEBPS/font.otf",
        "OEBPS/Images/cover.png",
    ],
)
def test_encrypted_resources(tmp_path, target):
    encryption = (
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        '<enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>'
        f'<enc:CipherData><enc:CipherReference URI="{target}"/></enc:CipherData>'
        "</enc:EncryptedData></encryption>"
    ).encode()
    items = _ITEMS + '<item id="font" href="font.otf" media-type="font/otf"/>'
    path = _source(
        tmp_path,
        {
            "META-INF/encryption.xml": encryption,
            "OEBPS/book.opf": _package(items=items),
            "OEBPS/font.otf": b"obfuscated font",
        },
    )
    if target.endswith((".xhtml", ".opf")):
        with pytest.raises(ContentError, match="unsupported_media_format"):
            _load(path)
    else:
        result = _load(path)
        resource = result.resources["font" if target.endswith(".otf") else "cover"]
        assert resource.encrypted
        assert all(not item.encrypted for item in result.spine)


@pytest.mark.parametrize(
    "limit", ["_XML_BYTES", "_MAX_RESOURCES", "_MAX_SPINE_ITEMS", "DOCUMENT_BYTES"]
)
def test_package_limits(tmp_path, monkeypatch, limit):
    path = _source(tmp_path)
    monkeypatch.setattr(epub, limit, 1)
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        _load(path)


def test_package_reads(tmp_path, monkeypatch):
    path = _source(tmp_path)
    calls = []
    read = epub.read_member

    def capture(archive, member, limit):
        """Record which resources are read during package inspection.

        Args:
            archive: The validated archive.
            member: The selected member.
            limit: The uncompressed byte budget.

        Returns:
            The actual member bytes.
        """
        calls.append((member.filename, limit))
        return read(archive, member, limit)

    monkeypatch.setattr(epub, "read_member", capture)
    _load(path)
    assert calls == [
        ("mimetype", 20),
        ("META-INF/container.xml", epub._XML_BYTES),
        ("OEBPS/book.opf", epub._XML_BYTES),
    ]


@pytest.mark.parametrize("change", ["replace", "delete"])
def test_source_changed(tmp_path, monkeypatch, change):
    path = _source(tmp_path)
    read = epub.read_member

    def changed(archive, member, limit):
        """Change the source after reading the package XML.

        Args:
            archive: The validated archive.
            member: The selected member.
            limit: The uncompressed byte budget.

        Returns:
            The bytes read before replacing or deleting the source.
        """
        data = read(archive, member, limit)
        if member.filename == "OEBPS/book.opf":
            if change == "replace":
                before = path.stat()
                replacement = path.with_suffix(".tmp")
                replacement.write_bytes(path.read_bytes())
                os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
                replacement.replace(path)
            else:
                path.unlink()
        return data

    monkeypatch.setattr(epub, "read_member", changed)
    with pytest.raises(ContentError, match="content_changed"):
        _load(path)


def test_package_crc(tmp_path):
    path = _source(tmp_path, compression=zipfile.ZIP_STORED)
    with zipfile.ZipFile(path) as archive:
        offset = archive.getinfo("OEBPS/book.opf").header_offset
    data = bytearray(path.read_bytes())
    name_size, extra_size = struct.unpack_from("<HH", data, offset + 26)
    data[offset + 30 + name_size + extra_size + 10] ^= 1
    path.write_bytes(data)
    with pytest.raises(ContentError, match="invalid_archive"):
        _load(path)


def test_xml_external_access(tmp_path, monkeypatch):
    parser = epub.etree.XMLParser
    calls = []

    class DenyResolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            """Fail if any external resolution is attempted.

            Args:
                url: The requested external location.
                public_id: Its optional public identifier.
                context: The XML parser context.

            Raises:
                AssertionError: For every external resource request.
            """
            calls.append(url)
            raise AssertionError("unexpected external XML resource access")

    def guarded(**kwargs):
        """Add a resolver that detects external network or filesystem access.

        Args:
            kwargs: Options forwarded unchanged to the original XML parser.

        Returns:
            The parser with an external access trap.
        """
        result = parser(**kwargs)
        result.resolvers.add(DenyResolver())
        return result

    monkeypatch.setattr(epub.etree, "XMLParser", guarded)
    declaration = (
        b'<!DOCTYPE package SYSTEM "https://invalid.example/book.dtd" ['
        b'<!ENTITY external SYSTEM "file:///must-not-be-read.txt">]>'
    )
    data = declaration + _package(metadata="&external;")
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {"OEBPS/book.opf": data}))
    assert not calls


@pytest.mark.parametrize(
    "reference",
    [
        "",
        '<enc:CipherReference URI="https://invalid.example/cipher"/>',
        '<enc:CipherReference URI="../outside"/>',
    ],
)
def test_encryption_invalid(tmp_path, reference):
    encryption = (
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
        'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
        f"<enc:CipherData>{reference}</enc:CipherData></enc:EncryptedData></encryption>"
    ).encode()
    with pytest.raises(ContentError, match="invalid_epub"):
        _load(_source(tmp_path, {"META-INF/encryption.xml": encryption}))
