from pathlib import Path

from sanic.log import logger

from app.core.flow.context import MANUAL_KEY, Context
from app.core.flow.fields.code import CodeField
from app.core.flow.handles import OutputHandle
from app.core.flow.nodes.base import CancellationSignal, Node, start_node
from app.models.flow import GraphCategory


@start_node(order=1, icon="boxMultipleSearch", categories=(GraphCategory.INGEST,))
class IngestStartNode(Node):
    """Start media metadata ingestion or candidate search.

    Input: server/caller media fields shown in `example`, available directly in
    context. Output: unchanged context. Automatic runs stop if the target NFO
    already exists; manual runs skip that check. Finish with the appropriate
    movie, tv_show, episode, novel or comic metadata node."""

    example = CodeField(
        "request_example",
        language="jsonc",
        collapse=False,
        readonly=True,
        template="req/ingest.jsonc",
    )

    class Handles:
        output = OutputHandle()

    @classmethod
    async def execute(cls, *, context: Context, **kwargs):
        """Skip NFO generation when the source already has an NFO file.

        Args:
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.

        Raises:
            CancellationSignal: If the flow should end without an error.
        """
        # manual lookup can refresh metadata even when an NFO exists
        bootparams = context.bootparams
        if bootparams.get(MANUAL_KEY):
            return

        # check if NFO file already exists
        nfo_path = bootparams.get("nfo_path")
        if nfo_path and Path(nfo_path).exists():
            logger.info("NFO file already exists, skipping generation: %s", nfo_path)
            raise CancellationSignal
