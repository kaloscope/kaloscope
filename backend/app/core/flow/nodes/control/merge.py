import asyncio
import contextlib
from typing import Any

from app.core.flow.edge import Source
from app.core.flow.fields import NumberField
from app.core.flow.handles import STOP, InputHandle, OutputHandle
from app.core.flow.nodes.base import Node, control_node

_DONE = "$done"
_SIGNAL = "$signal"
_RECEIVED = "$received"


@control_node(order=3, icon="circleDashedPlus")
class MergeNode(Node):
    """Synchronize incoming branches before continuing once.

    Input: branch contexts; wait for `min(maximum, incoming source count)` sources.
    A positive timeout permits continuation with available sources; zero disables
    the timer. Output: ordinary merged context through `output`, not an array of
    branch results. Example: maximum 2 joins two incoming branches."""

    maximum = NumberField(
        tooltip="merge_maximum", required=True, min=1, max=20, default=1
    )
    timeout = NumberField(
        "timeout", tooltip="merge_timeout", required=True, min=0, default=30
    )

    class Handles:
        input = InputHandle(maxconn=20)
        output = OutputHandle()

    @classmethod
    async def execute(
        cls,
        *,
        node_data: dict[str, Any],
        source: Source | None = None,
        sources: list[Source] | None = None,
        **kwargs,
    ) -> OutputHandle | None:
        """Emit once enough inputs arrive or the configured wait expires.

        Args:
            node_data: Configured fields and mutable node state.
            source: Incoming source, or `None` when unavailable.
            sources: Connected sources; `None` leaves the configured input limit
                unchanged.
            **kwargs: Unused execution arguments supplied by the flow engine.

        Returns:
            Output handle for the first completed merge; `STOP` for other inputs.

        Raises:
            ValueError: If either required numeric field is empty.
        """
        # discard this input if already done
        if node_data.get(_DONE):
            return STOP

        # determine the maximum number of inputs to wait for
        maximum = cls.maximum.extract(node_data)
        timeout = cls.timeout.extract(node_data)
        if maximum is None or timeout is None:
            raise ValueError("Merge input limit and timeout must not be empty")
        if sources:
            maximum = min(maximum, len(sources))

        # accumulate received source
        received: list[Source] = node_data.setdefault(_RECEIVED, [])
        if source is not None and source not in received:
            received.append(source)

        # output if enough inputs have been received
        handles = cls.Handles
        if len(received) >= maximum:
            _done(node_data)
            return handles.output

        # wait for remaining inputs or until timeout
        if timeout > 0:
            signal: asyncio.Event = node_data.setdefault(_SIGNAL, asyncio.Event())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(signal.wait(), timeout=timeout)

            # another coroutine may have already output while we were waiting
            if node_data.get(_DONE):
                return STOP

            # output with whatever inputs arrived so far after timeout
            _done(node_data)
            return handles.output

        # discard this input if no timeout configured
        return STOP


def _done(node_data: dict):
    """Mark the merge node as done and signal any waiting coroutines.

    Args:
        node_data: Node data.
    """
    node_data[_DONE] = True
    node_data.pop(_RECEIVED, None)
    signal: asyncio.Event | None = node_data.pop(_SIGNAL, None)
    if signal is not None:
        signal.set()
