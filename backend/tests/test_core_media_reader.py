"""Unit tests for fresh metadata file reads, source ownership and error isolation."""

import hashlib
import os
import zipfile
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.media import reader
from app.core.media.epub import package as epub_package
from app.core.media.handlers.reading import ReadingSource
from app.core.media.reader import read_metadata
from app.models.media import MediaFormat


def _opf(fields: str) -> bytes:
    """Build OPF metadata without a readable manifest or spine.

    Args:
        fields: The metadata element contents.

    Returns:
        Bounded UTF-8 package bytes.
    """
    return (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<metadata>{fields}</metadata></package>"
    ).encode()


def _comic(fields: str) -> bytes:
    """Build ComicInfo bytes for one source.

    Args:
        fields: The ComicInfo element contents.

    Returns:
        UTF-8 metadata bytes.
    """
    return f"<ComicInfo>{fields}</ComicInfo>".encode()


def _source(tmp_path: Path, format: MediaFormat | None) -> ReadingSource:
    """Create an isolated work container and a source without indexing it.

    Args:
        tmp_path: The isolated library root.
        format: The body format, or None for a comic collection.

    Returns:
        A source with an existing container and no parent.
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
    """Write isolated ZIP members, adding EPUB descriptors when applicable.

    Args:
        source: The target EPUB, CBZ or ZIP source.
        entries: Members overriding the EPUB defaults or forming the comic archive.
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
            "Book/book.opf": _opf("<dc:title>Embedded</dc:title>"),
            "Book/body.xhtml": b"invalid body that metadata reads must never open",
        }
    files.update(entries)
    with zipfile.ZipFile(source.path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data, compress_type=zipfile.ZIP_STORED)


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.DIR, None])
def test_missing_metadata(tmp_path, format):
    source = _source(tmp_path, format)
    before = sorted(source.directory.iterdir())
    result = read_metadata(source)
    assert result.state == "missing" and not result.has_local_metadata
    assert (
        result.external
        is result.embedded
        is result.parent
        is result.cover_source
        is None
    )
    assert result.summary() == {
        "title": "Book" if format == MediaFormat.TXT else "Work",
        "year": None,
        "rating": None,
    }
    assert sorted(source.directory.iterdir()) == before


def test_opf_priority(tmp_path):
    source = _source(tmp_path, MediaFormat.TXT)
    paths = [
        source.directory / name for name in ("BOOK.OPF", "content.opf", "metadata.opf")
    ]
    for index, path in enumerate(paths):
        path.write_bytes(_opf(f"<dc:title>Title {index}</dc:title>"))
    for index, path in enumerate(paths):
        result = read_metadata(source)
        assert result.external is not None and result.external.path == path
        assert result.data.title == f"Title {index}"
        assert result.state == "ready" and result.has_local_metadata
        assert (
            result.external.signature == hashlib.sha256(path.read_bytes()).hexdigest()
        )
        path.unlink()
    assert read_metadata(source).state == "missing"


def test_opf_invalid_priority(tmp_path):
    source = _source(tmp_path, MediaFormat.TXT)
    source.path.with_suffix(".opf").write_bytes(b"broken")
    (source.directory / "metadata.opf").write_bytes(
        _opf("<dc:title>Lower priority</dc:title>")
    )
    result = read_metadata(source)
    assert result.state == "error" and not result.has_local_metadata
    assert result.data.title == "Book"
    assert result.external is not None and result.external.error == "invalid_metadata"


def test_external_case_collision(tmp_path, monkeypatch):
    source = _source(tmp_path, MediaFormat.TXT)
    entries = [
        SimpleNamespace(name=name, path=str(source.directory / name))
        for name in ("metadata.opf", "Metadata.OPF")
    ]
    monkeypatch.setattr(reader.os, "scandir", lambda path: nullcontext(entries))
    result = read_metadata(source)
    assert result.external is not None and result.external.error == "ambiguous_metadata"


def test_metadata_freshness(tmp_path):
    source = _source(tmp_path, MediaFormat.TXT)
    path = source.directory / "metadata.opf"
    path.write_bytes(_opf("<dc:title>Old</dc:title><dc:creator>Author</dc:creator>"))
    old = read_metadata(source)
    assert old.data.authors == ("Author",)
    stamp = path.stat()
    data = path.read_bytes().replace(b"Old", b"New").replace(b"Author", b"Writer")
    path.write_bytes(data)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    current = read_metadata(source)
    assert current.data.title == "New" and current.data.authors == ("Writer",)
    assert current.external is not None and old.external is not None
    assert current.external.signature != old.external.signature
    path.write_bytes(_opf("<dc:title>New</dc:title>"))
    assert read_metadata(source).data.authors == ()
    path.unlink()
    assert read_metadata(source).data.title == "Book"


def test_metadata_summary(tmp_path):
    source = _source(tmp_path, MediaFormat.DIR)
    path = source.directory / "ComicInfo.xml"
    data = _comic(
        "<Title>" + "文" * 300 + "</Title><Writer>Author</Writer>"
        "<CommunityRating>0</CommunityRating><Year>bad</Year>"
    )
    path.write_bytes(data)
    result = read_metadata(source)
    assert result.state == "ready" and result.has_local_metadata
    assert result.external is not None and result.external.parsed is not None
    assert result.external.parsed.invalid_fields == ("year",)
    assert result.summary() == {"title": "文" * 255, "year": None, "rating": Decimal(0)}
    assert result.data.title == "文" * 300 and path.read_bytes() == data
    path.write_bytes(_comic(""))
    assert read_metadata(source).has_local_metadata


@pytest.mark.parametrize(
    "kind",
    ["file_link", "directory_link", "missing_directory", "oversized", "permission"],
)
def test_external_errors(tmp_path, monkeypatch, kind):
    source = _source(tmp_path, MediaFormat.TXT)
    path = source.directory / "metadata.opf"
    expected = "media_source_unavailable"
    if kind == "file_link":
        target = tmp_path / "outside.opf"
        target.write_bytes(_opf("<dc:title>Outside</dc:title>"))
        path.symlink_to(target)
    elif kind == "directory_link":
        target = tmp_path / "outside"
        source.directory.rename(target)
        source.directory.symlink_to(target, target_is_directory=True)
    elif kind == "missing_directory":
        source.path.unlink()
        source.directory.rmdir()
    elif kind == "oversized":
        monkeypatch.setattr(reader, "METADATA_BYTES", 64)
        path.write_bytes(b" " * 65)
        expected = "media_limit_exceeded"
    else:
        path.write_bytes(_opf(""))

        def denied(*args):
            """Reject the selected source read.

            Args:
                args: Unused reader arguments.

            Raises:
                PermissionError: For the isolated unreadable-file scenario.
            """
            raise PermissionError

        monkeypatch.setattr(reader, "_read_local", denied)
    result = read_metadata(source)
    assert result.state == "error" and result.data.title == "Book"
    assert result.external is not None and result.external.error == expected


@pytest.mark.parametrize("change", ["replace", "delete", "continuous"])
def test_external_changes(tmp_path, monkeypatch, change):
    source = _source(tmp_path, MediaFormat.TXT)
    path = source.directory / "metadata.opf"
    path.write_bytes(_opf("<dc:title>Old</dc:title>"))
    parse = reader.parse_opf
    calls = []

    def changing(data):
        """Change the file during XML parsing to exercise bounded retries.

        Args:
            data: Bytes from the open metadata source.

        Returns:
            The real parsed fields before the source stability check.
        """
        calls.append(data)
        result = parse(data)
        if change == "delete":
            path.unlink()
        elif len(calls) == 1 or change == "continuous":
            replacement = path.with_suffix(".tmp")
            replacement.write_bytes(_opf("<dc:title>New</dc:title>"))
            replacement.replace(path)
        return result

    monkeypatch.setattr(reader, "parse_opf", changing)
    result = read_metadata(source)
    if change == "replace":
        assert result.data.title == "New" and result.state == "ready"
        assert len(calls) == 2
    elif change == "delete":
        assert result.state == "missing" and len(calls) == 1
    else:
        assert (
            result.external is not None and result.external.error == "content_changed"
        )
        assert result.data.title == "Book" and len(calls) == 2


def test_epub_metadata_only(tmp_path, monkeypatch):
    source = _source(tmp_path, MediaFormat.EPUB)
    data = _opf(
        "<dc:title>Embedded</dc:title><dc:creator>Author</dc:creator>"
        '<meta name="fixed-layout" content="true"/>'
    )
    _archive(source, {"Book/book.opf": data})
    external = source.directory / "metadata.opf"
    external.write_bytes(_opf("<dc:title>External</dc:title>"))
    before = source.path.read_bytes()
    read = epub_package.read_member
    members = []

    def track(archive, member, limit, **kwargs):
        """Record the members consumed by EPUB metadata discovery.

        Args:
            archive: The open archive.
            member: The selected metadata member.
            limit: The byte budget.
            kwargs: Optional read arguments forwarded unchanged.

        Returns:
            The member bytes from the real bounded reader.
        """
        members.append(member.filename)
        return read(archive, member, limit, **kwargs)

    monkeypatch.setattr(epub_package, "read_member", track)
    result = read_metadata(source)
    assert result.data.title == "External" and result.data.authors == ("Author",)
    assert result.state == "ready" and result.has_local_metadata
    assert result.embedded is not None and result.embedded.member == "Book/book.opf"
    assert result.embedded.signature == hashlib.sha256(data).hexdigest()
    assert members == ["mimetype", "META-INF/container.xml", "Book/book.opf"]
    assert source.path.read_bytes() == before
    assert sorted(path.name for path in source.directory.iterdir()) == [
        "Book.epub",
        "metadata.opf",
    ]


def test_cover_source(tmp_path):
    source = _source(tmp_path, MediaFormat.EPUB)
    data = _opf("<dc:title>Embedded</dc:title>").replace(
        b"</package>",
        b'<manifest><item id="cover" properties="cover-image" '
        b'href="images/cover.jpg"/></manifest></package>',
    )
    _archive(source, {"Book/book.opf": data})
    path = source.directory / "metadata.opf"
    path.write_bytes(_opf("<dc:title>External</dc:title>"))
    result = read_metadata(source)
    assert result.cover_source is result.embedded
    assert (
        result.data.cover is not None and result.data.cover.href == "images/cover.jpg"
    )
    path.write_bytes(data.replace(b"images/cover.jpg", b"outside.jpg"))
    result = read_metadata(source)
    assert result.cover_source is result.external
    assert result.data.cover is not None and result.data.cover.href == "outside.jpg"


@pytest.mark.parametrize("damage", ["container", "missing", "encrypted", "zip"])
def test_epub_errors(tmp_path, damage):
    source = _source(tmp_path, MediaFormat.EPUB)
    entries = {}
    expected = "invalid_epub"
    if damage == "container":
        entries["META-INF/container.xml"] = b"broken"
    elif damage == "missing":
        entries["META-INF/container.xml"] = (
            b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            b'version="1.0"><rootfiles><rootfile full-path="missing.opf" '
            b'media-type="application/oebps-package+xml"/></rootfiles></container>'
        )
    elif damage == "encrypted":
        entries["META-INF/encryption.xml"] = (
            b'<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            b'xmlns:e="http://www.w3.org/2001/04/xmlenc#"><e:EncryptedData>'
            b'<e:CipherData><e:CipherReference URI="Book/book.opf"/>'
            b"</e:CipherData></e:EncryptedData></encryption>"
        )
        expected = "unsupported_media_format"
    _archive(source, entries)
    if damage == "zip":
        source.path.write_bytes(b"broken zip")
        expected = "invalid_archive"
    (source.directory / "metadata.opf").write_bytes(
        _opf("<dc:title>External</dc:title>")
    )
    result = read_metadata(source)
    assert result.data.title == "External" and result.has_local_metadata
    assert result.state == "error"
    assert result.embedded is not None and result.embedded.error == expected


@pytest.mark.parametrize("format", [MediaFormat.CBZ, MediaFormat.ZIP])
@pytest.mark.parametrize("member", ["ComicInfo.xml", "Wrapper/ComicInfo.XML"])
def test_comic_embedded(tmp_path, format, member):
    source = _source(tmp_path, format)
    data = _comic(
        "<Title>Inside</Title><Writer>Author</Writer>"
        '<Pages><Page Image="0" Type="FrontCover"/></Pages>'
    )
    _archive(source, {member: data, "1.jpg": b"body bytes are not read"})
    before = source.path.read_bytes()
    result = read_metadata(source)
    assert result.external is None and result.state == "ready"
    assert result.has_local_metadata and result.data.title == "Inside"
    assert result.embedded is not None and result.embedded.member == member
    assert result.cover_source is result.embedded
    assert result.data.cover is not None and result.data.cover.page == 0
    assert source.path.read_bytes() == before
    assert list(source.directory.iterdir()) == [source.path]


@pytest.mark.parametrize(
    "scenario", ["root", "ambiguous", "case", "hidden", "absent", "crc", "oversized"]
)
def test_comic_selection(tmp_path, monkeypatch, scenario):
    source = _source(tmp_path, MediaFormat.CBZ)
    data = _comic("<Title>Metadata</Title>")
    entries = {"A/ComicInfo.xml": data, "B/ComicInfo.xml": data}
    expected = "ambiguous_metadata"
    if scenario == "root":
        entries["ComicInfo.xml"] = _comic("<Title>Root</Title>")
    elif scenario == "case":
        entries = {"ComicInfo.xml": data, "COMICINFO.XML": data}
    elif scenario == "hidden":
        entries = {"__MACOSX/ComicInfo.xml": data, ".hidden/ComicInfo.xml": data}
    elif scenario == "absent":
        entries = {"1.jpg": b"body"}
    elif scenario in ("crc", "oversized"):
        entries = {"ComicInfo.xml": data}
        expected = "invalid_archive" if scenario == "crc" else "media_limit_exceeded"
    _archive(source, entries)
    if scenario == "crc":
        source.path.write_bytes(
            source.path.read_bytes().replace(b"Metadata", b"Corruptd", 1)
        )
    elif scenario == "oversized":
        monkeypatch.setattr(reader, "METADATA_BYTES", len(data) - 1)
    result = read_metadata(source)
    if scenario == "root":
        assert result.data.title == "Root" and result.state == "ready"
    elif scenario in ("hidden", "absent"):
        assert result.embedded is None and result.state == "missing"
    else:
        assert result.embedded is not None and result.embedded.error == expected
        assert result.state == "error"


def test_external_error_fallback(tmp_path):
    source = _source(tmp_path, MediaFormat.CBZ)
    _archive(
        source,
        {"ComicInfo.xml": _comic("<Title>Inside</Title><Writer>Author</Writer>")},
    )
    (source.directory / "ComicInfo.xml").write_bytes(b"broken")
    result = read_metadata(source)
    assert result.data.title == "Inside" and result.data.authors == ("Author",)
    assert result.state == "error" and result.has_local_metadata
    assert result.external is not None and result.external.error == "invalid_metadata"


def test_embedded_not_needed(tmp_path):
    source = _source(tmp_path, MediaFormat.CBZ)
    fields = (
        "<Title>External</Title><Summary>Plot</Summary><Writer>A</Writer>"
        "<Penciller>B</Penciller><Publisher>C</Publisher><LanguageISO>en</LanguageISO>"
        "<GTIN>9780306406157</GTIN><Series>S</Series><Volume>1</Volume><Number>2</Number>"
        "<Year>2024</Year><Month>2</Month><Day>29</Day><Genre>G</Genre><Tags>T</Tags>"
        "<CommunityRating>0</CommunityRating><PageCount>0</PageCount>"
        "<BlackAndWhite>No</BlackAndWhite><Manga>Yes</Manga>"
        '<AlternateSeries>Original</AlternateSeries><Pages><Page Image="0" '
        'Type="FrontCover"/></Pages>'
    )
    (source.directory / "ComicInfo.xml").write_bytes(_comic(fields))
    result = read_metadata(source)
    assert result.state == "ready" and result.embedded is None
    assert result.has_local_metadata and result.data.title == "External"
    assert not source.path.exists()


@pytest.mark.parametrize("continuous", [False, True])
def test_embedded_changes(tmp_path, monkeypatch, continuous):
    source = _source(tmp_path, MediaFormat.CBZ)
    _archive(source, {"ComicInfo.xml": _comic("<Title>Old</Title>")})
    parse = reader.parse_comicinfo
    calls = []

    def changing(data):
        """Replace the archive after parsing, before the stable context exits.

        Args:
            data: The selected ComicInfo member bytes.

        Returns:
            Parsed metadata, subject to the archive's final stability check.
        """
        calls.append(data)
        result = parse(data)
        if continuous or len(calls) == 1:
            replacement = ReadingSource(source.path.with_suffix(".tmp"), source.format)
            _archive(replacement, {"ComicInfo.xml": _comic("<Title>New</Title>")})
            replacement.path.replace(source.path)
        return result

    monkeypatch.setattr(reader, "parse_comicinfo", changing)
    result = read_metadata(source)
    assert len(calls) == 2
    if continuous:
        assert result.embedded is not None
        assert result.embedded.error == "content_changed" and result.state == "error"
        assert result.data.title == "Book"
    else:
        assert result.data.title == "New" and result.state == "ready"


def test_parent_metadata(tmp_path):
    directory = tmp_path / "Work"
    chapter = directory / "Chapter"
    chapter.mkdir(parents=True)
    source = ReadingSource(chapter, MediaFormat.DIR, directory)
    parent = directory / "ComicInfo.xml"
    parent.write_bytes(
        _comic(
            "<Title>Parent</Title><Writer>Parent author</Writer>"
            "<Penciller>Artist</Penciller><Publisher>Press</Publisher>"
            "<Genre>Genre</Genre><Number>99</Number>"
        )
    )
    result = read_metadata(source)
    assert result.state == "ready" and not result.has_local_metadata
    assert result.data.title == "Chapter" and result.data.number is None
    assert result.data.authors == ("Parent author",) and result.data.illustrators == (
        "Artist",
    )
    assert result.data.publisher == "Press" and result.data.genres == ("Genre",)
    (chapter / "ComicInfo.xml").write_bytes(
        _comic("<Title>Chapter title</Title><Writer>Own author</Writer>")
    )
    result = read_metadata(source)
    assert result.data.authors == ("Own author",) and result.has_local_metadata
    parent.write_bytes(_comic("<Publisher>New press</Publisher>"))
    result = read_metadata(source)
    assert result.data.publisher == "New press" and result.data.illustrators == ()
    parent.write_bytes(b"broken")
    result = read_metadata(source)
    assert result.state == "error" and result.data.title == "Chapter title"
    assert result.parent is not None and result.parent.error == "invalid_metadata"


def test_parent_not_needed(tmp_path):
    directory = tmp_path / "Work"
    chapter = directory / "Chapter"
    chapter.mkdir(parents=True)
    source = ReadingSource(chapter, MediaFormat.DIR, directory)
    (directory / "ComicInfo.xml").write_bytes(b"broken")
    (chapter / "ComicInfo.xml").write_bytes(
        _comic(
            "<Writer>A</Writer><Penciller>B</Penciller><Publisher>C</Publisher><Genre>D</Genre>"
        )
    )
    result = read_metadata(source)
    assert result.parent is None and result.state == "ready"


@pytest.mark.parametrize(
    "kind",
    ["relative", "traversal", "novel_parent", "wrong_parent", "collection_parent"],
)
def test_source_ownership(tmp_path, kind):
    source = _source(
        tmp_path, MediaFormat.TXT if kind == "novel_parent" else MediaFormat.DIR
    )
    if kind == "relative":
        source = ReadingSource(Path("relative"), MediaFormat.DIR)
    elif kind == "traversal":
        source = ReadingSource(tmp_path / "Work" / ".." / "outside", MediaFormat.DIR)
    elif kind == "novel_parent":
        source = ReadingSource(source.path, source.format, source.directory.parent)
    elif kind == "wrong_parent":
        source = ReadingSource(source.path, source.format, tmp_path / "Another")
    else:
        source = ReadingSource(source.path, None, tmp_path)
    with pytest.raises(ValueError, match="ownership"):
        read_metadata(source)
