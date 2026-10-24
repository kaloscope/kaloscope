"""Read bundled workflow authoring references without accessing runtime data."""

from pathlib import Path
from typing import Literal

from app.models.mcp import WorkflowReference


def get_workflow_reference(topic: Literal["context", "templates"]) -> WorkflowReference:
    """Read a fixed reference shared by the assistant and MCP.

    Args:
        topic: Context passing rules or project-specific template functions.

    Returns:
        Complete English reference without executing templates or workflow nodes.
    """
    filenames = {"context": "context.md", "templates": "templates.md"}
    path = Path(__file__).parents[3] / "static" / "flow" / filenames[topic]
    return WorkflowReference(topic=topic, content=path.read_text(encoding="utf-8"))
