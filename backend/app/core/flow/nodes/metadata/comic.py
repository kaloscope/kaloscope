"""Return comic metadata candidates and publish automatic ComicInfo results."""

from asyncio import to_thread
from typing import Any

from app.core.flow.context import RETVAL_KEY, Context
from app.core.flow.fields import CodeField, ToggleField
from app.core.flow.handles import InputHandle
from app.core.flow.nodes.base import CancellationSignal, Node, end_node
from app.core.flow.nodes.metadata.common import parse_candidates, reading_item
from app.models.flow import GraphCategory
from app.models.media import LibType
from app.utils import json


@end_node(order=5, icon="imageMultiple", categories=(GraphCategory.INGEST,))
class ComicNode(Node):
    """Return validated comic metadata candidates and optionally save metadata.

    Input: render `response` to the candidate array in `example` (at most 100).
    Output: `$retval` holds validated candidates. Automatic ingestion uses the
    server's `item_id` in a matching library and saves the first candidate;
    manual searches only return candidates and require matching `lib_type`.
    `force_end=true` ends the entire run. Execution may update metadata/files."""

    example = CodeField(
        "response_example",
        language="jsonc",
        collapse=True,
        readonly=True,
        template="resp/comic.jsonc",
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
        """Preview comic candidates or save the first automatic result as ComicInfo.

        Args:
            node_data: Response template and optional force-end setting.
            context: Workflow context with server-selected source boot parameters.
            **kwargs: Additional execution arguments supplied by the flow engine.

        Raises:
            ContentError: If the source, candidates or publication is invalid.
            CancellationSignal: After success when force-end is enabled.
        """
        from app.services.media import MediaItemService

        item_id = await reading_item(context, LibType.COMIC)
        candidates = await to_thread(
            parse_candidates,
            cls.response.extract(node_data, context=context),
            "comicinfo",
        )
        if item_id is not None and candidates:
            await MediaItemService.save_metadata(item_id, candidates[0])
        context[RETVAL_KEY] = candidates
        if cls.force_end.extract(node_data):
            raise CancellationSignal
