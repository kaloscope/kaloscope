"""Unit tests for local cover selection, ownership and bounded resource reads."""

import hashlib
import os
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from app.core.media import cover
from app.core.media.common import ContentError
from app.core.media.cover import read_cover
from app.core.media.handlers.reading import ReadingSource
from app.core.media.image import build_image_index
from app.core.media.reader import read_metadata
from app.models.media import MediaFormat

_PNG = b"\x89PNG\r\n\x1a\nprimary image"
_GIF = b"GIF89asecond image"


def _opf(href: str | None = None) -> bytes:
    """Build minimal OPF metadata with an optional declared cover.

    Args:
        href: The cover URL, or None to omit the declaration.

    Returns:
        OPF bytes without a readable spine or body.
    """
    manifest = (
        f'<manifest><item id="cover" properties="cover-image" '
        f'href={quoteattr(href)} media-type="image/jpeg"/></manifest>'
        if href is not None
        else ""
    )
    return (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f"<metadata/>{manifest}</package>"
    ).encode()


def _comic(page: int) -> bytes:
    """Build a ComicInfo cover marker.

    Args:
        page: The zero-based image number to mark as the front cover.

    Returns:
        UTF-8 ComicInfo bytes.
    """
    return (
        f'<ComicInfo><Pages><Page Image="{page}" Type="FrontCover"/>'
        "</Pages></ComicInfo>"
    ).encode()


def _source(tmp_path: Path, format: MediaFormat | None) -> ReadingSource:
    """Create a reading source in an isolated work container.

    Args:
        tmp_path: The temporary library directory.
        format: The body format, or None for a comic collection.

    Returns:
        A reading source whose parent directory exists.
    """
    directory = tmp_path / "Work"
    directory.mkdir()
    path = (
        directory if format in (None, MediaFormat.DIR) else directory / f"Book.{format}"
    )
    if format == MediaFormat.TXT:
        path.write_text("Body")
    return ReadingSource(path, format)


def _archive(source: ReadingSource, entries: dict[str, bytes]) -> None:
    """Write a test archive with EPUB container defaults when needed.

    Args:
        source: The target EPUB, CBZ or ZIP reading source.
        entries: Exact member names and bytes overriding any defaults.
    """
    files = {}
    if source.format == MediaFormat.EPUB:
        files = {
            "mimetype": b"application/epub+zip",
            "META-INF/container.xml": (
                b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                b'version="1.0"><rootfiles><rootfile full-path="Book/book.opf" '
                b'media-type="application/oebps-package+xml"/></rootfiles></container>'
            ),
            "Book/book.opf": _opf("images/front"),
            "Book/images/front": _GIF,
            "Book/body.xhtml": b"invalid body that cover reads must not parse",
        }
    files.update(entries)
    with zipfile.ZipFile(source.path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)


@pytest.mark.parametrize("format", [None, *MediaFormat])
def test_named_cover(tmp_path, format):
    source = _source(tmp_path, format)
    if format in (MediaFormat.EPUB, MediaFormat.CBZ, MediaFormat.ZIP):
        _archive(source, {"1.png": _GIF})
    elif format == MediaFormat.DIR:
        (source.path / "1.png").write_bytes(_GIF)
    files = {"PoStEr.png": _GIF, "folder.gif": _GIF, "CoVeR.jpg": _PNG}
    for name, data in files.items():
        (source.directory / name).write_bytes(data)
    result = read_cover(source, read_metadata(source))
    assert result is not None
    assert result.path == source.directory / "CoVeR.jpg"
    assert result.member is None
    assert (result.data, result.mime_type) == (_PNG, "image/png")
    assert {name: (source.directory / name).read_bytes() for name in files} == files


@pytest.mark.parametrize("bad", [b"bad image", _PNG * 10])
def test_named_fallback(tmp_path, monkeypatch, bad):
    source = _source(tmp_path, MediaFormat.TXT)
    (source.directory / "cover.jpg").write_bytes(bad)
    (source.directory / "folder.png").write_bytes(_PNG)
    (source.directory / "poster.gif").write_bytes(_GIF)
    monkeypatch.setattr(cover, "IMAGE_BYTES", len(_PNG))
    result = read_cover(source, read_metadata(source))
    assert result is not None and result.path.name == "folder.png"


def test_cover_tiebreak(tmp_path):
    source = _source(tmp_path, MediaFormat.TXT)
    (source.directory / "cover.png").write_bytes(_PNG)
    (source.directory / "cover.gif").write_bytes(_GIF)
    result = read_cover(source, read_metadata(source))
    assert result is not None and result.path.name == "cover.gif"


@pytest.mark.parametrize("format", [None, MediaFormat.TXT, MediaFormat.DIR])
def test_missing_cover(tmp_path, format):
    source = _source(tmp_path, format)
    (source.directory / ".cover.png").write_bytes(_PNG)
    (source.directory / "cover.png.part").write_bytes(_PNG)
    assert read_cover(source, read_metadata(source)) is None


@pytest.mark.parametrize("name", ["封面 1.data", "e\u0301.data"])
def test_opf_reference(tmp_path, name):
    source = _source(tmp_path, MediaFormat.TXT)
    path = source.directory / "images" / name
    path.parent.mkdir()
    path.write_bytes(_PNG)
    (source.directory / "metadata.opf").write_bytes(
        _opf(f"images/../images/{name.replace(' ', '%20')}#cover")
    )
    result = read_cover(source, read_metadata(source))
    assert result is not None and result.path == path and result.data == _PNG


@pytest.mark.parametrize(
    "href",
    [
        "../outside.png",
        "%2e%2e/outside.png",
        "images/../../outside.png",
        "missing.png",
        "https://example.com/cover.png",
        "front.png?download=1",
        ".hidden.png",
    ],
)
def test_unusable_reference(tmp_path, href):
    source = _source(tmp_path, MediaFormat.TXT)
    (tmp_path / "outside.png").write_bytes(_PNG)
    (source.directory / ".hidden.png").write_bytes(_PNG)
    (source.directory / "front.png").write_bytes(_PNG)
    (source.directory / "metadata.opf").write_bytes(_opf(href))
    assert read_cover(source, read_metadata(source)) is None


@pytest.mark.parametrize("linked", ["file", "directory"])
def test_reference_symlink(tmp_path, linked):
    source = _source(tmp_path, MediaFormat.TXT)
    outside = tmp_path / "Outside"
    outside.mkdir()
    (outside / "front.png").write_bytes(_PNG)
    if linked == "directory":
        (source.directory / "images").symlink_to(outside, target_is_directory=True)
        href = "images/front.png"
    else:
        (source.directory / "front.png").symlink_to(outside / "front.png")
        href = "front.png"
    (source.directory / "metadata.opf").write_bytes(_opf(href))
    with pytest.raises(ContentError, match="media_source_unavailable"):
        read_cover(source, read_metadata(source))


def test_epub_cover(tmp_path, monkeypatch):
    source = _source(tmp_path, MediaFormat.EPUB)
    _archive(source, {})
    metadata = read_metadata(source)
    reads = []
    read_member = cover.read_member

    def record(archive, member, limit, **kwargs):
        """Record cover reads while preserving the bounded archive reader.

        Args:
            archive: The stable source archive.
            member: The selected archive member.
            limit: The caller's byte bound.
            kwargs: Additional reader options.

        Returns:
            The original member bytes.
        """
        reads.append(member.filename)
        return read_member(archive, member, limit, **kwargs)

    monkeypatch.setattr(cover, "read_member", record)
    result = read_cover(source, metadata)
    assert result is not None
    assert result.path == source.path and result.member == "Book/images/front"
    assert (result.data, result.mime_type) == (_GIF, "image/gif")
    assert reads == ["Book/book.opf", "Book/images/front"]
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize(
    "external", ["front.png", "missing.png", "https://example.com/front.png"]
)
def test_epub_external_priority(tmp_path, external):
    source = _source(tmp_path, MediaFormat.EPUB)
    _archive(source, {})
    (source.directory / "front.png").write_bytes(_PNG)
    (source.directory / "metadata.opf").write_bytes(_opf(external))
    result = read_cover(source, read_metadata(source))
    assert result is not None
    assert result.data == (_PNG if external == "front.png" else _GIF)
    assert result.member == (None if external == "front.png" else "Book/images/front")


@pytest.mark.parametrize(
    "problem", ["encrypted", "missing", "svg", "oversized", "outside"]
)
def test_epub_unusable_cover(tmp_path, monkeypatch, problem):
    source = _source(tmp_path, MediaFormat.EPUB)
    entries = {}
    if problem == "encrypted":
        entries["META-INF/encryption.xml"] = (
            b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            b'xmlns:enc="http://www.w3.org/2001/04/xmlenc#"><enc:EncryptedData>'
            b'<enc:CipherData><enc:CipherReference URI="Book/images/front"/>'
            b"</enc:CipherData></enc:EncryptedData></encryption>"
        )
    elif problem in {"missing", "outside"}:
        entries["Book/book.opf"] = _opf(
            "missing" if problem == "missing" else "../../outside.png"
        )
    else:
        entries["Book/images/front"] = b"<svg/>" if problem == "svg" else _PNG * 10
        monkeypatch.setattr(cover, "IMAGE_BYTES", len(_PNG))
    _archive(source, entries)
    assert read_cover(source, read_metadata(source)) is None


@pytest.mark.parametrize("format", [MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP])
@pytest.mark.parametrize("page", [0, 1, 99])
def test_comic_marker(tmp_path, format, page):
    source = _source(tmp_path, format)
    files = {"10.png": _GIF, "2.png": _PNG}
    if format == MediaFormat.DIR:
        for name, data in files.items():
            (source.path / name).write_bytes(data)
    else:
        _archive(source, files)
    (source.directory / "ComicInfo.xml").write_bytes(_comic(page))
    result = read_cover(source, read_metadata(source))
    assert result is not None and result.data == (_GIF if page == 1 else _PNG)
    index = build_image_index(source, tmp_path / "cache")
    assert [resource.relative_path for resource in index.pages] == ["2.png", "10.png"]


@pytest.mark.parametrize("external_page", [None, 0, 99])
def test_comic_embedded_priority(tmp_path, external_page):
    source = _source(tmp_path, MediaFormat.CBZ)
    _archive(
        source,
        {"pages/1.png": _PNG, "pages/2.png": _GIF, "pages/ComicInfo.xml": _comic(1)},
    )
    if external_page is not None:
        (source.directory / "ComicInfo.xml").write_bytes(_comic(external_page))
    result = read_cover(source, read_metadata(source))
    assert result is not None
    assert result.member == ("pages/1.png" if external_page == 0 else "pages/2.png")


def test_comic_cover_numbering(tmp_path):
    source = _source(tmp_path, MediaFormat.CBZ)
    _archive(
        source,
        {
            "0/cover.png": _PNG,
            "pages/10.png": _PNG,
            "pages/2.png": _GIF,
            "ComicInfo.xml": _comic(1),
            "__MACOSX/0.png": b"ignored",
            ".hidden.png": b"ignored",
            "pages/1.txt": b"ignored",
        },
    )
    result = read_cover(source, read_metadata(source))
    assert result is not None and result.member == "pages/2.png"
    index = build_image_index(source, tmp_path / "cache")
    assert [resource.relative_path for resource in index.pages] == [
        "pages/2.png",
        "pages/10.png",
    ]


@pytest.mark.parametrize("named", [True, False])
def test_archive_fallback(tmp_path, named):
    source = _source(tmp_path, MediaFormat.ZIP)
    entries = {"page10.png": _GIF, "page2.png": _PNG, "cover.png": b"invalid"}
    if named:
        entries["nested/folder.png"] = _GIF
    _archive(source, entries)
    result = read_cover(source, read_metadata(source))
    assert result is not None
    assert result.member == ("nested/folder.png" if named else "page2.png")


def test_parent_cover_excluded(tmp_path):
    parent = tmp_path / "Work"
    chapter = parent / "Chapter"
    chapter.mkdir(parents=True)
    (parent / "cover.png").write_bytes(_PNG)
    (parent / "ComicInfo.xml").write_bytes(_comic(0))
    source = ReadingSource(chapter, MediaFormat.DIR, parent_path=parent)
    assert read_cover(source, read_metadata(source)) is None
    collection = ReadingSource(parent, None)
    (parent / "cover.png").unlink()
    (chapter / "cover.png").write_bytes(_GIF)
    assert read_cover(collection, read_metadata(collection)) is None


@pytest.mark.parametrize("format", [MediaFormat.EPUB, MediaFormat.CBZ])
def test_stale_metadata(tmp_path, format):
    source = _source(tmp_path, format)
    _archive(source, {"1.png": _PNG, "ComicInfo.xml": _comic(0)})
    metadata = read_metadata(source)
    _archive(
        source,
        {
            "1.png": _PNG,
            "ComicInfo.xml": _comic(1),
            "Book/book.opf": _opf("changed.png"),
        },
    )
    with pytest.raises(ContentError, match="content_changed"):
        read_cover(source, metadata)


def test_foreign_metadata(tmp_path):
    source = _source(tmp_path, MediaFormat.TXT)
    (source.directory / "metadata.opf").write_bytes(_opf("front.png"))
    metadata = read_metadata(source)
    assert metadata.external is not None
    foreign = replace(
        metadata, external=replace(metadata.external, path=tmp_path / "metadata.opf")
    )
    with pytest.raises(ContentError, match="media_source_unavailable"):
        read_cover(source, foreign)


@pytest.mark.parametrize("change", ["replace", "delete", "directory"])
def test_local_changed(tmp_path, monkeypatch, change):
    source = _source(tmp_path, MediaFormat.TXT)
    path = source.directory / "cover.png"
    path.write_bytes(_PNG)
    metadata = read_metadata(source)
    read_image = cover.read_image_file

    def mutate(path, **kwargs):
        """Change the container after the bounded image read.

        Args:
            path: The selected cover path.
            kwargs: The bounded reader's options.

        Returns:
            Bytes and stat information from the original read.
        """
        result = read_image(path, **kwargs)
        if change == "replace":
            replacement = path.with_suffix(".tmp")
            replacement.write_bytes(_GIF)
            replacement.replace(path)
        elif change == "delete":
            path.unlink()
        else:
            source.directory.rename(tmp_path / "Moved")
        return result

    monkeypatch.setattr(cover, "read_image_file", mutate)
    with pytest.raises(ContentError, match="content_changed"):
        read_cover(source, metadata)


def test_cover_io_error(tmp_path, monkeypatch):
    source = _source(tmp_path, MediaFormat.TXT)
    (source.directory / "cover.png").write_bytes(_PNG)
    (source.directory / "folder.gif").write_bytes(_GIF)
    metadata = read_metadata(source)

    def unavailable(*args, **kwargs):
        """Simulate an unreadable cover instead of a missing optional image.

        Args:
            args: Positional reader arguments.
            kwargs: Reader options.

        Raises:
            PermissionError: For every attempted image read.
        """
        raise PermissionError("unavailable")

    monkeypatch.setattr(cover, "read_image_file", unavailable)
    with pytest.raises(ContentError, match="media_source_unavailable"):
        read_cover(source, metadata)


def test_archive_crc(tmp_path):
    source = _source(tmp_path, MediaFormat.CBZ)
    _archive(source, {"1.png": _PNG})
    data = source.path.read_bytes()
    assert _PNG in data
    source.path.write_bytes(data.replace(_PNG, _PNG[:-1] + b"!"))
    with pytest.raises(ContentError, match="invalid_archive"):
        read_cover(source, read_metadata(source))


def test_current_cover(tmp_path):
    source = _source(tmp_path, MediaFormat.TXT)
    path = source.directory / "cover.png"
    path.write_bytes(_PNG)
    first = read_cover(source, read_metadata(source))
    before = path.stat()
    replacement = _GIF.ljust(len(_PNG), b" ")
    path.write_bytes(replacement)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    second = read_cover(source, read_metadata(source))
    assert first is not None and second is not None
    assert hashlib.sha256(first.data).digest() != hashlib.sha256(second.data).digest()
    assert second.data == replacement and second.mime_type == "image/gif"
