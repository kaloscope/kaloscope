from typing import Any

from app.core.flow.context import Context
from app.core.flow.fields.code import CodeField
from app.core.flow.handles import OutputHandle
from app.core.flow.nodes.base import Node, start_node
from app.core.renderer import render
from app.models.flow import GraphCategory
from app.utils import json


@start_node(order=1, categories=(GraphCategory.SCHEDULE,))
class ScheduleStartNode(Node):
    """Start a schedule workflow; does not create a job.

    Input: `params` holds caller parameters. Render `bootparams` as a JSONC object
    and merge into context: `{"limit":3}` exposes `{{ limit }}` downstream.
    """

    bootparams = CodeField(
        "bootparams",
        language="jinja2",
        darkmode=True,
        default="{{ params|tojson }}",
    )

    class Handles:
        output = OutputHandle()

    @classmethod
    async def execute(cls, *, node_data: dict[str, Any], context: Context, **kwargs):
        """Merge the configured schedule parameters into the context.

        Args:
            node_data: Configured fields and mutable node state.
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.
        """
        bootparams = cls.bootparams.extract(node_data)
        if not bootparams:
            return

        # merge the rendered startup parameters into context
        rendered = render(
            bootparams, {**context._context, "params": context.bootparams}
        )
        if not rendered:
            return
        if isinstance((obj := json.try_loads(rendered, with_comments=True)), dict):
            context.update(obj)
