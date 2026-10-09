"""Validate candidates and resolve targets for reading metadata nodes."""

from typing import Literal
from uuid import UUID

from app.core.flow.context import MANUAL_KEY, Context
from app.core.media.common import ContentError
from app.core.media.metadata import METADATA_BYTES, ReadingMetadata, render_metadata
from app.models.media import LibType, MediaItem
from app.utils import json


def parse_candidates(response: str, format: Literal["opf", "comicinfo"]) -> list[dict]:
    """Validate bounded JSONC candidates using the same rules as metadata saving.

    Args:
        response: The rendered response containing up to 100 candidate objects.
        format: The target metadata format for candidate round-trip validation.

    Returns:
        Normalized candidates, including an empty list for no search results.

    Raises:
        ContentError: If the response is oversized, malformed or unrepresentable.
    """
    if len(response.encode()) > METADATA_BYTES:
        raise ContentError("media_limit_exceeded")
    candidates = json.try_loads(response, with_comments=True)
    if not isinstance(candidates, list):
        raise ContentError("invalid_metadata")
    if len(candidates) > 100:
        raise ContentError("media_limit_exceeded")
    result = []
    for candidate in candidates:
        try:
            metadata = ReadingMetadata.model_validate_json(json.dumps(candidate))
        except (TypeError, ValueError) as error:
            raise ContentError("invalid_metadata") from error
        render_metadata(metadata, format, identifier=UUID(int=0))
        result.append(metadata.model_dump(mode="json", exclude_defaults=True))
    return result


async def reading_item(context: Context, lib_type: LibType) -> int | None:
    """Resolve the automatic target or validate a manual preview's library type.

    Args:
        context: The workflow context with server-selected source boot parameters.
        lib_type: The library type supported by the calling metadata node.

    Returns:
        The current item ID, or None for a manual preview without publication.

    Raises:
        ContentError: If the source is missing, invalid or belongs to another type.
    """
    params = context.bootparams
    if params.get(MANUAL_KEY) is True:
        if params.get("lib_type") != lib_type:
            raise ContentError("unsupported_media_format")
        return None
    id = params.get("item_id")
    if not isinstance(id, int) or isinstance(id, bool) or id <= 0:
        raise ContentError("invalid_metadata")
    item = await MediaItem.get_or_none(id=id).select_related("lib")
    if item is None:
        raise ContentError("not_found")
    if item.lib.lib_type != lib_type:
        raise ContentError("unsupported_media_format")
    return item.id
