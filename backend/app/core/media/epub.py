"""Read EPUB package structure and resolve bounded local resources."""

import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from lxml import etree

from app.core.media.archive import normalize_member_path, read_member
from app.core.media.common import ContentError

DOCUMENT_BYTES = 8 * 1024 * 1024
_XML_BYTES = 2 * 1024 * 1024
_MAX_RESOURCES = 20_000
_MAX_SPINE_ITEMS = 10_000
_CONTAINER = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_OPF = "{http://www.idpf.org/2007/opf}"
_XMLENC = "{http://www.w3.org/2001/04/xmlenc#}"
_XHTML_MIME = "application/xhtml+xml"
_PACKAGE_MIME = "application/oebps-package+xml"
_MIMETYPE = b"application/epub+zip"
_BAD_PATH = re.compile(r"[\x00-\x1f\x7f\\]")


@dataclass(frozen=True)
class EpubResource:
    """Locate a declared resource without fetching remote or missing members.

    The ID belongs to the manifest, not an HTTP resource URL. External URLs have
    no path; absent files have no member. MIME and encryption are declarations,
    so callers must validate bytes and reject encrypted content before rendering.
    """

    id: str
    path: str | None
    media_type: str
    properties: frozenset[str]
    member: ZipInfo | None
    encrypted: bool


@dataclass(frozen=True)
class EpubPackage:
    """Keep an in-memory manifest, spine and declared navigation and cover."""

    path: str
    resources: dict[str, EpubResource]
    spine: tuple[EpubResource, ...]
    navigation: EpubResource | None
    cover: EpubResource | None


def resolve_epub_reference(base_path: str, reference: str) -> str | None:
    """Resolve a resource URL within the package root without accessing files.

    Args:
        base_path: The normalized referring member path, or empty for the root.
        reference: A relative URL, optionally including a fragment identifier.

    Returns:
        The normalized local member path, or None for an external URL.

    Raises:
        ContentError: If a local reference is malformed, ambiguous or escapes the root.
    """
    try:
        if (
            not reference
            or len(reference) > 4096
            or reference != reference.strip()
            or _BAD_PATH.search(reference)
        ):
            raise ValueError("invalid resource URL")
        url = urlsplit(reference)
        if url.scheme or url.netloc or reference.startswith("//"):
            return None
        if url.query or re.search(r"%(?![0-9a-fA-F]{2})", url.path):
            raise ValueError("invalid local URL")
        path = unquote(url.path, errors="strict")
        if (
            _BAD_PATH.search(path)
            or path.startswith("/")
            or PureWindowsPath(path).drive
        ):
            raise ValueError("invalid local path")
        base = normalize_member_path(base_path) if base_path else ""
        if not path:
            return normalize_member_path(base)
        parts = base.split("/")[:-1]
        for part in path.split("/"):
            if part == "..":
                if not parts:
                    raise ValueError("resource outside package")
                parts.pop()
            elif part not in ("", "."):
                parts.append(part)
        return normalize_member_path("/".join(parts))
    except ValueError as error:
        raise ContentError("invalid_epub") from error


def _read_xml(
    archive: ZipFile, member: ZipInfo | None, root_tag: str
) -> etree._Element:
    """Read a required package XML member with external resolution disabled.

    Args:
        archive: The archive yielded by open_archive.
        member: The selected member, or None when a required file is missing.
        root_tag: The expected namespace-qualified document element.

    Returns:
        The validated XML root, without loading DTDs, entities or external files.

    Raises:
        ContentError: If XML is missing, malformed, unsafe, unsupported or over limits.
    """
    if member is None:
        raise ContentError("invalid_epub")
    data = read_member(archive, member, _XML_BYTES)
    try:
        root = etree.fromstring(
            data,
            parser=etree.XMLParser(
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
    if root.tag != root_tag or root.getroottree().docinfo.doctype:
        raise ContentError("invalid_epub")
    for element in root.iter():
        if (
            element.get("{http://www.w3.org/XML/1998/namespace}base") is not None
            or element.tag == "{http://www.w3.org/2001/XInclude}include"
        ):
            raise ContentError("unsupported_media_format")
    return root


def _encrypted_paths(archive: ZipFile, members: dict[str, ZipInfo]) -> set[str]:
    """Locate encrypted or obfuscated members without attempting decryption.

    Args:
        archive: The archive yielded by open_archive.
        members: Validated local files keyed by normalized member path.

    Returns:
        The referenced paths, including obfuscated fonts that will not be loaded.

    Raises:
        ContentError: If encryption declarations are malformed or over limits.
    """
    member = members.get("META-INF/encryption.xml")
    if member is None:
        return set()
    root = _read_xml(archive, member, f"{_CONTAINER}encryption")
    paths = set()
    for element in root.findall(f"{_XMLENC}EncryptedData"):
        reference = element.find(f"{_XMLENC}CipherData/{_XMLENC}CipherReference")
        if reference is None:
            raise ContentError("invalid_epub")
        path = resolve_epub_reference("", reference.get("URI", ""))
        if path is None or path in paths:
            raise ContentError("invalid_epub")
        paths.add(path)
    return paths


def _parse_manifest(
    root: etree._Element,
    package_path: str,
    members: dict[str, ZipInfo],
    encrypted: set[str],
) -> dict[str, EpubResource]:
    """Map manifest IDs to unique local paths or unavailable external resources.

    Args:
        root: The package document's single manifest element.
        package_path: The normalized package document path.
        members: Validated local files keyed by normalized member path.
        encrypted: The paths declared encrypted or obfuscated by the container.

    Returns:
        Declared resources keyed by their original manifest IDs.

    Raises:
        ContentError: If resource declarations are ambiguous, unsafe or over limits.
    """
    items = root.findall(f"{_OPF}item")
    if len(items) > _MAX_RESOURCES:
        raise ContentError("media_limit_exceeded")
    resources: dict[str, EpubResource] = {}
    paths: set[str] = set()
    for item in items:
        identifier = item.get("id", "")
        media_type = item.get("media-type", "").strip().lower()
        if (
            not identifier
            or len(identifier) > 4096
            or any(char.isspace() for char in identifier)
            or identifier in resources
            or not media_type
        ):
            raise ContentError("invalid_epub")
        path = resolve_epub_reference(package_path, item.get("href", ""))
        if path is not None:
            if path in paths:
                raise ContentError("invalid_epub")
            paths.add(path)
        resources[identifier] = EpubResource(
            id=identifier,
            path=path,
            media_type=media_type,
            properties=frozenset(item.get("properties", "").split()),
            member=members.get(path) if path is not None else None,
            encrypted=path in encrypted,
        )
    return resources


def _parse_spine(
    root: etree._Element, resources: dict[str, EpubResource]
) -> tuple[EpubResource, ...]:
    """Keep declared reading order, including auxiliary XHTML documents.

    Args:
        root: The package document's single spine element.
        resources: Resources keyed by manifest ID.

    Returns:
        Unique local XHTML resources in spine order, without reading their bodies.

    Raises:
        ContentError: If the spine is empty, malformed, unsupported or over limits.
    """
    items = root.findall(f"{_OPF}itemref")
    if len(items) > _MAX_SPINE_ITEMS:
        raise ContentError("media_limit_exceeded")
    spine: list[EpubResource] = []
    seen: set[str] = set()
    has_linear = False
    for item in items:
        resource = resources.get(item.get("idref", ""))
        linear = item.get("linear", "yes")
        if resource is None or resource.id in seen or linear not in ("yes", "no"):
            raise ContentError("invalid_epub")
        if (
            resource.path is None
            or resource.encrypted
            or resource.media_type != _XHTML_MIME
            or "rendition:layout-pre-paginated" in item.get("properties", "").split()
        ):
            raise ContentError("unsupported_media_format")
        if resource.member is None:
            raise ContentError("invalid_epub")
        if resource.member.file_size > DOCUMENT_BYTES:
            raise ContentError("media_limit_exceeded")
        has_linear |= linear == "yes"
        seen.add(resource.id)
        spine.append(resource)
    if not has_linear:
        raise ContentError("invalid_epub")
    return tuple(spine)


def load_epub_package(archive: ZipFile) -> EpubPackage:
    """Read EPUB 2 or 3 package structure from a validated, stable archive.

    Call inside open_archive so source changes and ZIP failures retain its error
    handling. Run this synchronous operation in a worker. It reads package
    descriptors only, without validating XHTML bodies, publishing content caches
    or persisting descriptive metadata.

    Args:
        archive: The archive yielded by open_archive after caller access checks.

    Returns:
        The first declared rendition's resource manifest and reading order.

    Raises:
        ContentError: If required structure is missing, malformed, unsupported
            or over limits; open_archive also checks source stability on exit.
    """
    members = {
        normalize_member_path(member.filename): member
        for member in archive.infolist()
        if not member.is_dir()
    }
    mime = members.get("mimetype")
    if (
        mime is None
        or mime.filename != "mimetype"
        or mime.header_offset != 0
        or mime.compress_type != ZIP_STORED
        or mime.file_size != len(_MIMETYPE)
        or read_member(archive, mime, len(_MIMETYPE)) != _MIMETYPE
    ):
        raise ContentError("invalid_epub")
    container = _read_xml(
        archive, members.get("META-INF/container.xml"), f"{_CONTAINER}container"
    )
    groups = container.findall(f"{_CONTAINER}rootfiles")
    if container.get("version") != "1.0" or len(groups) != 1:
        raise ContentError("invalid_epub")
    rootfiles = groups[0].findall(f"{_CONTAINER}rootfile")
    if not rootfiles or rootfiles[0].get("media-type") != _PACKAGE_MIME:
        raise ContentError("invalid_epub")
    package_path = resolve_epub_reference("", rootfiles[0].get("full-path", ""))
    if package_path is None:
        raise ContentError("invalid_epub")
    encrypted = _encrypted_paths(archive, members)
    if encrypted & {
        "mimetype",
        "META-INF/container.xml",
        "META-INF/encryption.xml",
        package_path,
    }:
        raise ContentError("unsupported_media_format")
    root = _read_xml(archive, members.get(package_path), f"{_OPF}package")
    if root.get("version") not in ("2.0", "3.0"):
        raise ContentError("unsupported_media_format")
    manifests, spines = root.findall(f"{_OPF}manifest"), root.findall(f"{_OPF}spine")
    if len(manifests) != 1 or len(spines) != 1:
        raise ContentError("invalid_epub")
    metadata = root.findall(f"{_OPF}metadata/{_OPF}meta")
    if any(
        (
            meta.get("property") == "rendition:layout"
            and (meta.text or "").strip() == "pre-paginated"
        )
        or (meta.get("name") == "fixed-layout" and meta.get("content") == "true")
        for meta in metadata
    ):
        raise ContentError("unsupported_media_format")
    resources = _parse_manifest(manifests[0], package_path, members, encrypted)
    spine = _parse_spine(spines[0], resources)
    navigation = [item for item in resources.values() if "nav" in item.properties]
    covers = [item for item in resources.values() if "cover-image" in item.properties]
    if len(navigation) > 1 or len(covers) > 1:
        raise ContentError("invalid_epub")
    legacy_cover = next(
        (meta.get("content", "") for meta in metadata if meta.get("name") == "cover"),
        "",
    )
    return EpubPackage(
        path=package_path,
        resources=resources,
        spine=spine,
        navigation=navigation[0]
        if navigation
        else resources.get(spines[0].get("toc", "")),
        cover=covers[0] if covers else resources.get(legacy_cover),
    )
