from app.core.flow.fields import CodeField
from app.core.flow.handles import OutputHandle
from app.core.flow.nodes.base import Node, start_node
from app.models.flow import GraphCategory

BOARD_CONFIG = """
display:
  view_modes:
    - grid
    - table
  cover_ratio: 2/3

calendar:
  week: false
  week_start: 0
""".lstrip()


@start_node(order=2, icon="appsListDetail", categories=(GraphCategory.INDEXER,))
class BoardStartNode(Node):
    """Start an indexer board request.

    Input: caller boot parameters (an empty object `{}` is sufficient when the
    board needs no parameters). `config` controls display/calendar options.
    Output: unchanged context through `output`; finish with `board_end`."""

    config = CodeField(
        "config",
        language="yaml",
        darkmode=True,
        default=BOARD_CONFIG,
    )

    class Handles:
        output = OutputHandle(tag="board")
