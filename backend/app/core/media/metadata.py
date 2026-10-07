"""Parse, render and combine reading metadata without persistence or file I/O."""

import mimetypes
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from lxml import etree
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.core.media.common import ContentError
from app.utils.xml import get_all_text, get_text

METADATA_BYTES = 2 * 1024 * 1024
_MAX_NODES = 100_000
_OPF = "{http://www.idpf.org/2007/opf}"
_DC = "{http://purl.org/dc/elements/1.1/}"
PARENT_FIELDS = frozenset({"authors", "illustrators", "publisher", "genres"})
type _ShortText = Annotated[str, Field(min_length=1, max_length=4096)]
type _Names = Annotated[tuple[_ShortText, ...], Field(max_length=256)]


class CoverReference(BaseModel):
    """Keep an unresolved OPF link or ComicInfo page number.

    This is an internal hint, never a rendering URL or authorization to open a
    file. The source reader must resolve it within its own file or archive boundary
    before serving it, retaining which metadata source supplied the hint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    href: str | None = Field(default=None, min_length=1, max_length=4096)
    page: int | None = Field(default=None, ge=0, le=0x7FFFFFFF)

    @model_validator(mode="after")
    def check_reference(self) -> Self:
        """Require one reference form and reject unsafe URL schemes.

        Returns:
            The validated hint, still requiring source-boundary resolution.

        Raises:
            ValueError: If the hint is ambiguous or has an unsafe URL spelling.
        """
        if (self.href is None) == (self.page is None):
            raise ValueError("exactly one cover reference is required")
        if self.href is not None:
            href = self.href
            url = urlsplit(href)
            if (
                href != href.strip()
                or re.search(r"[\x00-\x1f\x7f\\]", href)
                or re.search(r"%(?![0-9a-fA-F]{2})", href)
                or href.startswith("/")
                or url.scheme not in ("", "http", "https")
                or (url.scheme and (not url.hostname or url.username is not None))
                or (not url.scheme and not url.path)
            ):
                raise ValueError("invalid cover URL")
        return self


class ReadingMetadata(BaseModel):
    """Keep normalized reading details for the current operation only.

    Empty strings and lists mean missing; zero and false remain valid values.
    Only authors and pencillers or explicitly credited illustrators share the
    public creator fields. Other creative roles are not silently combined.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    title: _ShortText | None = None
    originaltitle: _ShortText | None = None
    plot: str | None = Field(default=None, min_length=1, max_length=1024 * 1024)
    authors: _Names = ()
    illustrators: _Names = ()
    publisher: _ShortText | None = None
    language: str | None = Field(default=None, min_length=1, max_length=64)
    isbn: str | None = None
    series: _ShortText | None = None
    volume: _ShortText | None = None
    number: _ShortText | None = None
    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    day: int | None = Field(default=None, ge=1, le=31)
    genres: _Names = ()
    tags: _Names = ()
    rating: Decimal | None = Field(default=None, ge=0, le=10, decimal_places=2)
    page_count: int | None = Field(default=None, ge=0, le=0x7FFFFFFF)
    black_and_white: bool | None = None
    cover: CoverReference | None = None

    @field_validator("*", mode="before")
    @classmethod
    def normalize_text(cls, value: object, info: ValidationInfo) -> object:
        """Normalize absent text and stable, whole-field list values.

        Args:
            value: An input field before its declared type is validated.
            info: The field and input mode, used for JSON arrays and decimal numbers.

        Returns:
            Stripped text, an ordered distinct tuple, or the unchanged value.
        """
        if isinstance(value, str):
            value = value.strip() or None
        if info.mode == "json":
            if info.field_name in {"authors", "illustrators", "genres", "tags"}:
                if isinstance(value, list):
                    value = tuple(value)
            elif (
                info.field_name == "rating"
                and isinstance(value, (str, int, float))
                and not isinstance(value, bool)
            ):
                try:
                    value = Decimal(str(value))
                except InvalidOperation as error:
                    raise ValueError("invalid rating") from error
        if isinstance(value, tuple) and all(isinstance(part, str) for part in value):
            return tuple(dict.fromkeys(part.strip() for part in value if part.strip()))
        return value

    @field_validator("isbn")
    @classmethod
    def check_isbn(cls, value: str | None) -> str | None:
        """Normalize and verify a declared ISBN without inventing a book identifier.

        Args:
            value: The declared ISBN, or None when absent.

        Returns:
            A valid ISBN-10 or ISBN-13 without spacing or hyphens.

        Raises:
            ValueError: If the declared value has an invalid format or checksum.
        """
        if value is None:
            return None
        value = re.sub(r"[\s-]", "", value).upper()
        if re.fullmatch(r"[0-9]{9}[0-9X]", value):
            digits = [10 if char == "X" else int(char) for char in value]
            if (
                sum(number * (10 - index) for index, number in enumerate(digits)) % 11
                == 0
            ):
                return value
        if (
            re.fullmatch(r"97[89][0-9]{10}", value)
            and sum(
                int(char) * (1 if index % 2 == 0 else 3)
                for index, char in enumerate(value)
            )
            % 10
            == 0
        ):
            return value
        raise ValueError("invalid ISBN")


@dataclass(frozen=True)
class ParsedMetadata:
    """Keep valid fields and controlled field names for unusable optional values."""

    data: ReadingMetadata
    invalid_fields: tuple[str, ...] = ()


def _read_xml(data: bytes, root_tags: set[str]) -> etree._Element:
    """Parse bounded metadata bytes with external resolution and recovery disabled.

    Args:
        data: A complete metadata document obtained by a caller-owned bounded read.
        root_tags: The exact supported namespace-qualified document elements.

    Returns:
        The validated XML root, without following references or loading a schema.

    Raises:
        ContentError: If XML is malformed, unsafe, unsupported or over limits.
    """
    if len(data) > METADATA_BYTES:
        raise ContentError("media_limit_exceeded")
    try:
        root = etree.fromstring(
            data,
            etree.XMLParser(
                resolve_entities=False,
                load_dtd=False,
                no_network=True,
                recover=False,
                huge_tree=False,
                remove_comments=True,
                remove_pis=True,
            ),
        )
    except etree.XMLSyntaxError as error:
        raise ContentError("invalid_metadata") from error
    if root.tag not in root_tags or root.getroottree().docinfo.internalDTD is not None:
        raise ContentError("invalid_metadata")
    for index, element in enumerate(root.iter()):
        if index >= _MAX_NODES:
            raise ContentError("media_limit_exceeded")
        if (
            not isinstance(element.tag, str)
            or element.get("{http://www.w3.org/XML/1998/namespace}base") is not None
            or element.tag == "{http://www.w3.org/2001/XInclude}include"
        ):
            raise ContentError("invalid_metadata")
    return root


def _validated(values: dict[str, object], invalid: list[str]) -> ParsedMetadata:
    """Keep usable fields while reporting invalid optional fields independently.

    Args:
        values: Only supported application fields extracted from XML.
        invalid: Field names already rejected during format-specific conversion.

    Returns:
        Normalized details and deduplicated names, without raw invalid values.
    """
    try:
        metadata = ReadingMetadata.model_validate(values)
    except ValidationError as error:
        for detail in error.errors(include_url=False, include_input=False):
            name = str(detail["loc"][0])
            values.pop(name, None)
            invalid.append(name)
        metadata = ReadingMetadata.model_validate(values)
    if metadata.month is not None and metadata.day is not None:
        try:
            date(metadata.year or 2000, metadata.month, metadata.day)
        except ValueError:
            metadata = metadata.model_copy(update={"day": None})
            invalid.append("day")
    return ParsedMetadata(metadata, tuple(dict.fromkeys(invalid)))


def _integer(value: str | None) -> int | str | None:
    """Convert an XML integer while retaining malformed values for validation.

    Args:
        value: Optional XML text; -1 is the ComicInfo unknown-value sentinel.

    Returns:
        A parsed integer, None for an unknown value, or the original invalid text.
    """
    if value in (None, "", "-1"):
        return None
    return int(value) if re.fullmatch(r"[+-]?[0-9]{1,10}", value) else value


def _decimal(value: str | None) -> Decimal | str | None:
    """Convert a finite XML decimal without accepting exponents or special values.

    Args:
        value: Optional numeric text from the source format.

    Returns:
        A decimal, None, or invalid text for field validation.
    """
    if not value:
        return None
    if len(value) > 32 or not re.fullmatch(
        r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", value
    ):
        return value
    return Decimal(value)


def parse_opf(data: bytes) -> ParsedMetadata:
    """Read OPF 2 or 3 description fields without requiring a readable EPUB body.

    Accept an OPF package with one metadata element, or a standalone namespaced
    metadata element. Cover links remain relative to this particular OPF source.
    Descriptions are plain strings, including escaped markup, never rendered HTML.

    Args:
        data: Bounded external OPF bytes or the selected embedded package bytes.

    Returns:
        Normalized fields and names of unusable optional fields.

    Raises:
        ContentError: If the XML boundary or required metadata structure is invalid.
    """
    root = _read_xml(data, {f"{_OPF}package", f"{_OPF}metadata"})
    if root.tag == f"{_OPF}package":
        if root.get("version") not in (None, "2.0", "3.0"):
            raise ContentError("invalid_metadata")
        groups = root.findall(f"{_OPF}metadata")
        if len(groups) != 1:
            raise ContentError("invalid_metadata")
        metadata = groups[0]
    else:
        metadata = root
    invalid: list[str] = []
    values: dict[str, object] = {
        name: get_text(metadata, f"{_DC}{tag}")
        for name, tag in (
            ("title", "title"),
            ("plot", "description"),
            ("publisher", "publisher"),
            ("language", "language"),
        )
    }
    values["genres"] = tuple(get_all_text(metadata, f"{_DC}subject") or ())
    refinements: dict[str, dict[str, str]] = {}
    extensions: dict[str, str] = {}
    for meta in metadata.findall(f"{_OPF}meta"):
        reference, prop = meta.get("refines", ""), meta.get("property", "")
        value = (meta.text or "").strip()
        if (
            reference.startswith("#")
            and prop
            and value
            and (prop != "identifier-type" or meta.get("scheme") == "onix:codelist5")
        ):
            refinements.setdefault(reference[1:], {}).setdefault(prop, value)
        name = meta.get("name")
        if name is not None and meta.get("content"):
            extensions.setdefault(name, meta.get("content", "").strip())
    for title in metadata.findall(f"{_DC}title"):
        if (
            refinements.get(title.get("id", ""), {}).get("title-type") == "main"
            and (title.text or "").strip()
        ):
            values["title"] = title.text
            break
    authors, illustrators = [], []
    for element in metadata:
        if (
            element.tag not in (f"{_DC}creator", f"{_DC}contributor")
            or not element.text
        ):
            continue
        role = refinements.get(element.get("id", ""), {}).get("role")
        role = role or element.get(f"{_OPF}role")
        if role == "aut" or (role is None and element.tag == f"{_DC}creator"):
            authors.append(element.text)
        elif role == "ill":
            illustrators.append(element.text)
    values.update(authors=tuple(authors), illustrators=tuple(illustrators))
    for identifier in metadata.findall(f"{_DC}identifier"):
        value = (identifier.text or "").strip()
        scheme = identifier.get(f"{_OPF}scheme", "").casefold()
        refinement = refinements.get(identifier.get("id", ""), {})
        if (
            scheme == "isbn"
            or value.casefold().startswith("urn:isbn:")
            or refinement.get("identifier-type") in ("02", "15")
        ):
            values["isbn"] = (
                value[9:] if value.casefold().startswith("urn:isbn:") else value
            )
            break
    published = get_text(metadata, f"{_DC}date")
    if published:
        try:
            if re.fullmatch(r"[0-9]{4}(?:-[0-9]{2})?", published):
                pieces = [int(part) for part in published.split("-")]
                date(pieces[0], pieces[1] if len(pieces) > 1 else 1, 1)
                values["year"] = pieces[0]
                if len(pieces) > 1:
                    values["month"] = pieces[1]
            else:
                when = (
                    datetime.fromisoformat(published)
                    if "T" in published
                    else date.fromisoformat(published)
                )
                values.update(year=when.year, month=when.month, day=when.day)
        except ValueError:
            invalid.append("year")
    values.update(
        series=extensions.get("calibre:series"),
        number=extensions.get("calibre:series_index"),
        rating=_decimal(extensions.get("calibre:rating")),
    )
    for meta in metadata.findall(f"{_OPF}meta"):
        props = refinements.get(meta.get("id", ""), {})
        if (
            meta.get("property") == "belongs-to-collection"
            and props.get("collection-type") == "series"
            and (meta.text or "").strip()
        ):
            values.update(series=meta.text, number=props.get("group-position"))
            break
    number = values.get("number")
    if (
        isinstance(number, str)
        and number.strip()
        and not isinstance(_decimal(number.strip()), Decimal)
    ):
        values.pop("number")
        invalid.append("number")
    covers = [
        item
        for item in root.findall(f"{_OPF}manifest/{_OPF}item")
        if "cover-image" in item.get("properties", "").split()
    ]
    if not covers and extensions.get("cover"):
        covers = [
            item
            for item in root.findall(f"{_OPF}manifest/{_OPF}item")
            if item.get("id") == extensions["cover"]
        ]
    if len(covers) == 1 and covers[0].get("href"):
        values["cover"] = {"href": covers[0].get("href")}
    elif covers or extensions.get("cover"):
        invalid.append("cover")
    return _validated(values, invalid)


def parse_comicinfo(data: bytes) -> ParsedMetadata:
    """Read ComicInfo fields without changing page order or following cover hints.

    Only Writer and Penciller populate the public creator fields. Other creative
    roles remain unsupported rather than being mislabeled as the same role.

    Args:
        data: Bounded external or embedded ComicInfo.xml bytes.

    Returns:
        Normalized fields and names of unusable optional fields.

    Raises:
        ContentError: If XML is unsafe, invalid or over limits.
    """
    root = _read_xml(data, {"ComicInfo"})
    invalid: list[str] = []
    values: dict[str, object] = {
        name: get_text(root, tag)
        for name, tag in (
            ("title", "Title"),
            ("plot", "Summary"),
            ("publisher", "Publisher"),
            ("language", "LanguageISO"),
            ("series", "Series"),
            ("number", "Number"),
        )
    }
    for name, tag in (
        ("authors", "Writer"),
        ("illustrators", "Penciller"),
        ("genres", "Genre"),
        ("tags", "Tags"),
    ):
        value = get_text(root, tag)
        values[name] = tuple(value.split(",")) if value else ()
    for name, tag in (
        ("year", "Year"),
        ("month", "Month"),
        ("day", "Day"),
        ("page_count", "PageCount"),
    ):
        values[name] = _integer(get_text(root, tag))
    volume = _integer(get_text(root, "Volume"))
    if isinstance(volume, int) and 0 <= volume <= 0x7FFFFFFF:
        values["volume"] = str(volume)
    elif volume is not None:
        invalid.append("volume")
    rating = _decimal(get_text(root, "CommunityRating"))
    values["rating"] = rating * 2 if isinstance(rating, Decimal) else rating
    monochrome = get_text(root, "BlackAndWhite")
    if monochrome in ("Yes", "No"):
        values["black_and_white"] = monochrome == "Yes"
    elif monochrome not in (None, "", "Unknown"):
        invalid.append("black_and_white")
    if get_text(root, "Manga") in ("Yes", "YesAndRightToLeft"):
        values["originaltitle"] = get_text(root, "AlternateSeries")
    gtin = get_text(root, "GTIN")
    if gtin and gtin.startswith(("978", "979")):
        values["isbn"] = gtin
    covers = [
        page
        for page in root.findall("Pages/Page")
        if "FrontCover" in page.get("Type", "").split()
    ]
    if len(covers) == 1:
        values["cover"] = {"page": _integer(covers[0].get("Image"))}
    elif covers:
        invalid.append("cover")
    return _validated(values, invalid)


def merge_metadata(
    external: ReadingMetadata | None,
    embedded: ReadingMetadata | None,
    *,
    parent: ReadingMetadata | None = None,
) -> ReadingMetadata:
    """Fill missing fields without merging lists or retaining previous details.

    This pure operation performs no reads or writes. The caller verifies source
    ownership, reads current files, preserves source issues and resolves the
    selected cover in the context of the source that supplied it. Date components
    are filled only when compatible with the already selected date components.

    Args:
        external: The item's current valid external fields, or None if unavailable.
        embedded: The item's current valid embedded fields, or None if unavailable.
        parent: Current metadata of a verified direct comic parent; None for novels
            and standalone comics. Only the four public fields may be inherited.

    Returns:
        A new value using external, embedded and allowed parent fields in order.
    """
    values = {}
    for name in ReadingMetadata.model_fields:
        for source in (external, embedded, parent if name in PARENT_FIELDS else None):
            if source is not None:
                value = getattr(source, name)
                if value is not None and value != ():
                    if name in ("month", "day"):
                        earlier = ("year",) if name == "month" else ("year", "month")
                        if any(
                            part in values
                            and getattr(source, part) not in (None, values[part])
                            for part in earlier
                        ):
                            continue
                    if name == "day" and "month" in values:
                        try:
                            date(values.get("year", 2000), values["month"], value)
                        except ValueError:
                            continue
                    values[name] = value
                    break
    return ReadingMetadata.model_validate(values)


def render_metadata(
    metadata: ReadingMetadata, format: Literal["opf", "comicinfo"], *, identifier: UUID
) -> bytes:
    """Rebuild an external metadata document using only the selected candidate.

    Unsupported or lossy field mappings fail round-trip validation. This generates
    a descriptive OPF sidecar, not an EPUB package containing readable body files.

    Args:
        metadata: The normalized candidate with a nonempty title.
        format: The external OPF or ComicInfo format selected by the library.
        identifier: A stable local UUID for OPF without ISBN, retained by the caller
            across publication retries and ignored for ComicInfo.

    Returns:
        Bounded UTF-8 XML bytes that can be read back without losing candidate fields.

    Raises:
        ContentError: If fields cannot be represented, XML is invalid or too large.
    """
    if metadata.title is None:
        raise ContentError("invalid_metadata")
    try:
        if format == "opf":
            metadata = metadata.model_copy(
                update={"language": metadata.language or "und"}
            )
            root = etree.Element(
                f"{_OPF}package",
                nsmap={"opf": _OPF[1:-1], "dc": _DC[1:-1]},
                attrib={"version": "2.0", "unique-identifier": "bookid"},
            )
            group = etree.SubElement(root, f"{_OPF}metadata")
            value = f"urn:isbn:{metadata.isbn}" if metadata.isbn else identifier.urn
            etree.SubElement(group, f"{_DC}identifier", id="bookid").text = value
            for name, tag in (
                ("title", "title"),
                ("plot", "description"),
                ("publisher", "publisher"),
                ("language", "language"),
            ):
                value = getattr(metadata, name)
                if value is not None:
                    etree.SubElement(group, f"{_DC}{tag}").text = value
            for names, tag, role in (
                (metadata.authors, "creator", "aut"),
                (metadata.illustrators, "contributor", "ill"),
            ):
                for name in names:
                    etree.SubElement(
                        group, f"{_DC}{tag}", {f"{_OPF}role": role}
                    ).text = name
            for genre in metadata.genres:
                etree.SubElement(group, f"{_DC}subject").text = genre
            if metadata.year is not None:
                published = f"{metadata.year:04}"
                if metadata.month is not None:
                    published += f"-{metadata.month:02}"
                    if metadata.day is not None:
                        published += f"-{metadata.day:02}"
                etree.SubElement(group, f"{_DC}date").text = published
            for name, tag in (
                ("series", "series"),
                ("number", "series_index"),
                ("rating", "rating"),
            ):
                value = getattr(metadata, name)
                if value is not None:
                    etree.SubElement(
                        group, f"{_OPF}meta", name=f"calibre:{tag}", content=str(value)
                    )
            manifest = etree.SubElement(root, f"{_OPF}manifest")
            if metadata.cover and metadata.cover.href:
                href = metadata.cover.href
                mime = mimetypes.guess_file_type(urlsplit(href).path)[0]
                if not mime or not mime.startswith("image/"):
                    raise ContentError("invalid_metadata")
                etree.SubElement(group, f"{_OPF}meta", name="cover", content="cover")
                etree.SubElement(
                    manifest,
                    f"{_OPF}item",
                    {"id": "cover", "href": href, "media-type": mime},
                )
            etree.SubElement(root, f"{_OPF}spine")
            parser = parse_opf
        elif format == "comicinfo":
            root = etree.Element("ComicInfo")
            for name, tag in (
                ("title", "Title"),
                ("series", "Series"),
                ("number", "Number"),
                ("volume", "Volume"),
                ("plot", "Summary"),
                ("year", "Year"),
                ("month", "Month"),
                ("day", "Day"),
                ("authors", "Writer"),
                ("illustrators", "Penciller"),
                ("publisher", "Publisher"),
                ("genres", "Genre"),
                ("tags", "Tags"),
                ("page_count", "PageCount"),
                ("language", "LanguageISO"),
                ("isbn", "GTIN"),
            ):
                value = getattr(metadata, name)
                if value is not None and value != ():
                    etree.SubElement(root, tag).text = (
                        ", ".join(value) if isinstance(value, tuple) else str(value)
                    )
            if metadata.black_and_white is not None:
                etree.SubElement(root, "BlackAndWhite").text = (
                    "Yes" if metadata.black_and_white else "No"
                )
            if metadata.rating is not None:
                etree.SubElement(root, "CommunityRating").text = str(
                    metadata.rating / 2
                )
            if metadata.cover and metadata.cover.page is not None:
                pages = etree.SubElement(root, "Pages")
                etree.SubElement(
                    pages, "Page", Image=str(metadata.cover.page), Type="FrontCover"
                )
            parser = parse_comicinfo
        else:
            raise ContentError("unsupported_media_format")
        data = etree.tostring(
            root, encoding="utf-8", xml_declaration=True, pretty_print=True
        )
    except (ValueError, etree.LxmlError) as error:
        if isinstance(error, ContentError):
            raise
        raise ContentError("invalid_metadata") from error
    result = parser(data)
    if result.invalid_fields or result.data != metadata:
        raise ContentError("invalid_metadata")
    return data
