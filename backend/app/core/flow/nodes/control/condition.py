from typing import Any

from lxml import etree

from app.core.flow.context import Context
from app.core.flow.fields import TextField
from app.core.flow.handles import InputHandle, OutputHandle
from app.core.flow.nodes.base import Node, control_node


def condition_value(var: Any) -> bool:
    """Interpret a rendered condition the way branch selection does.

    Args:
        var: Native value rendered from the condition expression.

    Returns:
        Whether the `is_true` branch is taken.
    """
    # an XPath element indicates a match even when it has no children
    if isinstance(var, etree._Element):
        return True
    return bool(var) and str(var).lower() not in ("false", "0", "none", "null")


@control_node(order=1, icon="arrowCircleDownSplit")
class ConditionNode(Node):
    """Choose a branch using a rendered expression.

    Input: `expression`, e.g. `{{ items|length > 0 }}`. Native false/empty values
    and text `false`, `0`, `none`, `null` are false; an XML/HTML element is true.
    Output: unchanged context via `is_true` or `is_false`; no new variable."""

    expression = TextField("expression", required=True, maxlength=1024)

    class Handles:
        input = InputHandle()
        is_true = OutputHandle(style="top: 35%;")
        is_false = OutputHandle(style="top: 65%; border-style: dotted;")

    @classmethod
    async def execute(
        cls, *, node_data: dict[str, Any], context: Context, **kwargs
    ) -> OutputHandle | None:
        """Choose the output handle from the rendered condition.

        Args:
            node_data: Configured fields and mutable node state.
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.

        Returns:
            True or false output handle.
        """
        var = cls.expression.extract(node_data, context=context, raw=True)
        if condition_value(var):
            return cls.Handles.is_true
        else:
            return cls.Handles.is_false
