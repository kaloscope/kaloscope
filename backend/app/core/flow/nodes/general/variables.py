from typing import Any

from app.core.flow.context import Context
from app.core.flow.fields.code import CodeField
from app.core.flow.nodes.base import Node, general_node
from app.utils import json


@general_node(order=3, icon="bracesVariable")
class VariablesNode(Node):
    """Render a JSONC object and merge its keys into context.

    Input: `variables`, e.g. `{"query": {{ keyword|tojson }}, "page": 1}`.
    With `keyword="cats"`, output adds `query="cats"` and `page=1`. Existing nested
    objects are deep-merged; lists append new elements. Other keys remain.
    Non-object output writes nothing. This does not persist variables between runs."""

    variables = CodeField(
        "variables",
        language="jinja2",
        darkmode=True,
        default="{}",
    )

    @classmethod
    async def execute(cls, *, node_data: dict[str, Any], context: Context, **kwargs):
        """Merge rendered JSON variables into the flow context.

        Args:
            node_data: Configured fields and mutable node state.
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.
        """
        variables = cls.variables.extract(node_data, context=context)
        if not variables:
            return

        # merge the rendered variables into context
        if isinstance((obj := json.try_loads(variables, with_comments=True)), dict):
            context.update(obj)
