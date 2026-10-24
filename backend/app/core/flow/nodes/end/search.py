from typing import Any

from app.core.flow.context import RETVAL_KEY, Context
from app.core.flow.fields import CodeField, ToggleField
from app.core.flow.handles import InputHandle
from app.core.flow.nodes.base import CancellationSignal, Node, end_node
from app.models.flow import GraphCategory
from app.utils import json


@end_node(order=3, icon="boxMultipleSearchFilled", categories=(GraphCategory.INDEXER,))
class SearchEndNode(Node):
    """Return a paginated indexer search result.

    Input: render `response` to the complete JSONC object shown in `example`.
    Output: parsed value in `$retval`, consumed by the search UI. `total` counts
    all matches, while `items` holds this page. `force_end=true` ends the whole
    run; otherwise only this branch ends."""

    example = CodeField(
        "response_example",
        language="jsonc",
        collapse=True,
        readonly=True,
        template="resp/search.jsonc",
    )
    response = CodeField(
        "response",
        required=True,
        language="jinja2",
        darkmode=True,
        default=json.pretty({"total": 0, "items": [{"id": ""}]}),
    )
    force_end = ToggleField("force_end", tooltip="force_end", required=True)

    class Handles:
        input = InputHandle(tag="search")

    @classmethod
    async def execute(cls, *, node_data: dict[str, Any], context: Context, **kwargs):
        # extract the response
        """Store the rendered search results in the flow context.

        Args:
            node_data: Configured fields and mutable node state.
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.

        Raises:
            CancellationSignal: If the flow should end without an error.
        """
        context[RETVAL_KEY] = json.try_loads(
            cls.response.extract(node_data, context=context), with_comments=True
        )
        # force end the flow if required
        if cls.force_end.extract(node_data):
            raise CancellationSignal
