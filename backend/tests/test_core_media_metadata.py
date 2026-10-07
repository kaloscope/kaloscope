"""Unit tests for reading metadata parsing and field-level source precedence."""

from decimal import Decimal
from uuid import UUID

import pytest
from lxml import etree
from pydantic import ValidationError

from app.core.media import metadata
from app.core.media.common import ContentError
from app.core.media.metadata import (
    CoverReference,
    ReadingMetadata,
    merge_metadata,
    parse_comicinfo,
    parse_opf,
    render_metadata,
)


def _opf(fields: str, extra: str = "", *, version: str = "2.0") -> bytes:
    """Build an isolated OPF document without any source files.

    Args:
        fields: Metadata element contents.
        extra: Other package elements; empty by default.
        version: Package version, defaulting to OPF 2.

    Returns:
        UTF-8 package bytes with the required namespaces.
    """
    return (
        '<package xmlns="http://www.idpf.org/2007/opf" '
        'xmlns:opf="http://www.idpf.org/2007/opf" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        f'version="{version}"><metadata>{fields}</metadata>{extra}</package>'
    ).encode()


def _comic(fields: str) -> bytes:
    """Build an isolated ComicInfo document.

    Args:
        fields: ComicInfo element contents.

    Returns:
        UTF-8 metadata bytes.
    """
    return f"<ComicInfo>{fields}</ComicInfo>".encode()


def test_opf_fields():
    result = parse_opf(
        _opf(
            "<dc:title>  书名 &amp; Title  </dc:title>"
            '<dc:creator opf:role="aut">甲</dc:creator>'
            "<dc:creator>Doe, Jane</dc:creator>"
            '<dc:creator opf:role="ill">乙</dc:creator>'
            '<dc:contributor opf:role="aut">丙</dc:contributor>'
            '<dc:contributor opf:role="ill">丁</dc:contributor>'
            '<dc:contributor opf:role="trl">Translator</dc:contributor>'
            "<dc:description>第一行\n第二行 &lt;b&gt;plain markup&lt;/b&gt;"
            "</dc:description>"
            "<dc:publisher>出版社</dc:publisher><dc:language>zh-Hans</dc:language>"
            '<dc:identifier opf:scheme="UUID">not-an-isbn</dc:identifier>'
            '<dc:identifier opf:scheme="ISBN">978-0-306-40615-7</dc:identifier>'
            "<dc:date>2024-02-29T12:00:00Z</dc:date>"
            "<dc:subject>科幻</dc:subject>"
            "<dc:subject>Science &amp; Technology</dc:subject>"
            '<dc:subject>科幻</dc:subject><meta name="calibre:series" content="系列"/>'
            '<meta name="calibre:series_index" content="2.5"/>'
            '<meta name="calibre:rating" content="8.5"/>'
            '<meta name="cover" content="cover"/>',
            '<manifest><item id="cover" href="Images/封面 1.jpg" '
            'media-type="image/jpeg"/></manifest>',
        )
    )
    assert result.invalid_fields == ()
    assert result.data == ReadingMetadata(
        title="书名 & Title",
        authors=("甲", "Doe, Jane", "丙"),
        illustrators=("乙", "丁"),
        plot="第一行\n第二行 <b>plain markup</b>",
        publisher="出版社",
        language="zh-Hans",
        isbn="9780306406157",
        year=2024,
        month=2,
        day=29,
        genres=("科幻", "Science & Technology"),
        series="系列",
        number="2.5",
        rating=Decimal("8.5"),
        cover=CoverReference(href="Images/封面 1.jpg"),
    )


def test_opf_refinements():
    result = parse_opf(
        _opf(
            '<dc:title id="other">Subtitle</dc:title>'
            '<dc:title id="main">Main</dc:title>'
            '<meta property="title-type" refines="#main">main</meta>'
            '<dc:creator id="a">Author</dc:creator>'
            '<meta property="role" refines="#a" scheme="marc:relators">aut</meta>'
            '<dc:contributor id="b">Artist</dc:contributor>'
            '<meta property="role" refines="#b" scheme="marc:relators">ill</meta>'
            '<dc:identifier id="isbn">0306406152</dc:identifier>'
            '<meta property="identifier-type" refines="#isbn" '
            'scheme="onix:codelist5">02</meta>'
            '<meta property="belongs-to-collection" id="series">Series</meta>'
            '<meta property="collection-type" refines="#series">series</meta>'
            '<meta property="group-position" refines="#series">0</meta>'
            '<meta name="calibre:series" content="Fallback"/>'
            '<meta name="calibre:series_index" content="2"/>',
            '<manifest><item id="cover" properties="other cover-image" '
            'href="../Images/cover.png" media-type="image/png"/></manifest>',
            version="3.0",
        )
    )
    assert result.invalid_fields == ()
    assert result.data.title == "Main"
    assert result.data.authors == ("Author",)
    assert result.data.illustrators == ("Artist",)
    assert result.data.isbn == "0306406152"
    assert (result.data.series, result.data.number) == ("Series", "0")
    assert result.data.cover == CoverReference(href="../Images/cover.png")


def test_opf_empty_refinements():
    result = parse_opf(
        _opf(
            "<dc:title>Fallback title</dc:title>"
            '<dc:title id="main"> </dc:title>'
            '<meta property="title-type" refines="#main">main</meta>'
            '<meta property="belongs-to-collection" id="series"> </meta>'
            '<meta property="collection-type" refines="#series">series</meta>'
            '<meta name="calibre:series" content="Fallback series"/>',
            version="3.0",
        )
    )
    assert result.data.title == "Fallback title"
    assert result.data.series == "Fallback series"
    assert result.invalid_fields == ()


@pytest.mark.parametrize(
    ("identifier", "isbn"),
    [
        ("<dc:identifier>urn:isbn:0-8044-2957-X</dc:identifier>", "080442957X"),
        ('<dc:identifier opf:scheme="UUID">9780306406157</dc:identifier>', None),
        (
            '<dc:identifier id="other">9780306406157</dc:identifier>'
            '<meta property="identifier-type" refines="#other" '
            'scheme="other">15</meta>',
            None,
        ),
    ],
)
def test_opf_identifiers(identifier, isbn):
    assert parse_opf(_opf(identifier)).data.isbn == isbn


@pytest.mark.parametrize(
    ("value", "parts"),
    [
        ("2024", (2024, None, None)),
        ("2024-03", (2024, 3, None)),
        ("2024-03-01", (2024, 3, 1)),
        ("2024-02-30", (None, None, None)),
    ],
)
def test_opf_dates(value, parts):
    result = parse_opf(_opf(f"<dc:date>{value}</dc:date>"))
    assert (result.data.year, result.data.month, result.data.day) == parts
    assert result.invalid_fields == (("year",) if value == "2024-02-30" else ())


def test_opf_standalone():
    result = parse_opf(
        b'<o:metadata xmlns:o="http://www.idpf.org/2007/opf" '
        b'xmlns:d="http://purl.org/dc/elements/1.1/"><d:title>Title</d:title></o:metadata>'
    )
    assert result.data.title == "Title"


def test_comic_fields():
    result = parse_comicinfo(
        _comic(
            "<Title>Chapter</Title><Series>Series</Series><Number>12.5 番外</Number>"
            "<Volume>0</Volume><Summary>Line 1\nLine 2 &amp; more</Summary>"
            "<Writer>甲, 乙,甲, </Writer><Penciller>丙</Penciller>"
            "<Inker>Inker</Inker><Colorist>Colorist</Colorist>"
            "<CoverArtist>Cover artist</CoverArtist>"
            "<Publisher>Publisher</Publisher><LanguageISO>zh-Hant</LanguageISO>"
            "<Year>2024</Year><Month>2</Month><Day>29</Day>"
            "<Genre>Adventure, Fantasy</Genre><Tags>tag1, tag2</Tags>"
            "<CommunityRating>4.25</CommunityRating><PageCount>0</PageCount>"
            "<BlackAndWhite>No</BlackAndWhite><Manga>YesAndRightToLeft</Manga>"
            "<AlternateSeries>原系列名</AlternateSeries><GTIN>9780306406157</GTIN>"
            '<Pages><Page Image="0" Type="FrontCover"/>'
            '<Page Image="1" Type="Story"/></Pages>',
        )
    )
    assert result.invalid_fields == ()
    assert result.data == ReadingMetadata(
        title="Chapter",
        series="Series",
        originaltitle="原系列名",
        number="12.5 番外",
        volume="0",
        plot="Line 1\nLine 2 & more",
        authors=("甲", "乙"),
        illustrators=("丙",),
        publisher="Publisher",
        language="zh-Hant",
        year=2024,
        month=2,
        day=29,
        genres=("Adventure", "Fantasy"),
        tags=("tag1", "tag2"),
        rating=Decimal("8.50"),
        page_count=0,
        black_and_white=False,
        isbn="9780306406157",
        cover=CoverReference(page=0),
    )


def test_unknown_values():
    result = parse_comicinfo(
        _comic(
            "<Title> </Title><Writer> , </Writer><Volume>-1</Volume><Year>-1</Year>"
            "<Month>-1</Month><Day>-1</Day><BlackAndWhite>Unknown</BlackAndWhite>"
            "<AlternateSeries>Crossover</AlternateSeries><GTIN>0123456789012</GTIN>"
        )
    )
    assert result.data == ReadingMetadata()
    assert result.invalid_fields == ()
    assert parse_opf(_opf("")).data == ReadingMetadata()
    assert parse_comicinfo(b"<ComicInfo/>").data == ReadingMetadata()


@pytest.mark.parametrize(
    ("field", "xml"),
    [
        ("year", "<Year>1.5</Year>"),
        ("year", "<Year>10000</Year>"),
        ("month", "<Month>13</Month>"),
        ("day", "<Year>2023</Year><Month>2</Month><Day>29</Day>"),
        ("volume", "<Volume>番外</Volume>"),
        ("volume", "<Volume>1.5</Volume>"),
        ("volume", "<Volume>2147483648</Volume>"),
        ("rating", "<CommunityRating>NaN</CommunityRating>"),
        ("rating", "<CommunityRating>1e2</CommunityRating>"),
        ("rating", "<CommunityRating>6</CommunityRating>"),
        ("rating", "<CommunityRating>-0.5</CommunityRating>"),
        ("page_count", "<PageCount>-2</PageCount>"),
        ("black_and_white", "<BlackAndWhite>false</BlackAndWhite>"),
        ("isbn", "<GTIN>9780306406158</GTIN>"),
        ("cover", '<Pages><Page Image="-1" Type="FrontCover"/></Pages>'),
        (
            "cover",
            '<Pages><Page Image="0" Type="FrontCover"/>'
            '<Page Image="1" Type="FrontCover"/></Pages>',
        ),
    ],
)
def test_comic_invalid_fields(field, xml):
    result = parse_comicinfo(_comic("<Title>Usable</Title>" + xml))
    assert result.invalid_fields == (field,)
    assert getattr(result.data, field) is None
    assert result.data.title == "Usable"


def test_opf_invalid_fields():
    result = parse_opf(
        _opf(
            "<dc:title>Usable</dc:title>"
            '<dc:identifier opf:scheme="ISBN">bad</dc:identifier>'
            '<meta name="calibre:rating" content="Infinity"/>'
            '<meta name="calibre:series_index" content="not a number"/>'
            '<meta name="cover" content="missing"/>',
        )
    )
    assert set(result.invalid_fields) == {"isbn", "rating", "number", "cover"}
    assert result.data.title == "Usable"


@pytest.mark.parametrize(
    "href",
    [
        "file:///secret",
        "/absolute.png",
        "C:/cover.png",
        "javascript:alert(1)",
        "//remote/cover.png",
        "%zz",
        "https://user:password@example.test/image",
    ],
)
def test_cover_links(href):
    result = parse_opf(
        _opf(
            "",
            '<manifest><item properties="cover-image" href="' + href + '"/></manifest>',
        )
    )
    assert result.data.cover is None
    assert result.invalid_fields == ("cover",)


def test_metadata_merge():
    external = parse_comicinfo(
        _comic(
            "<Title>External</Title><Writer>Author A</Writer><Genre>Genre A</Genre>"
            "<CommunityRating>0</CommunityRating><BlackAndWhite>No</BlackAndWhite>"
            '<PageCount>0</PageCount><Pages><Page Image="0" Type="FrontCover"/></Pages>'
        )
    ).data
    embedded = ReadingMetadata(
        title="Embedded",
        authors=("Author B",),
        genres=("Genre B",),
        publisher="Embedded publisher",
        number="0",
        rating=Decimal(10),
        black_and_white=True,
        page_count=100,
        cover=CoverReference(page=5),
        tags=("embedded",),
    )
    parent = ReadingMetadata(
        title="Parent",
        authors=("Parent author",),
        illustrators=("Artist",),
        publisher="Parent publisher",
        genres=("Parent genre",),
        plot="Parent plot",
        isbn="9780306406157",
        series="Parent series",
        volume="2",
        number="3",
        year=2020,
        tags=("parent",),
        rating=Decimal(8),
        page_count=200,
        cover=CoverReference(page=7),
    )
    before = (external.model_dump(), embedded.model_dump(), parent.model_dump())
    merged = merge_metadata(external, embedded, parent=parent)
    assert merged.title == "External"
    assert merged.authors == ("Author A",) and merged.genres == ("Genre A",)
    assert (
        merged.illustrators == ("Artist",) and merged.publisher == "Embedded publisher"
    )
    assert (merged.rating, merged.page_count, merged.black_and_white) == (
        Decimal(0),
        0,
        False,
    )
    assert merged.cover == CoverReference(page=0)
    assert merged.number == "0" and merged.tags == ("embedded",)
    assert (
        merged.volume
        is merged.year
        is merged.plot
        is merged.isbn
        is merged.series
        is None
    )
    assert (external.model_dump(), embedded.model_dump(), parent.model_dump()) == before
    assert merge_metadata(None, None, parent=parent) == ReadingMetadata(
        authors=("Parent author",),
        illustrators=("Artist",),
        publisher="Parent publisher",
        genres=("Parent genre",),
    )
    assert merge_metadata(None, None) == ReadingMetadata()


def test_merge_removed_fields():
    old = parse_opf(
        _opf("<dc:title>Old</dc:title><dc:creator>Author</dc:creator>")
    ).data
    assert merge_metadata(old, None).authors == ("Author",)
    current = parse_opf(_opf("<dc:title>New</dc:title>")).data
    assert merge_metadata(current, None).authors == ()
    assert merge_metadata(None, None).title is None
    assert merge_metadata(current, old).title == "New"
    assert merge_metadata(current, old).authors == ("Author",)


def test_merge_partial_dates():
    external = ReadingMetadata(year=2025)
    embedded = ReadingMetadata(year=2024, month=2, day=29)
    assert merge_metadata(external, embedded) == external
    external = ReadingMetadata(month=2, day=29)
    merged = merge_metadata(external, ReadingMetadata(year=2025))
    assert (merged.year, merged.month, merged.day) == (2025, 2, None)
    merged = merge_metadata(ReadingMetadata(year=2024), embedded)
    assert (merged.year, merged.month, merged.day) == (2024, 2, 29)


@pytest.mark.parametrize("kind", ["opf", "comic"])
@pytest.mark.parametrize(
    "damage", ["broken", "doctype", "entity", "include", "base", "namespace"]
)
def test_xml_boundary(kind, damage):
    parse = parse_opf if kind == "opf" else parse_comicinfo
    data = (
        _opf("<dc:title>Title</dc:title>")
        if kind == "opf"
        else _comic("<Title>Title</Title>")
    )
    tag = b"package" if kind == "opf" else b"ComicInfo"
    if damage == "broken":
        data = data[:-3]
    elif damage == "doctype":
        data = (
            b"<!DOCTYPE " + tag + b' SYSTEM "https://example.test/metadata.dtd">' + data
        )
    elif damage == "entity":
        data = (
            b"<!DOCTYPE "
            + tag
            + b' [<!ENTITY secret SYSTEM "file:///secret">]>'
            + data.replace(b">Title<", b">&secret;<")
        )
    elif damage == "include":
        data = data.replace(
            b">Title<",
            b'><xi:include xmlns:xi="http://www.w3.org/2001/XInclude" href="file:///secret"/><',
        )
    elif damage == "base":
        data = data.replace(
            b"<" + tag, b"<" + tag + b' xml:base="https://example.test/"', 1
        )
    else:
        data = (
            data.replace(b"http://www.idpf.org/2007/opf", b"urn:wrong")
            if kind == "opf"
            else data.replace(b"<ComicInfo>", b'<ComicInfo xmlns="urn:wrong">')
        )
    with pytest.raises(ContentError, match="invalid_metadata"):
        parse(data)


def test_xml_external_access(monkeypatch):
    accesses = []
    parser = etree.XMLParser

    class RejectResolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            """Reject any external XML resource access.

            Args:
                url: The requested location.
                public_id: The optional public identifier.
                context: The active parser context.
            """
            accesses.append(url)
            raise AssertionError("external XML access")

    def guarded_parser(*args, **kwargs):
        """Install an external-access trap on real lxml parsers.

        Args:
            args: Positional parser arguments.
            kwargs: Keyword parser arguments.

        Returns:
            A real parser with the rejecting resolver.
        """
        result = parser(*args, **kwargs)
        result.resolvers.add(RejectResolver())
        return result

    monkeypatch.setattr(etree, "XMLParser", guarded_parser)
    for parse, data in (
        (parse_opf, b'<!DOCTYPE package SYSTEM "file:///secret">' + _opf("")),
        (
            parse_comicinfo,
            b'<!DOCTYPE ComicInfo SYSTEM "https://example.test/schema">' + _comic(""),
        ),
    ):
        with pytest.raises(ContentError, match="invalid_metadata"):
            parse(data)
    assert accesses == []


def test_xml_limits(monkeypatch):
    monkeypatch.setattr(metadata, "METADATA_BYTES", 64)
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        parse_comicinfo(_comic("<Title>" + "x" * 64 + "</Title>"))
    monkeypatch.setattr(metadata, "METADATA_BYTES", 2 * 1024 * 1024)
    monkeypatch.setattr(metadata, "_MAX_NODES", 2)
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        parse_comicinfo(_comic("<Title>T</Title><Writer>A</Writer>"))


def test_field_limits():
    result = parse_comicinfo(
        _comic("<Title>" + "x" * 4097 + "</Title><Writer>A</Writer>")
    )
    assert result.invalid_fields == ("title",) and result.data.authors == ("A",)
    result = parse_opf(
        _opf("".join(f"<dc:creator>Author {i}</dc:creator>" for i in range(257)))
    )
    assert result.invalid_fields == ("authors",) and result.data.authors == ()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-be"])
def test_xml_encoding(encoding):
    xml = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        "<ComicInfo><Title>中文😀</Title></ComicInfo>"
    )
    assert parse_comicinfo(xml.encode(encoding)).data.title == "中文😀"


def test_metadata_model():
    assert ReadingMetadata(title=" ", authors=("A", " A ", "")).authors == ("A",)
    with pytest.raises(ValidationError):
        ReadingMetadata.model_validate({"rating": True})
    with pytest.raises(ValidationError):
        ReadingMetadata.model_validate({"unknown": "value"})
    with pytest.raises(ValidationError):
        ReadingMetadata.model_validate({"rating": Decimal("NaN")})


@pytest.mark.parametrize("format", ["opf", "comicinfo"])
def test_render_metadata(format):
    candidate = ReadingMetadata(
        title="新书 & <新版> 😀",
        plot="Line 1\n<b>Plain text</b>",
        authors=("作者甲", "作者乙"),
        illustrators=("绘者",),
        publisher="Publisher",
        language="zh-CN",
        isbn="9780306406157",
        series="Series",
        number="1.5",
        year=2024,
        month=2,
        day=29,
        genres=("Fantasy", "Science Fiction"),
        rating=Decimal("8.51"),
        volume="0" if format == "comicinfo" else None,
        tags=("Tag",) if format == "comicinfo" else (),
        page_count=0 if format == "comicinfo" else None,
        black_and_white=False if format == "comicinfo" else None,
        cover=CoverReference(href="cover.jpg")
        if format == "opf"
        else CoverReference(page=0),
    )
    identifier = UUID("29d9aa19-d6e9-4ea6-aa47-a07b32e15d64")
    data = render_metadata(candidate, format, identifier=identifier)
    parsed = (parse_opf if format == "opf" else parse_comicinfo)(data)
    assert parsed.data == candidate and not parsed.invalid_fields
    assert data == render_metadata(candidate, format, identifier=identifier)
    assert b"&amp;" in data and b"&lt;b&gt;" in data


@pytest.mark.parametrize("format", ["opf", "comicinfo"])
def test_render_rebuild(format):
    identifier = UUID("29d9aa19-d6e9-4ea6-aa47-a07b32e15d64")
    candidate = ReadingMetadata(title="New")
    data = render_metadata(candidate, format, identifier=identifier)
    current = (parse_opf if format == "opf" else parse_comicinfo)(data).data
    assert current == (
        candidate.model_copy(update={"language": "und"})
        if format == "opf"
        else candidate
    )
    if format == "opf":
        root = etree.fromstring(data)
        assert root.get("unique-identifier") == "bookid"
        assert identifier.urn.encode() in data and b"urn:isbn:" not in data
    assert b"Old" not in data


@pytest.mark.parametrize(
    ("format", "values"),
    [
        ("opf", {"title": None}),
        ("comicinfo", {"title": "bad\x00text"}),
        ("opf", {"year": 2025, "month": 2, "day": 29}),
        ("opf", {"month": 12}),
        ("opf", {"number": "Extra"}),
        ("opf", {"tags": ("Tag",)}),
        ("opf", {"originaltitle": "Original"}),
        ("opf", {"cover": CoverReference(page=0)}),
        ("comicinfo", {"authors": ("Doe, Jane",)}),
        ("comicinfo", {"volume": "1.5"}),
        ("comicinfo", {"volume": "-1"}),
        ("comicinfo", {"isbn": "0306406152"}),
        ("comicinfo", {"cover": CoverReference(href="cover.jpg")}),
    ],
)
def test_render_invalid(format, values):
    candidate = ReadingMetadata.model_validate({"title": "Title", **values})
    with pytest.raises(ContentError, match="invalid_metadata"):
        render_metadata(candidate, format, identifier=UUID(int=1))


def test_render_limit(monkeypatch):
    monkeypatch.setattr(metadata, "METADATA_BYTES", 100)
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        render_metadata(
            ReadingMetadata(title="x" * 200), "comicinfo", identifier=UUID(int=1)
        )


def test_metadata_json():
    metadata = ReadingMetadata.model_validate_json(
        '{"title":" Book ","authors":["A"," A ",""],"rating":8.51}'
    )
    assert metadata.title == "Book" and metadata.authors == ("A",)
    assert metadata.rating == Decimal("8.51")
    assert ReadingMetadata.model_validate_json(metadata.model_dump_json()) == metadata


@pytest.mark.parametrize(
    "data",
    [
        '{"rating":true}',
        '{"rating":"not a rating"}',
        '{"rating":"NaN"}',
        '{"authors":[1]}',
        '{"authors":"A"}',
        '{"year":"2024"}',
    ],
)
def test_metadata_json_invalid(data):
    with pytest.raises(ValidationError):
        ReadingMetadata.model_validate_json(data)
