from typing import Any

from app.core.flow.fields import ToggleField
from app.core.flow.handles import InputHandle
from app.core.flow.nodes.base import CancellationSignal, Node, end_node
from app.models.flow import GraphCategory


@end_node(order=1, categories=(GraphCategory.SCHEDULE,))
class ScheduleEndNode(Node):
    """End this branch without changing context or returning a value.

    `force_end=true` stops the entire run.
    """

    force_end = ToggleField("force_end", tooltip="force_end", required=True)

    class Handles:
        input = InputHandle()

    @classmethod
    async def execute(cls, *, node_data: dict[str, Any], **kwargs):
        # force end the flow if required
        """Stop the flow when the end node requests cancellation.

        Args:
            node_data: Configured fields and mutable node state.
            **kwargs: Unused execution arguments supplied by the flow engine.

        Raises:
            CancellationSignal: If the flow should end without an error.
        """
        if cls.force_end.extract(node_data):
            raise CancellationSignal
