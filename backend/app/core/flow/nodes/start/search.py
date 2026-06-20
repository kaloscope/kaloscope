from app.core.flow.fields import CodeField
from app.core.flow.handles import OutputHandle
from app.core.flow.nodes.base import Node, start_node
from app.models.flow import GraphCategory

SEARCH_CONFIG = """
display:
  page_size: 20
  view_modes:
    - table
    - grid
  cover_ratio: 16/9

keyword:
  global: true
  required: false

filters:
  key:
    type: # text | radio | checkbox | select | datetime
    label:
""".lstrip()


@start_node(order=3, icon="boxMultipleSearch", categories=(GraphCategory.INDEXER,))
class SearchStartNode(Node):
    example = CodeField(
        "request_example",
        language="jsonc",
        collapse=True,
        readonly=True,
        template="req/search.jsonc",
    )
    config = CodeField(
        "config",
        language="yaml",
        darkmode=True,
        default=SEARCH_CONFIG,
    )

    class Handles:
        output = OutputHandle(tag="search")
