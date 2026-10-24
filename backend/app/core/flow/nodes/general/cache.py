import time
from typing import Any

from app.core.flow.context import Context
from app.core.flow.fields import CodeField, NumberField, TextField
from app.core.flow.nodes.base import Node, general_node
from app.models.flow import FlowVariable


@general_node(order=4, icon="save", width="32rem")
class CacheNode(Node):
    """Persist a workflow-local variable for later runs.

    Input: rendered `key` and `value`; `ttl` is seconds, zero/null means no expiry.
    Example: key `token`, value `{{ session_token }}`, ttl `3600` stores rendered
    text for one hour. Output: no current-context update; later runs load `token`.
    JSON-looking values remain strings; parse them explicitly when reading."""

    key = TextField(required=True, maxlength=64, span=70)
    ttl = NumberField(tooltip="cache_ttl", min=0, span=30)
    value = CodeField(
        required=True, language="jinja2", width=None, darkmode=True, default="{}"
    )

    @classmethod
    async def execute(
        cls, *, graph_id: int, node_data: dict[str, Any], context: Context, **kwargs
    ):
        """Cache a rendered variable with an optional expiry.

        Args:
            graph_id: Flow graph ID.
            node_data: Configured fields and mutable node state; an empty `ttl`
                keeps the cached value indefinitely.
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.
        """
        key = cls.key.extract(node_data, context=context)
        configured_ttl = cls.ttl.extract(node_data)
        ttl = int(configured_ttl) if configured_ttl is not None else 0
        if not key or ttl < 0:
            return

        value = cls.value.extract(node_data, context=context)
        expires = int(time.time() + ttl) if ttl > 0 else None

        await FlowVariable.update_or_create(
            graph_id=graph_id,
            key=key,
            defaults={"value": value, "expires": expires},
        )
