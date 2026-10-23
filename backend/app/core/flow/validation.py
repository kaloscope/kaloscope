"""Validate editor graphs against registered nodes without executing them."""

import math
import re
from collections import Counter, defaultdict, deque
from copy import deepcopy
from typing import Any

from jinja2 import TemplateError

from app.core.exceptions import KaloscopeException
from app.core.flow.context import LOOP_KEY
from app.core.flow.fields import Field, NumberField, SelectField
from app.core.flow.nodes.base import Node, NodeGroup
from app.core.flow.nodes.general.ai import AINode
from app.core.renderer import ENV
from app.models.flow import (
    GraphCategory,
    GraphDocument,
    GraphIssue,
    GraphReadiness,
    GraphValidation,
)
from app.services.ai import AIProviderService


def compress_document(document: GraphDocument) -> dict[str, Any]:
    """Copy an editor document and keep only server-owned node type references.

    Args:
        document: Validated graph envelope, optionally including a write version.

    Returns:
        Independent graph with compressed schemas and no write version.
    """
    result = document.model_dump(include={"nodes", "edges"})
    for node in result["nodes"]:
        schema = node["data"].get("$schema")
        if isinstance(schema, dict):
            node["data"]["$schema"] = schema.get("node_type")
    return deepcopy(result)


def comparison_document(document: dict[str, Any]) -> dict[str, Any]:
    """Copy a graph without transient editor state for meaningful comparisons.

    Args:
        document: Saved or proposed compressed graph.

    Returns:
        An independent graph preserving authored fields and layout.
    """
    result = compress_document(GraphDocument.model_validate(deepcopy(document)))
    for node in result["nodes"]:
        for key in ("selected", "measured", "dragging"):
            node.pop(key, None)
    for edge in result["edges"]:
        edge.pop("selected", None)
    return result


def _field_issue(field: Field, value: Any, *, complete: bool) -> str | None:
    """Check a visible field's type, bounds, and optional publication constraints.

    Args:
        field: Registered field definition.
        value: Supplied value or registered default.
        complete: Whether to check missing values and template syntax.

    Returns:
        Stable issue code, or `None` when this field is acceptable.
    """
    kind = field.field_type
    if kind == "divider":
        return None
    if value is None or (isinstance(value, str) and not value.strip()):
        return "required" if complete and field.required else None
    if kind in {"text", "textarea", "url", "code"}:
        if not isinstance(value, str):
            return "field_type"
        if (maximum := getattr(field, "maxlength", None)) and len(value) > maximum:
            return "field_length"
        if complete and len(value) < getattr(field, "minlength", 0):
            return "field_length"
        if complete and (
            getattr(field, "language", None) == "jinja2"
            or (kind != "code" and any(mark in value for mark in ("{{", "{%")))
        ):
            try:
                ENV.parse(value)
            except TemplateError:
                return "template_syntax"
    elif isinstance(field, NumberField):
        if type(value) is not int and type(value) is not float:
            return "field_type"
        try:
            if not math.isfinite(value):
                return "field_type"
        except OverflowError:
            return "field_range"
        if complete and (
            (field.min is not None and value < field.min)
            or (field.max is not None and value > field.max)
            or round(value, field.precision) != value
        ):
            return "field_range"
    elif isinstance(field, SelectField):
        if not any(
            type(value) is type(option) and value == option
            for option in field.options.values()
        ):
            return "field_option"
    elif kind == "toggle":
        if not isinstance(value, bool):
            return "field_type"
    elif kind == "script":
        if not isinstance(value, dict) or value.get("language") not in (
            "python",
            "javascript",
        ):
            return "field_type"
        code = value.get("code")
        if code is not None and not isinstance(code, str):
            return "field_type"
        if complete and field.required and (not code or not code.strip()):
            return "required"
    elif kind == "kvpairs":
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(pair, dict)
            or not isinstance(pair.get("key"), str)
            or not isinstance(pair.get("value"), str)
            for pair in value
        ):
            return "field_type"
    elif kind == "ai_model":
        if not isinstance(value, dict):
            return "field_type"
        if value.get("provider_id") is not None and (
            type(value["provider_id"]) is not int or value["provider_id"] <= 0
        ):
            return "field_type"
        if value.get("model_id") is not None and not isinstance(value["model_id"], str):
            return "field_type"
        if complete and (
            not value.get("provider_id") or not (value.get("model_id") or "").strip()
        ):
            return "required"
    return None


def _has_cycle(nodes: set[str], adjacency: dict[str, list[str]]) -> bool:
    """Detect directed cycles without recursion or executing loop expressions.

    Args:
        nodes: Node or component identifiers in the graph.
        adjacency: Outgoing destinations for each identifier.

    Returns:
        Whether the directed graph contains a cycle.
    """
    indegree = Counter(target for targets in adjacency.values() for target in targets)
    pending = deque(node_id for node_id in nodes if indegree[node_id] == 0)
    visited = 0
    while pending:
        node_id = pending.popleft()
        visited += 1
        for target in adjacency.get(node_id, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    return visited != len(nodes)


async def validate_document(
    document: GraphDocument, category: GraphCategory, *, complete: bool = True
) -> GraphValidation:
    """Check graph structure, fields, and potential category-specific entry paths.

    Args:
        document: Graph with typed node and edge envelopes.
        category: Workflow category constraining the available node types.
        complete: Check required fields, template syntax, and AI dependencies.

    Returns:
        Validation status with node, field, and edge locations. No workflow,
        script, template expression, or model request is executed.
    """
    graph = compress_document(document)
    schemas = {schema.node_type: schema for schema in Node.schemas}
    issues: list[GraphIssue] = []
    if complete and not graph["nodes"]:
        issues.append(GraphIssue(code="empty_graph"))
    nodes = {}
    groups = {}
    handles = {}
    for node in graph["nodes"]:
        node_id, data = node["id"], node["data"]
        if node_id in nodes:
            issues.append(GraphIssue(code="duplicate_node", node_id=node_id))
        nodes[node_id] = node
        node_type = data.get("$schema")
        schema = schemas.get(node_type) if isinstance(node_type, str) else None
        if schema is None:
            issues.append(GraphIssue(code="unknown_node", node_id=node_id))
            continue
        if schema.categories and category not in schema.categories:
            issues.append(GraphIssue(code="node_category", node_id=node_id))
        groups[node_id] = schema.group
        handles[node_id] = {handle.id: handle for handle in schema.handles}
        fields = {field.id: field for field in schema.fields}
        for key, value in data.items():
            # editor dividers store no executable value and may change positional IDs
            if value is None and re.fullmatch(r"divider_\d+", key):
                continue
            if key != "$schema" and key not in fields:
                issues.append(
                    GraphIssue(code="unknown_field", node_id=node_id, field_id=key)
                )
        for field in schema.fields:
            if field.visible_when and any(
                data.get(key, fields[key].default) != expected
                for key, expected in field.visible_when.items()
            ):
                continue
            code = _field_issue(field, field.extract(data), complete=complete)
            if code:
                issues.append(GraphIssue(code=code, node_id=node_id, field_id=field.id))
        if (
            complete
            and node_type == "ai"
            and not any(issue.node_id == node_id for issue in issues)
        ):
            try:
                config = AINode.configuration(data)
                await AIProviderService.resolve_model(config.model)
            except KaloscopeException as exc:
                issues.append(GraphIssue(code=str(exc.message), node_id=node_id))

    edge_ids = set()
    connections = set()
    counts = Counter()
    adjacency = defaultdict(list)
    execution_paths = defaultdict(list)
    undirected = defaultdict(set)
    connected = defaultdict(set)
    tags = defaultdict(set)
    loop_bounds = []
    for edge in graph["edges"]:
        edge_id = edge["id"]
        source, target = edge["source"], edge["target"]
        connection = (source, edge["sourceHandle"], target, edge["targetHandle"])
        if edge_id in edge_ids or connection in connections:
            issues.append(GraphIssue(code="duplicate_edge", edge_id=edge_id))
        edge_ids.add(edge_id)
        connections.add(connection)
        if source == target:
            issues.append(GraphIssue(code="self_connection", edge_id=edge_id))
        if source not in nodes or target not in nodes:
            issues.append(GraphIssue(code="missing_endpoint", edge_id=edge_id))
            continue
        source_handle = handles.get(source, {}).get(edge["sourceHandle"])
        target_handle = handles.get(target, {}).get(edge["targetHandle"])
        if (
            source_handle is None
            or source_handle.handle_type != "source"
            or target_handle is None
            or target_handle.handle_type != "target"
        ):
            issues.append(GraphIssue(code="invalid_handle", edge_id=edge_id))
            continue
        for node_id, handle in ((source, source_handle), (target, target_handle)):
            counts[node_id, handle.id] += 1
            if counts[node_id, handle.id] > handle.maxconn:
                issues.append(
                    GraphIssue(
                        code="connection_limit",
                        severity="warning",
                        node_id=node_id,
                        edge_id=edge_id,
                    )
                )
            if handle.tag and handle.tag != LOOP_KEY:
                tags[node_id].add(handle.tag)
        if source_handle.tag == LOOP_KEY:
            loop_bounds.append((target, source))
        if target_handle.tag == LOOP_KEY:
            loop_bounds.append((source, target))
        if source_handle.tag == target_handle.tag == LOOP_KEY:
            issues.append(GraphIssue(code="invalid_loop", edge_id=edge_id))
        connected[source].add(target)
        connected[target].add(source)
        execution_paths[source].append(target)
        if source_handle.tag != LOOP_KEY and target_handle.tag != LOOP_KEY:
            adjacency[source].append(target)
            undirected[source].add(target)
            undirected[target].add(source)

    # normal edges form a DAG; intentional cycles use explicit loop handles
    if _has_cycle(set(nodes), adjacency):
        issues.append(GraphIssue(code="uncontrolled_cycle"))

    checked = set()
    for node_id in nodes:
        if node_id in checked:
            continue
        pending = [node_id]
        component_tags = set()
        while pending:
            current = pending.pop()
            if current in checked:
                continue
            checked.add(current)
            component_tags.update(tags[current])
            pending.extend(connected[current] - checked)
        if len(component_tags) > 1:
            issues.append(GraphIssue(code="conflicting_tags", node_id=node_id))

    # each loop body belongs to one loop and stays outside that loop's main path
    components = {}
    for node_id in nodes:
        if node_id in components:
            continue
        pending = [node_id]
        while pending:
            current = pending.pop()
            if current in components:
                continue
            components[current] = node_id
            pending.extend(undirected[current] - components.keys())
    owners = defaultdict(set)
    nesting = defaultdict(list)
    for body, owner in loop_bounds:
        owners[components[body]].add(owner)
        nesting[components[owner]].append(components[body])
        if components[body] == components[owner]:
            issues.append(GraphIssue(code="invalid_loop", node_id=owner))
    for component, loop_ids in owners.items():
        if len(loop_ids) > 1:
            issues.append(GraphIssue(code="conflicting_loops", node_id=component))
    if _has_cycle(set(components.values()), nesting):
        issues.append(GraphIssue(code="invalid_loop"))
    valid = not any(issue.severity == "error" for issue in issues)
    return GraphValidation(
        valid=valid,
        issues=issues,
        readiness=_readiness(
            category,
            groups,
            execution_paths,
            empty=not nodes,
            valid=valid,
            complete=complete,
        ),
    )


def _readiness(
    category: GraphCategory,
    groups: dict[str, NodeGroup],
    adjacency: dict[str, list[str]],
    *,
    empty: bool,
    valid: bool,
    complete: bool,
) -> GraphReadiness:
    """Find possible entry-to-result paths without evaluating runtime behavior.

    Indexers and ingestion need a reachable end/result node. Scheduled tasks may
    finish naturally after general work without an explicit end node. Conditions
    and loop handles are possible paths, not guarantees of traversal or success.
    Incomplete field checks never certify readiness. Warnings do not block saving.

    Args:
        category: Category already checked against the registered node schemas.
        groups: Registered groups indexed by node identity.
        adjacency: Directed connections including explicit loop boundaries.
        empty: Whether the document contains no nodes.
        valid: Whether structure and field checks have no blocking issues.
        complete: Whether all required fields and dependencies were checked.

    Returns:
        Static status and at most 30 located warnings with exact omission counts.
    """
    warnings = []
    if empty:
        status = "empty"
    elif not valid:
        status = "invalid"
    elif not complete:
        status = "not_checked"
    else:
        starts = sorted(
            key for key, group in groups.items() if group == NodeGroup.START
        )
        ends = {key for key, group in groups.items() if group == NodeGroup.END}
        outcomes = ends.copy()
        if category == GraphCategory.SCHEDULE:
            outcomes.update(
                key for key, group in groups.items() if group == NodeGroup.GENERAL
            )
        if not starts:
            warnings.append(GraphIssue(code="missing_entry", severity="warning"))
        reached = set()
        for start in starts:
            visited = set()
            pending = [start]
            while pending:
                current = pending.pop()
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(adjacency.get(current, []))
            reached.update(visited)
            if not visited & outcomes:
                warnings.append(
                    GraphIssue(
                        code="no_reachable_task"
                        if category == GraphCategory.SCHEDULE
                        else "no_reachable_output",
                        severity="warning",
                        node_id=start,
                    )
                )
        warnings.extend(
            GraphIssue(code="unreachable_output", severity="warning", node_id=node_id)
            for node_id in sorted(ends - reached)
        )
        status = "incomplete" if warnings else "ready"
    return GraphReadiness(
        status=status,
        category=category,
        warnings=warnings[:30],
        warning_count=len(warnings),
        omitted_warnings=max(0, len(warnings) - 30),
    )
