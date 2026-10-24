from collections.abc import Iterable, Sized
from typing import Any

from app.core.flow.context import IDX_KEY, LOOP_KEY, Context
from app.core.flow.fields import TextField
from app.core.flow.handles import DefaultHandles, InputHandle, OutputHandle
from app.core.flow.nodes.base import Node, control_node


@control_node(order=2, icon="arrowSyncCircle")
class LoopNode(Node):
    """Repeat a branch over a finite, indexable sequence.

    Input: `expression` (e.g. `{{ items }}`) and `varname` (e.g. `item`). An integer
    becomes range(0, n); a dictionary becomes key/value pairs. Output: `loop_start`
    binds `item` for the body; connect its last node to `loop_continue`. Normal
    `output` runs after exhaustion (immediately for an empty sequence). The index
    is internal node state, not an automatically available context variable."""

    expression = TextField("expression", required=True, maxlength=1024)
    varname = TextField("varname", required=True, maxlength=32)

    class Handles(DefaultHandles):
        loop_start = OutputHandle(
            "bottom",
            maxconn=1,
            style="left: 25%; border-style: dashed;",
            tag=LOOP_KEY,
        )
        loop_continue = InputHandle(
            "bottom",
            maxconn=1,
            style="left: 75%; border-style: dashed;",
            tag=LOOP_KEY,
        )

    @classmethod
    async def execute(
        cls,
        *,
        node_id: str,
        node_data: dict[str, Any],
        context: Context,
        input_handle: InputHandle,
        **kwargs,
    ) -> OutputHandle | None:
        # extract the loop variable
        """Start or advance the loop and bind its current value.

        Args:
            node_id: Current node ID.
            node_data: Configured fields and mutable node state.
            context: Shared variables and storage for this flow run.
            input_handle: Handle receiving this execution.
            **kwargs: Unused execution arguments supplied by the flow engine.

        Returns:
            Loop start or exit handle, or `None` for an unrelated input.

        Raises:
            ValueError: If the loop input has no defined length.
        """
        var = cls.expression.extract(node_data, context=context, raw=True)
        varname = cls.varname.extract(node_data)
        if isinstance(var, int):
            var = list(range(var))
        elif isinstance(var, dict):
            var = list(var.items())

        # start the loop
        handles = cls.Handles
        if handles.input == input_handle:
            if not (isinstance(var, Iterable) and isinstance(var, Sized)):
                raise ValueError("loop variable must be an iterable with length")
            node_data[IDX_KEY] = 0
            if len(var) > 0:
                node_data[LOOP_KEY] = {varname: var[0]}
                return handles.loop_start.with_loop(node_id)
            else:
                # skip the loop
                return handles.output

        # continue the loop
        if handles.loop_continue == input_handle:
            node_data[IDX_KEY] += 1
            if node_data[IDX_KEY] <= len(var) - 1:
                node_data[LOOP_KEY] = {varname: var[node_data[IDX_KEY]]}
                return handles.loop_start.with_loop(node_id)
            else:
                # end the loop
                return handles.output
