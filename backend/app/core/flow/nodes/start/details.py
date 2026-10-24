from app.core.flow.fields import CodeField
from app.core.flow.handles import OutputHandle
from app.core.flow.nodes.base import Node, start_node
from app.models.flow import GraphCategory

DETAILS_CONFIG = """
specific:
  media_type: video # video | audio | image | text
  video_type: # mp4 | flv | hls | dash
""".lstrip()


@start_node(order=4, icon="contentView", categories=(GraphCategory.INDEXER,))
class DetailsStartNode(Node):
    """Start an indexer resource or chapter lookup.

    Input: resource `id` and optional `chapter_id`, as shown in `example`.
    `config.specific` supplies display defaults. Output: unchanged context through
    `output`; finish with `details_end`. IDs belong to the remote indexer."""

    example = CodeField(
        "request_example",
        language="jsonc",
        collapse=True,
        readonly=True,
        template="req/details.jsonc",
    )
    config = CodeField(
        "config",
        language="yaml",
        darkmode=True,
        default=DETAILS_CONFIG,
    )

    class Handles:
        output = OutputHandle(tag="details")
