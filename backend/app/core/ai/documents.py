"""Describe bounded graph edits and preserve their immutable source documents."""

from copy import deepcopy
from typing import Annotated, Any, Literal

from pydantic import Field

from app.models.assistant import ContextModel, ResourceID
from app.models.flow import GraphDocument, GraphEdge, GraphNode, GraphPosition
from app.utils.json import JSONType, dumps


class GraphReference(ContextModel):
    """Select an exact saved version or an owned immutable snapshot."""

    workflow_id: ResourceID
    draft_version: int = Field(ge=0, strict=True)
    snapshot_id: ResourceID | None = None


class SetField(ContextModel):
    """Replace one authored field without regenerating the rest of its node."""

    op: Literal["set_field"]
    node_id: str = Field(min_length=1, max_length=64)
    field_id: str = Field(min_length=1, max_length=128, pattern=r"^[^$].*$")
    value: JSONType


class RemoveField(ContextModel):
    """Remove one optional authored field from an existing node."""

    op: Literal["remove_field"]
    node_id: str = Field(min_length=1, max_length=64)
    field_id: str = Field(min_length=1, max_length=128, pattern=r"^[^$].*$")


class AddNode(ContextModel):
    """Add a node with an explicit stable identifier."""

    op: Literal["add_node"]
    node: GraphNode = Field(
        description=(
            'type="node"; data.$schema=registered node_type (e.g. "http"). '
            "Authored fields belong inside data. Read list_node_types first."
        )
    )


class RemoveNode(ContextModel):
    """Remove a node after explicitly removing its incident edges."""

    op: Literal["remove_node"]
    node_id: str = Field(min_length=1, max_length=64)


class MoveNode(ContextModel):
    """Change authored layout without replacing business fields."""

    op: Literal["move_node"]
    node_id: str = Field(min_length=1, max_length=64)
    position: GraphPosition


class AddEdge(ContextModel):
    """Add a fully specified connection with a stable identifier."""

    op: Literal["add_edge"]
    edge: GraphEdge


class RemoveEdge(ContextModel):
    """Remove one existing connection by identifier."""

    op: Literal["remove_edge"]
    edge_id: str = Field(min_length=1, max_length=256)


type GraphEdit = Annotated[
    SetField | RemoveField | AddNode | RemoveNode | MoveNode | AddEdge | RemoveEdge,
    Field(discriminator="op"),
]


class GraphProposal(GraphReference):
    """Apply a bounded set of changes against an exact graph reference."""

    changes: list[GraphEdit] = Field(default_factory=list, max_length=100)


def apply_changes(document: dict, changes: list[GraphEdit]) -> GraphDocument:
    """Apply explicit edits to an independent copy of the complete graph.

    Args:
        document: Immutable base containing all original nodes and edges.
        changes: Ordered operations targeting stable identifiers.

    Returns:
        A complete document ready for shared semantic validation.

    Raises:
        ValueError: If identities are ambiguous, missing, or still connected.
    """
    copied = deepcopy(document)
    nodes = {node["id"]: node for node in copied["nodes"]}
    edges = {edge["id"]: edge for edge in copied["edges"]}
    if len(nodes) != len(copied["nodes"]) or len(edges) != len(copied["edges"]):
        raise ValueError("Base contains duplicate identifiers")
    for change in changes:
        if isinstance(change, (SetField, RemoveField, RemoveNode, MoveNode)):
            if change.node_id not in nodes:
                raise ValueError("Unknown node")
            node = nodes[change.node_id]
        match change:
            case SetField():
                node["data"][change.field_id] = deepcopy(change.value)
            case RemoveField():
                if change.field_id not in node["data"]:
                    raise ValueError("Unknown field")
                del node["data"][change.field_id]
            case MoveNode():
                node["position"] = change.position.model_dump()
            case RemoveNode():
                if any(
                    change.node_id in (edge["source"], edge["target"])
                    for edge in edges.values()
                ):
                    raise ValueError("Remove connected edges explicitly first")
                del nodes[change.node_id]
            case AddNode():
                if change.node.id in nodes:
                    raise ValueError("Duplicate node")
                nodes[change.node.id] = change.node.model_dump()
            case AddEdge():
                if change.edge.id in edges:
                    raise ValueError("Duplicate edge")
                edges[change.edge.id] = change.edge.model_dump()
            case RemoveEdge():
                if change.edge_id not in edges:
                    raise ValueError("Unknown edge")
                del edges[change.edge_id]
    return GraphDocument(nodes=list(nodes.values()), edges=list(edges.values()))


def value_summary(value: Any, *, limit: int = 1200) -> Any:
    """Describe large values without confusing a sample with a complete value.

    Args:
        value: JSON-compatible business value.
        limit: Maximum bytes before returning explicit truncation metadata.

    Returns:
        The original small value or a bounded, clearly labeled description.
    """
    encoded = dumps(value)
    if len(encoded) <= limit:
        return value
    result: dict[str, Any] = {"truncated": True, "bytes": len(encoded)}
    if isinstance(value, list):
        result.update(
            type="array",
            item_count=len(value),
            sample=[value_summary(item, limit=200) for item in value[:2]],
        )
    elif isinstance(value, dict):
        result.update(
            type="object",
            field_count=len(value),
            keys=[key[:100] for key in list(value)[:10]],
        )
    elif isinstance(value, str):
        result.update(type="string", characters=len(value), preview=value[:200])
    else:
        result.update(type=type(value).__name__)
    return result


def graph_summary(document: dict) -> dict:
    """Summarize structure and field values while keeping scripts behind references.

    Args:
        document: Complete compressed graph.

    Returns:
        Deterministic counts and a bounded structural overview.
    """
    nodes, edges = document["nodes"], document["edges"]
    result = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": [
            {
                "id": node["id"],
                "data": {
                    key: value_summary(value, limit=300)
                    for key, value in list(node["data"].items())[:20]
                },
            }
            for node in nodes[:30]
        ],
        "edges": edges[:40],
        "omitted_nodes": max(0, len(nodes) - 30),
        "omitted_edges": max(0, len(edges) - 40),
    }
    while len(dumps(result)) > 6000 and (result["nodes"] or result["edges"]):
        if result["edges"]:
            result["edges"].pop()
            result["omitted_edges"] += 1
        else:
            result["nodes"].pop()
            result["omitted_nodes"] += 1
    return result


# trial run facts retained when older tool exchanges are compacted
RUN_FACTS = (
    "workflow_id",
    "run_key",
    "executed",
    "status",
    "replayed",
    "target",
    "effects",
    "rerun",
    "evidence",
    "output_log_id",
)
# plan change identities and outcomes retained; settings stay in the original record
PLAN_CHANGE_FACTS = (
    "valid",
    "issues",
    "action",
    "plan_id",
    "workflow_id",
    "workflow_name",
    "revision",
    "request_id",
    "changes",
    "timing",
    "applied",
    "outcome",
    "replayed",
    "confirmation_required",
    "stale",
    "conflict",
    "pending",
    "failed",
    "message",
)

# task action identities, effects and outcomes retained for confirmation
TASK_ACTION_FACTS = (
    "valid",
    "issues",
    "action",
    "delete_files",
    "task_id",
    "name",
    "state",
    "actions",
    "dispatch",
    "revision",
    "request_id",
    "applied",
    "outcome",
    "replayed",
    "confirmation_required",
    "stale",
    "conflict",
    "pending",
    "failed",
    "message",
)


def tool_facts(result: dict) -> dict:
    """Preserve source identities and deterministic facts in historical tool records.

    Args:
        result: Original persisted tool outcome, including its call identifier.

    Returns:
        A bounded projection with exact counts, paths, readiness, and source IDs.
        Saved workflow reads retain explicit draft provenance, including records
        written before `graph_source` was introduced.
    """
    data = result.get("data", {})
    if not isinstance(data, dict):
        data = {}
    if result.get("name") == "run_workflow":
        # outputs, issues and guidance stay in the original run record
        data = {key: data[key] for key in RUN_FACTS if key in data}
    elif result.get("name") in {
        "review_download_plan_change",
        "apply_download_plan_change",
    }:
        data = {key: data[key] for key in PLAN_CHANGE_FACTS if key in data}
    elif result.get("name") in {
        "review_download_task_action",
        "apply_download_task_action",
    }:
        data = {key: data[key] for key in TASK_ACTION_FACTS if key in data}
    facts = {}
    if result.get("name") == "get_workflow" and isinstance(data.get("graph"), dict):
        facts["graph_source"] = "saved_draft"
    for key, value in data.items():
        if key == "source_facts":
            # history reads already project and flatten the original tool facts
            facts[key] = value
        elif key == "runs":
            facts[key] = [
                {k: v for k, v in run.items() if k not in {"output", "errors"}}
                | {"errors": value_summary(run.get("errors", []), limit=700)}
                for run in value[:10]
            ]
        elif key == "graph":
            facts[key] = {k: v for k, v in value.items() if k.endswith("_count")}
        elif key == "changes":
            facts[key] = []
            for change in value[:5]:
                item = {k: v for k, v in change.items() if k not in {"before", "after"}}
                if "before" in change or "after" in change:
                    item["before_after_in_original"] = True
                if "changed_paths" in item:
                    paths = item["changed_paths"][:4]
                    while len(dumps(paths)) > 300:
                        paths.pop()
                    item["changed_paths"] = paths
                    item["omitted_changed_paths"] = item["changed_path_count"] - len(
                        paths
                    )
                if len(dumps([*facts[key], item])) > 1200:
                    break
                facts[key].append(item)
        elif key == "readiness":
            facts[key] = _readiness_facts(value)
        elif key == "validation":
            facts[key] = {
                k: _readiness_facts(v)
                if k == "readiness"
                else value_summary(v, limit=400)
                for k, v in value.items()
            }
        elif isinstance(value, (str, list, dict)):
            facts[key] = value_summary(value, limit=600)
        else:
            facts[key] = value
    if "changes" in facts:
        facts["omitted_changes"] = data.get(
            "change_count", len(data["changes"]) + data.get("omitted_changes", 0)
        ) - len(facts["changes"])
    return {
        **{key: result[key] for key in ("id", "name", "ok", "error") if key in result},
        "data": facts,
        "historical": True,
    }


def _readiness_facts(value: dict) -> dict:
    """Preserve static readiness and located warning samples after compaction.

    Args:
        value: Readiness record returned by shared validation.

    Returns:
        Original status and counts with at most three located warnings.
    """
    warnings = value["warnings"][:3]
    return {
        **value,
        "warnings": warnings,
        "omitted_warnings": value["warning_count"] - len(warnings),
    }
