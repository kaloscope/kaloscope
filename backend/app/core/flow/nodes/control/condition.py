from typing import Any

from lxml import etree

from app.core.flow.context import Context
from app.core.flow.fields import TextField
from app.core.flow.handles import InputHandle, OutputHandle
from app.core.flow.nodes.base import Node, control_node


@control_node(order=1, icon="arrowCircleDownSplit")
class ConditionNode(Node):
    expression = TextField("expression", required=True, maxlength=1024)

    class Handles:
        input = InputHandle()
        is_true = OutputHandle(style="top: 35%;")
        is_false = OutputHandle(style="top: 65%; border-style: dotted;")

    @classmethod
    async def execute(
        cls, *, node_data: dict[str, Any], context: Context, **kwargs
    ) -> OutputHandle | None:
        var = cls.expression.extract(node_data, context=context, raw=True)
        # an XPath element indicates a match even when it has no children
        if isinstance(var, etree._Element):
            var = True
        if var and str(var).lower() not in ("false", "0", "none", "null"):
            return cls.Handles.is_true
        else:
            return cls.Handles.is_false
