"""Convert bounded EPUB XHTML documents into ordered reading blocks."""

import hashlib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import groupby
from typing import Literal, Protocol, cast
from zipfile import ZipFile

from lxml import etree

from app.core.media.archive import read_member
from app.core.media.common import ContentError
from app.core.media.epub import (
    DOCUMENT_BYTES,
    EpubPackage,
    EpubResource,
    resolve_epub_reference,
)

_XHTML = "{http://www.w3.org/1999/xhtml}"
_SVG = "{http://www.w3.org/2000/svg}"
_MAX_NODES = 100_000
_MAX_BLOCKS = 20_000
_SPACE = re.compile(r"[ \t\r\n]+")
_CONTAINERS = {
    "address",
    "article",
    "aside",
    "div",
    "figure",
    "figcaption",
    "footer",
    "header",
    "main",
    "section",
    "p",
    "li",
    "dt",
    "dd",
}
_OMITTED = {"script", "style", "link", "meta", "base", "source"}
_UNSUPPORTED = {
    "audio",
    "video",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "canvas",
}
_TABLE = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption"}
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

type Mark = Literal["strong", "em"]
type TextKind = Literal["paragraph", "heading", "quote"]
type WarningCode = Literal[
    "missing_image",
    "external_image",
    "invalid_image_reference",
    "encrypted_image",
    "unsupported_image",
    "invalid_image",
    "image_limit_exceeded",
    "unsupported_content",
    "simplified_layout",
]


class _DTDEntities(Protocol):
    """Describe the entity iterator omitted by lxml-stubs 0.5.1."""

    def iterentities(self) -> Iterator[object]:
        """Iterate internal entity declarations.

        Returns:
            The declarations exposed by lxml.etree.DTD.iterentities.
        """
        ...


@dataclass(frozen=True)
class TextRun:
    """Keep plain text and the supported semantic emphasis marks."""

    text: str
    marks: tuple[Mark, ...] = ()


@dataclass(frozen=True)
class TextBlock:
    """Keep a paragraph, heading or quote without source markup."""

    id: str
    type: TextKind
    runs: tuple[TextRun, ...]
    level: int | None = None


@dataclass(frozen=True)
class ListBlock:
    """Keep simple list items in their original reading order."""

    id: str
    ordered: bool
    items: tuple[tuple[TextRun, ...], ...]
    start: int | None = None
    type: Literal["list"] = "list"


@dataclass(frozen=True)
class ImageBlock:
    """Locate a declared image candidate or retain an unavailable-image placeholder."""

    id: str
    asset_id: str | None
    alt: str
    type: Literal["image"] = "image"


type ContentBlock = TextBlock | ListBlock | ImageBlock


@dataclass(frozen=True)
class ContentWarning:
    """Associate a controlled reason with a block, without exposing source URLs."""

    code: WarningCode
    block_id: str


@dataclass(frozen=True)
class XhtmlDocument:
    """Keep converted content and internal candidates awaiting image-byte validation.

    Assets contain source members for the later cache builder, not HTTP payloads.
    Only validated image bytes may be served; no source URL is a rendering URL.
    """

    title: str | None
    blocks: tuple[ContentBlock, ...]
    warnings: tuple[ContentWarning, ...]
    assets: dict[str, EpubResource]


def _runs(parts: list[TextRun], *, preserve: bool = False) -> tuple[TextRun, ...]:
    """Combine adjacent emphasis spans and trim collapsed block-edge spaces.

    Args:
        parts: Text fragments in source order.
        preserve: Keep edge whitespace for preformatted text; false by default.

    Returns:
        Nonempty spans with normalized block edges.
    """
    result = [
        TextRun("".join(part.text for part in group), marks)
        for marks, group in groupby(parts, key=lambda part: part.marks)
    ]
    if not preserve:
        while result and not result[0].text.strip(" "):
            result.pop(0)
        while result and not result[-1].text.strip(" "):
            result.pop()
        if result:
            result[0] = TextRun(result[0].text.lstrip(" "), result[0].marks)
            result[-1] = TextRun(result[-1].text.rstrip(" "), result[-1].marks)
    return tuple(part for part in result if part.text)


def _plain_text(element: etree._Element) -> str:
    """Collect alternate text once while excluding script and style contents.

    Args:
        element: The validated source element whose visible text is retained.

    Returns:
        Plain text with collapsed HTML whitespace.
    """
    parts: list[str] = []

    def visit(node: etree._Element):
        """Append one subtree's text, leaving its tail to the parent.

        Args:
            node: The current source element.
        """
        if etree.QName(node).localname in _OMITTED:
            return
        separated = node.tag in {f"{_SVG}title", f"{_SVG}desc", f"{_SVG}text"}
        if separated:
            parts.append(" ")
        parts.append(node.text or "")
        for child in node:
            visit(child)
            parts.append(child.tail or "")
        if separated:
            parts.append(" ")

    visit(element)
    return _SPACE.sub(" ", "".join(parts)).strip()


class _Converter:
    """Preserve document order while discarding executable markup and styling."""

    def __init__(self, root: etree._Element, path: str, package: EpubPackage):
        """Prepare source positions and the package's local resource lookup.

        Args:
            root: The validated XHTML document element.
            path: The normalized spine member path.
            package: Its validated EPUB package.
        """
        self.path = path
        self.positions = {element: index for index, element in enumerate(root.iter())}
        self.parts: dict[etree._Element, int] = {}
        self.resources = {
            resource.path: resource
            for resource in package.resources.values()
            if resource.path is not None
        }
        self.assets: dict[str, EpubResource] = {}
        self.warnings: list[ContentWarning] = []
        self.warning_keys: set[ContentWarning] = set()
        self.warning_blocks: set[str] = set()
        self.count = 0

    def identifier(self, element: etree._Element) -> str:
        """Identify one output fragment by source position within this document.

        Args:
            element: The originating source element.

        Returns:
            An opaque deterministic block ID.

        Raises:
            ContentError: If the conversion exceeds its block budget.
        """
        self.count += 1
        if self.count > _MAX_BLOCKS:
            raise ContentError("media_limit_exceeded")
        part = self.parts.get(element, 0)
        self.parts[element] = part + 1
        key = f"xhtml:{self.path}:{self.positions[element]}:{part}"
        return hashlib.sha256(key.encode()).hexdigest()[:32]

    def warn(
        self, code: WarningCode, blocks: list[ContentBlock], element: etree._Element
    ):
        """Attach a warning to existing content or an empty placeholder.

        Args:
            code: The controlled warning reason.
            blocks: Converted content, extended when a placeholder is needed.
            element: The source element used for a new placeholder ID.
        """
        if not blocks:
            blocks.append(TextBlock(self.identifier(element), "paragraph", ()))
        warning = ContentWarning(code, blocks[0].id)
        if warning not in self.warning_keys:
            self.warnings.append(warning)
            self.warning_keys.add(warning)
            self.warning_blocks.add(warning.block_id)

    def image(self, element: etree._Element, reference: str, alt: str) -> ImageBlock:
        """Resolve a local manifest image without reading or trusting its bytes.

        Args:
            element: The image's source element.
            reference: Its original resource reference, never returned to a reader.
            alt: Plain alternate text retained even when the resource is unavailable.

        Returns:
            An image block with an opaque candidate ID or a warning placeholder.
        """
        identifier = self.identifier(element)
        code: WarningCode | None = None
        resource = None
        try:
            path = resolve_epub_reference(self.path, reference)
            if path is None:
                code = "external_image"
            else:
                resource = self.resources.get(path)
                if resource is None or resource.member is None:
                    code = "missing_image"
                elif resource.encrypted:
                    code = "encrypted_image"
                elif resource.media_type not in _IMAGE_MIMES:
                    code = "unsupported_image"
        except ContentError:
            code = "invalid_image_reference"
        if code is not None or resource is None:
            block = ImageBlock(identifier, None, alt)
            self.warn(code or "missing_image", [block], element)
            return block
        asset_id = hashlib.sha256(f"epub:{resource.path}".encode()).hexdigest()[:32]
        self.assets[asset_id] = resource
        return ImageBlock(identifier, asset_id, alt)

    def svg(self, element: etree._Element) -> ImageBlock:
        """Extract a bitmap from a simple SVG wrapper or keep its alternate text.

        Args:
            element: The inline SVG document element.

        Returns:
            A bitmap candidate or a placeholder for unsupported vector content.
        """
        nodes = list(element.iter())
        images = [node for node in nodes if node.tag == f"{_SVG}image"]
        alt = _plain_text(element)
        if len(images) == 1 and all(
            node.tag
            in {f"{_SVG}{name}" for name in ("svg", "g", "title", "desc", "image")}
            for node in nodes
        ):
            reference = images[0].get("href") or images[0].get(
                "{http://www.w3.org/1999/xlink}href", ""
            )
            return self.image(element, reference, alt)
        identifier = self.identifier(element)
        block = ImageBlock(identifier, None, alt)
        self.warn("unsupported_image", [block], element)
        return block

    def listing(
        self, element: etree._Element, marks: tuple[Mark, ...]
    ) -> list[ContentBlock]:
        """Keep simple lists and flatten complex items without losing content order.

        Args:
            element: The ordered or unordered list element.
            marks: Emphasis inherited from surrounding inline markup.

        Returns:
            A list block, or ordered fallback blocks with a layout warning.
        """
        start = element.get("start")
        simple = (
            not (element.text or "").strip()
            and all(
                child.tag == f"{_XHTML}li"
                and child.get("value") is None
                and not (child.tail or "").strip()
                for child in element
            )
            and element.get("reversed") is None
            and (start is None or re.fullmatch(r"[+-]?[0-9]{1,7}", start) is not None)
        )
        if not simple:
            blocks = self.flow(element, marks)
            self.warn("simplified_layout", blocks, element)
            return blocks
        groups = [self.flow(child, marks) for child in element]
        if any(
            not isinstance(block, TextBlock)
            or block.type != "paragraph"
            or block.id in self.warning_blocks
            for group in groups
            for block in group
        ):
            blocks = [block for group in groups for block in group]
            self.warn("simplified_layout", blocks, element)
            return blocks
        items = []
        for group in groups:
            parts: list[TextRun] = []
            for block in group:
                if isinstance(block, TextBlock):
                    if parts:
                        parts.append(TextRun("\n"))
                    parts.extend(block.runs)
            items.append(_runs(parts))
        if not items:
            return []
        ordered = element.tag == f"{_XHTML}ol"
        return [
            ListBlock(
                self.identifier(element),
                ordered,
                tuple(items),
                int(start) if ordered and start is not None else None,
            )
        ]

    def flow(
        self,
        element: etree._Element,
        marks: tuple[Mark, ...] = (),
        kind: TextKind = "paragraph",
        level: int | None = None,
        *,
        preserve: bool = False,
    ) -> list[ContentBlock]:
        """Convert mixed children while preserving text and image order.

        Args:
            element: The container whose children are converted.
            marks: Inherited emphasis; empty by default.
            kind: The surrounding text block kind; paragraph by default.
            level: The heading level, or None outside a heading.
            preserve: Preserve preformatted whitespace; false by default.

        Returns:
            Ordered blocks without original tags, attributes, scripts or styles.
        """
        blocks: list[ContentBlock] = []
        pending: list[TextRun] = []

        def text(
            value: str | None, emphasis: tuple[Mark, ...], *, newline: bool = False
        ):
            """Append text with HTML whitespace handling and inherited emphasis.

            Args:
                value: Source text, or None when no text is present.
                emphasis: The current supported marks.
                newline: Preserve an explicit break; false for ordinary text.
            """
            if value:
                value = value if preserve or newline else _SPACE.sub(" ", value)
                if not preserve and pending and pending[-1].text.endswith((" ", "\n")):
                    value = value.lstrip(" ")
                if value:
                    pending.append(TextRun(value, emphasis))

        def flush():
            """Publish pending text before crossing a block or image boundary."""
            runs = _runs(pending, preserve=preserve)
            if any(run.text.strip() for run in runs):
                blocks.append(TextBlock(self.identifier(element), kind, runs, level))
            pending.clear()

        def visit(node: etree._Element, emphasis: tuple[Mark, ...]):
            """Convert one element while leaving its tail to the parent traversal.

            Args:
                node: The current validated source element.
                emphasis: The supported marks inherited by this element.
            """
            name = etree.QName(node).localname
            if name in _OMITTED:
                return
            if node.tag == f"{_SVG}svg":
                flush()
                blocks.append(self.svg(node))
            elif not str(node.tag).startswith(_XHTML):
                flush()
                converted = self.flow(node, emphasis)
                self.warn("simplified_layout", converted, node)
                blocks.extend(converted)
            elif name == "img":
                flush()
                blocks.append(
                    self.image(node, node.get("src", ""), node.get("alt", ""))
                )
            elif name in ("br", "hr"):
                text("\n", emphasis, newline=True)
            elif name in ("ul", "ol"):
                flush()
                blocks.extend(self.listing(node, emphasis))
            elif name in _UNSUPPORTED:
                flush()
                converted = self.flow(node, emphasis)
                self.warn("unsupported_content", converted, node)
                blocks.extend(converted)
            elif (
                name in _CONTAINERS
                or name in _TABLE
                or name
                in {
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                    "blockquote",
                    "pre",
                    "dl",
                }
            ):
                flush()
                heading = name in {"h1", "h2", "h3", "h4", "h5", "h6"}
                converted = self.flow(
                    node,
                    emphasis,
                    "heading" if heading else "quote" if name == "blockquote" else kind,
                    int(name[1]) if heading else level,
                    preserve=preserve or name == "pre",
                )
                if name in ("table", "dl"):
                    self.warn("simplified_layout", converted, node)
                blocks.extend(converted)
            else:
                if name in ("strong", "b", "em", "i"):
                    mark: Mark = "strong" if name in ("strong", "b") else "em"
                    emphasis = tuple(
                        value
                        for value in ("strong", "em")
                        if value in (*emphasis, mark)
                    )
                text(node.text, emphasis)
                for child in node:
                    visit(child, emphasis)
                    text(child.tail, emphasis)

        text(element.text, marks)
        for child in element:
            visit(child, marks)
            text(child.tail, marks)
        flush()
        return blocks


def read_xhtml_document(
    archive: ZipFile,
    package: EpubPackage,
    resource_id: str,
) -> XhtmlDocument:
    """Read one spine document into safe blocks inside a stable archive context.

    Run synchronously in a worker inside open_archive. Images are manifest
    candidates only; byte validation, navigation titles and caching belong to
    the caller. Empty documents remain empty for whole-book validation later.

    Args:
        archive: The archive yielded by open_archive after caller access checks.
        package: The package loaded from the same archive.
        resource_id: The original manifest ID of a document in its spine.

    Returns:
        Ordered blocks, controlled warnings and internal image candidates.

    Raises:
        ContentError: If the ID is unknown or XHTML is invalid, unsupported
            or over limits.
        OSError: If the source cannot be read; open_archive handles ZIP failures
            and checks source stability on exit.
    """
    resource = next((item for item in package.spine if item.id == resource_id), None)
    if resource is None:
        raise ContentError("not_found")
    if resource.member is None or resource.path is None:
        raise ContentError("invalid_epub")
    data = read_member(archive, resource.member, DOCUMENT_BYTES)
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
        raise ContentError("invalid_epub") from error
    if root.tag != f"{_XHTML}html":
        raise ContentError("invalid_epub")
    dtd = root.getroottree().docinfo.internalDTD
    if dtd is not None:
        declarations = cast(_DTDEntities, dtd)
        if next(declarations.iterentities(), None) is not None:
            raise ContentError("unsupported_media_format")
    for index, element in enumerate(root.iter()):
        if index >= _MAX_NODES:
            raise ContentError("media_limit_exceeded")
        if not isinstance(element.tag, str):
            raise ContentError("unsupported_media_format")
        if (
            element.get("{http://www.w3.org/XML/1998/namespace}base") is not None
            or element.tag == "{http://www.w3.org/2001/XInclude}include"
            or (element.tag == f"{_XHTML}base" and element.get("href") is not None)
        ):
            raise ContentError("unsupported_media_format")
    bodies = root.findall(f"{_XHTML}body")
    if len(bodies) != 1:
        raise ContentError("invalid_epub")
    converter = _Converter(root, resource.path, package)
    blocks = converter.flow(bodies[0])
    title = next(
        (
            "".join(run.text for run in block.runs).strip()
            for block in blocks
            if isinstance(block, TextBlock) and block.type == "heading"
        ),
        "",
    )
    if not title:
        titles = root.findall(f"{_XHTML}head/{_XHTML}title")
        title = _plain_text(titles[0]) if titles else ""
    return XhtmlDocument(
        title[:120] or None,
        tuple(blocks),
        tuple(converter.warnings),
        converter.assets,
    )
