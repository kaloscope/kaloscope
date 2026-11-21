"""Edit a single title in an indexed NFO while preserving its other XML fields."""

import hashlib
import os
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile

from lxml import etree

from app.core.media.common import ContentError
from app.core.media.metadata import METADATA_BYTES
from app.models.media import LibType


def nfo_title(
    directory: str,
    filename: str,
    kind: LibType,
    *,
    title: str | None = None,
    revision: str | None = None,
) -> dict[str, str | None]:
    """Read a bounded NFO or replace its reviewed title inside the source library.

    Args:
        directory: Canonical library root owned by the item.
        filename: NFO recorded for this item.
        kind: Video library type defining the accepted document roots.
        title: New title, or `None` for a read-only preview.
        revision: Fingerprint of the exact document the user reviewed.

    Returns:
        Previous title and fingerprint, including the NFO's location in the library.

    Raises:
        ContentError: If the source, ownership, document or revision is invalid.
        OSError: If atomic publication fails.
    """
    root = Path(directory).resolve()
    path = Path(filename)
    if (
        path.is_symlink()
        or not path.resolve().is_relative_to(root)
        or path.suffix.lower() != ".nfo"
    ):
        raise ContentError("media_source_unavailable")
    if not path.is_file():
        raise ContentError("media_source_unavailable")
    with path.open("rb") as source:
        data = source.read(METADATA_BYTES + 1)
        mode = os.fstat(source.fileno()).st_mode
    if len(data) > METADATA_BYTES:
        raise ContentError("media_limit_exceeded")
    try:
        node = etree.fromstring(
            data, etree.XMLParser(resolve_entities=False, no_network=True)
        )
    except etree.XMLSyntaxError as exc:
        raise ContentError("invalid_metadata") from exc
    tree = node.getroottree()
    roots = {"movie"} if kind == LibType.MOVIE else {"tvshow", "episodedetails"}
    if tree.docinfo.doctype or node.tag not in roots:
        raise ContentError("invalid_metadata")
    digest = hashlib.sha256(
        str(path.resolve().relative_to(root)).encode() + b"\0" + data
    ).hexdigest()
    field = node.find("title")
    preview = {
        "before_title": field.text if field is not None else None,
        "revision": digest,
    }
    if title is None:
        return preview
    if digest != revision:
        raise ContentError("content_changed")
    if field is None:
        field = etree.SubElement(node, "title")
    field.text = title
    changed = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
    with NamedTemporaryFile(
        dir=path.parent, prefix=".nfo-", suffix=".tmp", delete=False
    ) as temporary:
        try:
            temporary.write(changed)
            temporary.flush()
            os.fchmod(temporary.fileno(), stat.S_IMODE(mode) & 0o777)
            temporary.close()
            os.replace(temporary.name, path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    return preview
