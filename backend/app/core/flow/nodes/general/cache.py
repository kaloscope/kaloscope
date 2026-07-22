import time
from typing import Any

from app.core.flow.context import Context
from app.core.flow.fields import CodeField, NumberField, TextField
from app.core.flow.nodes.base import Node, general_node
from app.models.flow import FlowVariable


@general_node(order=4, icon="save", width="32rem")
class CacheNode(Node):
    key = TextField(required=True, maxlength=64, span=70)
    ttl = NumberField(tooltip="cache_ttl", min=0, span=30)
    value = CodeField(
        required=True, language="jinja2", width=None, darkmode=True, default="{}"
    )

    @classmethod
    async def execute(
        cls, *, graph_id: int, node_data: dict[str, Any], context: Context, **kwargs
    ):
        key = cls.key.extract(node_data, context=context)
        ttl = int(cls.ttl.extract(node_data))
        if not key or ttl < 0:
            return

        value = cls.value.extract(node_data, context=context)
        expires = int(time.time() + ttl) if ttl > 0 else None

        await FlowVariable.update_or_create(
            graph_id=graph_id,
            key=key,
            defaults={"value": value, "expires": expires},
        )
