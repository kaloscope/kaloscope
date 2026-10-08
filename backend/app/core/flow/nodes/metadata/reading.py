"""Return reading metadata candidates and publish automatic workflow results."""

from asyncio import to_thread
from typing import Any, Literal
from uuid import UUID

from app.core.flow.context import MANUAL_KEY, RETVAL_KEY, Context
from app.core.flow.fields import CodeField, ToggleField
from app.core.flow.handles import InputHandle
from app.core.flow.nodes.base import CancellationSignal, Node, end_node
from app.core.media.common import ContentError
from app.core.media.metadata import METADATA_BYTES, ReadingMetadata, render_metadata
from app.models.flow import GraphCategory
from app.models.media import LibType, MediaItem
from app.utils import json


def _parse_candidates(response: str, format: Literal["opf", "comicinfo"]) -> list[dict]:
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


@end_node(order=4, icon="bookText", categories=(GraphCategory.INGEST,))
class ReadingNode(Node):
    example = CodeField(
        "response_example",
        language="jsonc",
        collapse=True,
        readonly=True,
        template="resp/reading.jsonc",
    )
    response = CodeField(
        "response",
        required=True,
        language="jinja2",
        darkmode=True,
        default=json.pretty([{"title": ""}]),
    )
    force_end = ToggleField("force_end", tooltip="force_end", required=True)

    class Handles:
        input = InputHandle()

    @classmethod
    async def execute(cls, *, node_data: dict[str, Any], context: Context, **kwargs):
        """Preview candidates or save the first automatic result for a reading item.

        Args:
            node_data: The response template and optional force-end setting.
            context: The workflow context with server-selected source boot parameters.
            **kwargs: Additional execution arguments supplied by the flow engine.

        Raises:
            ContentError: If the source, candidates or publication is invalid.
            CancellationSignal: After success when force-end is enabled.
        """
        from app.services.media import MediaItemService

        params = context.bootparams
        manual = params.get(MANUAL_KEY) is True
        item = None
        lib_type = params.get("lib_type")
        if not manual:
            id = params.get("item_id")
            if not isinstance(id, int) or isinstance(id, bool) or id <= 0:
                raise ContentError("invalid_metadata")
            item = await MediaItem.get_or_none(id=id).select_related("lib")
            if item is None:
                raise ContentError("not_found")
            lib_type = item.lib.lib_type
        if lib_type not in (LibType.NOVEL, LibType.COMIC):
            raise ContentError("unsupported_media_format")
        format = "opf" if lib_type == LibType.NOVEL else "comicinfo"
        candidates = await to_thread(
            _parse_candidates, cls.response.extract(node_data, context=context), format
        )
        if item is not None and candidates:
            await MediaItemService.save_metadata(item.id, candidates[0])
        context[RETVAL_KEY] = candidates
        if cls.force_end.extract(node_data):
            raise CancellationSignal
