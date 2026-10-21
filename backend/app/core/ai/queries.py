"""Project workflow records into bounded facts for assistant conversations."""

from typing import Any

from app.core.ai.documents import graph_summary, value_summary
from app.core.exceptions import NotFoundException
from app.models.flow import FlowLog, GraphDocument, GraphQuery
from app.models.user import UserInfo
from app.services.flow import FlowGraphService


async def workflow_list(query: GraphQuery, user: UserInfo) -> dict:
    """Return workflow metadata without nested template definitions.

    Args:
        query: Validated filters, ordering, and pagination.
        user: Account whose accessible workflows to list.

    Returns:
        A compact page preserving exact node counts and page editability totals.
    """
    page = await FlowGraphService.list_graphs(query, user)
    fields = (
        "id",
        "name",
        "category",
        "state",
        "editable",
        "draft_version",
        "updated_at",
        "last_exec",
        "success_rate",
        "average_time",
    )
    return {
        "total": page["total"],
        "page_num": query.page_num,
        "page_size": query.page_size,
        "page_counts": {
            "returned": len(page["items"]),
            "editable": sum(item["editable"] is True for item in page["items"]),
            "read_only": sum(item["editable"] is False for item in page["items"]),
        },
        "items": [
            {
                **{key: item.get(key) for key in fields},
                "published_node_count": len(item.get("node_types", [])),
            }
            for item in page["items"]
        ],
    }


async def workflow_details(
    workflow_id: int, user: UserInfo, conversation_id: int | None
) -> dict:
    """Describe the saved draft and retain a reference without expanded schemas.

    Args:
        workflow_id: Workflow whose saved draft to inspect.
        user: Account with current resource permissions.
        conversation_id: Owning conversation, when persistence is active.

    Returns:
        Metadata, exact counts, a bounded graph overview, and its base reference.
    """
    from app.services.assistant_sessions import AssistantSessionService

    graph = await FlowGraphService.get_accessible(workflow_id, user)
    document = graph.draft or {"nodes": [], "edges": []}
    result = {
        "id": graph.id,
        "name": graph.name,
        "created_at": graph.created_at,
        "category": graph.category,
        "state": graph.state,
        "editable": graph.editable,
        "draft_version": graph.draft_version,
        "graph": graph_summary(document),
        "published_node_count": len((graph.definition or {}).get("nodes", [])),
        "base": {"workflow_id": graph.id, "draft_version": graph.draft_version},
    }
    if conversation_id is not None:
        snapshot = await AssistantSessionService.snapshot(
            conversation_id,
            graph.id,
            graph.draft_version,
            GraphDocument.model_validate(document),
            "saved",
            user,
        )
        result["base"]["snapshot_id"] = snapshot.id
    return result


async def workflow_logs(workflow_id: int, limit: int, user: UserInfo) -> dict:
    """Summarize retained executions while preserving errors and exact output counts.

    Args:
        workflow_id: Workflow whose executions to inspect.
        limit: Maximum number of recent execution groups.
        user: Account with current workflow access.

    Returns:
        Run summaries with stable detail identifiers and omission metadata.
    """
    await FlowGraphService.get_accessible(workflow_id, user)
    query = FlowLog.filter(graph_id=workflow_id)
    starts = (
        await query.order_by("-started_at")
        .distinct()
        .limit(limit + 1)
        .values_list("started_at", flat=True)
    )
    runs = []
    for started in starts[:limit]:
        run = query.filter(started_at=started)
        first = await run.order_by("id").first().only("id")
        error_count = await run.filter(ended_at=None).count()
        errors = await run.filter(ended_at=None).order_by("id").limit(5)
        last = await run.exclude(ended_at=None).order_by("-ended_at", "-id").first()
        output = last.retval if last else None
        count = len(output) if isinstance(output, list) else None
        runs.append(
            {
                "run_id": first.id,
                "started_at": started,
                "ended_at": last.ended_at if last else None,
                "status": "failed"
                if error_count
                else "completed"
                if last
                else "incomplete",
                "duration_ms": (last.ended_at - started).total_seconds() * 1000
                if last
                else None,
                "error_count": error_count,
                "errors": [
                    {
                        "log_id": row.id,
                        "node_id": row.node_id,
                        "node_type": row.node_type,
                        "input_handle": row.input_id,
                        "code": (row.node_data or {}).get("$error"),
                        "message": value_summary(row.exc_info, limit=500),
                    }
                    for row in errors[:5]
                ],
                "omitted_errors": max(0, error_count - 5),
                "output_log_id": last.id if last else None,
                "output_count": count,
                "output": value_summary(output),
            }
        )
    return {
        "workflow_id": workflow_id,
        "runs": runs,
        "truncated": len(starts) > limit,
    }


def value_page(value: Any, offset: int, limit: int) -> dict:
    """Read an explicit bounded slice without pretending it is the whole value.

    Args:
        value: Stored array, object, text, or scalar.
        offset: Zero-based item or character offset.
        limit: Maximum items or characters to include.

    Returns:
        A bounded page with the original total and continuation offset.
    """
    if isinstance(value, dict):
        items = list(value.items())
        data = {
            key: value_summary(item) for key, item in items[offset : offset + limit]
        }
        total = len(items)
    elif isinstance(value, list):
        data = [value_summary(item) for item in value[offset : offset + limit]]
        total = len(value)
    elif isinstance(value, str):
        data, total = value[offset : offset + limit], len(value)
    else:
        data, total = value, 1
    return {
        "data": data,
        "total": total,
        "offset": offset,
        "next_offset": offset + limit if offset + limit < total else None,
    }


async def log_detail(
    workflow_id: int,
    log_id: int,
    field: str,
    path: list[str | int],
    offset: int,
    limit: int,
    user: UserInfo,
) -> dict:
    """Retrieve an authorized retained log value by stable identity and explicit path.

    Args:
        workflow_id: Owning workflow.
        log_id: Exact retained record identifier.
        field: Stored output, parameters, node data, or exception field.
        path: Object keys or array indexes into that value.
        offset: Page offset within the selected value.
        limit: Maximum returned items or characters.
        user: Authenticated account.

    Returns:
        A bounded detail page with source identifiers.

    Raises:
        NotFoundException: If the record or value path is unavailable.
    """
    await FlowGraphService.get_accessible(workflow_id, user)
    row = await FlowLog.get_or_none(id=log_id, graph_id=workflow_id)
    if row is None:
        raise NotFoundException
    value = getattr(row, field)
    try:
        for key in path:
            if not isinstance(value, (list, dict)):
                raise KeyError(key)
            value = value[key]
    except (KeyError, IndexError, TypeError):
        raise NotFoundException from None
    return {
        "workflow_id": workflow_id,
        "log_id": log_id,
        "field": field,
        "path": path,
        **value_page(value, offset, limit),
    }
