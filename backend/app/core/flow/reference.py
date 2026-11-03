"""Read bundled workflow authoring references without accessing runtime data."""

from pathlib import Path
from typing import Literal

from app.models.mcp import WorkflowReference
from app.services.flow import FlowGraphService
from app.utils.json import dumps, loads

# recipes whose text names every node, field and handle they use
SELF_CONTAINED_RECIPES = frozenset({"html_indexer"})


def get_workflow_reference(
    topic: Literal[
        "context",
        "templates",
        "json_indexer",
        "html_indexer",
        "indexer_auth",
        "movie_scraper",
        "tv_scraper",
    ],
    include_nodes: bool = False,
) -> WorkflowReference:
    """Read a fixed reference shared by the assistant and MCP.

    Args:
        topic: Context, template functions, or a supported authoring recipe.
        include_nodes: Bundle the recipe's registered editable node schemas and
            shared context rules. General references have no node bundle.

    Returns:
        Complete English reference without executing templates or workflow nodes.
    """
    filenames = {
        "context": "context.md",
        "templates": "templates.md",
        "json_indexer": "json_indexer.md",
        "html_indexer": "html_indexer.md",
        "indexer_auth": "indexer_auth.md",
        "movie_scraper": "movie_scraper.md",
        "tv_scraper": "tv_scraper.md",
    }
    path = Path(__file__).parents[3] / "static" / "flow" / filenames[topic]
    result = WorkflowReference(topic=topic, content=path.read_text(encoding="utf-8"))
    recipes = {
        "json_indexer": (
            "search_start",
            "http",
            "search_end",
            "details_start",
            "details_end",
        ),
        "html_indexer": ("search_start", "http", "loop", "search_end"),
        "indexer_auth": ("auth_start", "http", "auth_end"),
        "movie_scraper": ("ingest_start", "http", "movie"),
        "tv_scraper": ("ingest_start", "condition", "http", "tv_show", "episode"),
    }
    if include_nodes and topic in recipes:
        schemas = {}
        for node_type in recipes[topic]:
            for group, nodes in FlowGraphService.get_node_schemas(
                node_type=node_type, include_examples=False
            ).items():
                schemas.setdefault(group, []).extend(nodes)
        result.nodes = compact_node_schemas(schemas)
        result.context = get_workflow_reference("context").content
    return result


def compact_node_schemas(schemas: dict) -> dict:
    """Omit editor decoration while retaining executable fields and handles.

    Args:
        schemas: JSON-compatible node schemas from the existing registry.

    Returns:
        An independent compact copy with defaults, constraints and widths intact.
    """
    result = loads(dumps(schemas))
    for nodes in result.values():
        for node in nodes:
            node.pop("icon")
            node.pop("order")
            node.pop("name")
            node.pop("group")
            if node["width"] is None:
                node.pop("width")
            for handle in node["handles"]:
                handle.pop("style", None)
                handle.pop("position", None)
                if handle.get("tag") is None:
                    handle.pop("tag", None)
            for field in node["fields"]:
                for key in (
                    "span",
                    "tabsize",
                    "lineLength",
                    "collapse",
                    "darkmode",
                    "copier",
                    "resetter",
                    "formatter",
                    "label",
                    "tooltip",
                ):
                    field.pop(key, None)
                for key, value in list(field.items()):
                    if value is None and key != "default":
                        field.pop(key)
    return result
