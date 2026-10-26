"""Project workflow, media, and download records into bounded assistant facts."""

from typing import Any, Literal

from sanic import Sanic

from app.core.ai.documents import graph_summary, value_summary
from app.core.dl.driver import DownloadState
from app.core.exceptions import NotFoundException
from app.core.media.watcher import LibWatcher
from app.models.download import Downloader, DownloadQuery, DownloadTask
from app.models.flow import FlowLog, GraphDocument, GraphQuery, JobQuery, JobTrigger
from app.models.media import MediaItem, MediaQuery
from app.models.user import UserInfo
from app.services.download import DownloadTaskService
from app.services.flow import FlowGraphService, FlowJobService
from app.services.media import MediaItemService, MediaLibService
from app.utils.json import dumps

MEDIA_FIELDS = (
    "id",
    "lib_id",
    "parent_id",
    "title",
    "name",
    "year",
    "season",
    "episode",
    "rating",
)
DETAIL_FIELDS = (
    "title",
    "name",
    "year",
    "season",
    "episode",
    "rating",
    "aired",
    "media_type",
    "path",
    "size",
    "format",
    "index_state",
    "index_error",
)
CHILD_SAMPLE = 10
METADATA_PREVIEW_BYTES = 1200
DOWNLOAD_FIELDS = (
    "id",
    "name",
    "downloader_id",
    "state",
    "raw_state",
    "error_kind",
    "percentage",
    "completed_size",
    "total_size",
    "dl_speed",
    "up_speed",
    "transfer_lib_id",
    "completed_at",
    "updated_at",
)
DOWNLOAD_STORED_FIELDS = tuple(
    field for field in DOWNLOAD_FIELDS if field != "error_kind"
) + ("error_msg",)


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
                "downloadable_search": item.get("downloadable_search", False),
            }
            for item in page["items"]
        ],
    }


async def workflow_jobs(query: JobQuery, user: UserInfo) -> dict:
    """Page accessible schedules without consulting or changing the scheduler.

    Args:
        query: Workflow name, activation state, trigger, ordering, and pagination.
        user: Account whose workflow permissions the service applies.

    Returns:
        Applied filters, exact counts and continuation, distinct schedule/workflow
        identities, and only the timing fields used by each trigger. Times use UTC;
        activation, concurrency and recovery settings do not prove execution.
    """
    page = await FlowJobService.list_jobs(query, user)
    timing_fields = {
        JobTrigger.DATE: ("run_date",),
        JobTrigger.CRON: ("cron_expr",),
        JobTrigger.INTERVAL: (
            "interval_num",
            "interval_unit",
            "interval_start",
            "interval_end",
        ),
    }
    return {
        "filters": {
            "name": query.name or None,
            "state": query.state,
            "trigger": query.trigger,
            "ordering": query.ordering or "",
        },
        "total": page["total"],
        "page_num": query.page_num,
        "page_size": query.page_size,
        "page_counts": {"returned": len(page["items"])},
        "next_page_num": (
            query.page_num + 1
            if query.page_num * query.page_size < page["total"]
            else None
        ),
        "timezone": "UTC",
        "items": [
            {
                "job_id": job["id"],
                "workflow_id": job["graph_id"],
                "workflow_name": job["graph_name"],
                **{
                    key: job[key]
                    for key in (
                        "state",
                        "trigger",
                        "repeatable",
                        "recoverable",
                        *timing_fields[job["trigger"]],
                    )
                },
            }
            for job in page["items"]
        ],
    }


async def workflow_details(
    workflow_id: int, user: UserInfo, conversation_id: int
) -> dict:
    """Describe the saved draft and retain a reference without expanded schemas.

    Args:
        workflow_id: Workflow whose saved draft to inspect.
        user: Account with current resource permissions.
        conversation_id: Owning conversation for the saved-draft snapshot.

    Returns:
        Metadata, exact counts, a bounded saved-draft overview, and its base
        reference. `graph_source` identifies what the graph counts describe.
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
        "graph_source": "saved_draft",
        "graph": graph_summary(document),
        "published_node_count": len((graph.definition or {}).get("nodes", [])),
        "base": {"workflow_id": graph.id, "draft_version": graph.draft_version},
    }
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
        Output counts cover a root array or an object's `items` array, with its
        exact path; other output shapes have an unknown count and path. Skip
        groups deleted during retrieval and omit duration without an end time.
    """
    await FlowGraphService.get_accessible(workflow_id, user)
    query = FlowLog.filter(graph_id=workflow_id)
    starts = (
        await query.order_by("-started_at")
        .distinct()
        .limit(limit + 1)
        .only("started_at")
    )
    runs = []
    for start in starts[:limit]:
        started = start.started_at
        run = query.filter(started_at=started)
        first = await run.order_by("id").first().only("id")
        if first is None:
            continue
        error_count = await run.filter(ended_at=None).count()
        errors = await run.filter(ended_at=None).order_by("id").limit(5)
        last = await run.exclude(ended_at=None).order_by("-ended_at", "-id").first()
        ended_at = last.ended_at if last else None
        output = last.retval if last else None
        items = output.get("items") if isinstance(output, dict) else output
        count = len(items) if isinstance(items, list) else None
        count_path = (
            (["items"] if isinstance(output, dict) else [])
            if count is not None
            else None
        )
        runs.append(
            {
                "run_id": first.id,
                "started_at": started,
                "ended_at": ended_at,
                "status": "failed"
                if error_count
                else "completed"
                if last
                else "incomplete",
                "duration_ms": (ended_at - started).total_seconds() * 1000
                if ended_at is not None
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
                "output_count_path": count_path,
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
            if isinstance(value, list):
                if not isinstance(key, int):
                    raise KeyError(key)
                value = value[key]
            elif isinstance(value, dict):
                value = value[key]
            else:
                raise KeyError(key)
    except (KeyError, IndexError, TypeError):
        raise NotFoundException from None
    return {
        "workflow_id": workflow_id,
        "log_id": log_id,
        "field": field,
        "path": path,
        **value_page(value, offset, limit),
    }


def lib_watcher() -> LibWatcher:
    """Return the current worker's library watcher for read-only scan status.

    Returns:
        The watcher started with this application worker.
    """
    return Sanic.get_app().ctx.lib_watcher


def media_fact(item: dict, **owners: int | None) -> dict:
    """Keep the identity and display facts of a serialized media item.

    Args:
        item: Media item dumped by the media service, which omits relation IDs.
        **owners: Known `lib_id` and `parent_id` values for the item.

    Returns:
        Compact facts; null values mean unavailable or, for `parent_id`, a
        top-level work.
    """
    return {key: item.get(key) for key in MEDIA_FIELDS} | owners


def _child_order(item: dict) -> tuple:
    """Order children by season, episode, and display name like the item page.

    Args:
        item: Serialized child item.

    Returns:
        A deterministic sort key with unknown numbers last.
    """
    return (
        item.get("season") is None,
        item.get("season") or 0,
        item.get("episode") is None,
        item.get("episode") or 0,
        (item.get("title") or item.get("name") or "").casefold(),
        item["id"],
    )


async def media_libraries(user: UserInfo) -> dict:
    """List accessible libraries with the facts needed to answer questions.

    Args:
        user: Account whose library permissions apply.

    Returns:
        The exact number of accessible libraries and their identities, scan state,
        and bound ingest workflows. Reading this never starts a scan.
    """
    libraries = await MediaLibService.list_libraries(user, lib_watcher())
    fields = ("id", "name", "lib_type", "language", "dir", "scanning")
    return {
        "total": len(libraries),
        "items": [
            {
                **{key: library[key] for key in fields},
                "ingest_workflows": [
                    {"id": trigger["graph_id"], "name": trigger["graph_name"]}
                    for trigger in library["triggers"]
                ],
            }
            for library in libraries
        ],
    }


async def media_items(query: MediaQuery, user: UserInfo) -> dict:
    """Page visible items while keeping unavailable libraries distinct from empty ones.

    Args:
        query: Library, title keyword, exact path, and pagination filters.
        user: Account whose library permissions apply.

    Returns:
        The exact matching total, this page's count and continuation, and compact
        items in the library page's order.

    Raises:
        NotFoundException: If the requested library does not exist.
        ForbiddenException: If the requested library is not accessible.
    """
    if query.lib_id is not None:
        # a missing or inaccessible library must not look like an empty one
        await MediaLibService.get_accessible(query.lib_id, user)
    page = await MediaItemService.list_items(query, user)
    owners = {
        row["id"]: row
        for row in await MediaItem.filter(
            id__in=[item["id"] for item in page["items"]]
        ).values("id", "lib_id", "parent_id")
    }
    more = query.page_num * query.page_size < page["total"]
    return {
        "total": page["total"],
        "page_num": query.page_num,
        "page_size": query.page_size,
        "page_counts": {"returned": len(page["items"])},
        "next_page_num": query.page_num + 1 if more else None,
        "scope": "exact_path" if query.path else "top_level_works",
        "items": [
            media_fact(
                item,
                lib_id=owners.get(item["id"], {}).get("lib_id"),
                parent_id=owners.get(item["id"], {}).get("parent_id"),
            )
            for item in page["items"]
        ],
    }


async def media_item_details(
    item_id: int,
    section: Literal["summary", "metadata", "children"],
    field: str | None,
    offset: int,
    limit: int,
    user: UserInfo,
) -> dict:
    """Read an accessible item as a compact summary or one bounded detail page.

    Args:
        item_id: Requested media item.
        section: `summary`, a `metadata` page, or a `children` page.
        field: Metadata key to page instead of the whole metadata object.
        offset: Zero-based item or character offset within the selected value.
        limit: Maximum returned items or characters.
        user: Account whose library permissions apply.

    Returns:
        Item facts with metadata state, a bounded metadata preview and child sample,
        or the requested page; omitted counts and continuation offsets are explicit.

    Raises:
        NotFoundException: If the item, its visible parent, or the field is missing.
        ForbiddenException: If the item's library is not accessible.
    """
    details = await MediaItemService.get_details(item_id, user)
    base = {"item_id": item_id, "library_id": details["lib"]["id"], "section": section}
    # the service returns only visible children in the item's own library
    children = [
        media_fact(child, lib_id=base["library_id"], parent_id=item_id)
        for child in sorted(details["children"], key=_child_order)
    ]
    if section == "children":
        return {**base, **value_page(children, offset, limit)}
    metadata = details.get("metadata")
    if section == "metadata":
        value = metadata
        if field is not None:
            if not isinstance(metadata, dict) or field not in metadata:
                raise NotFoundException
            value = metadata[field]
        return {
            **base,
            "field": field,
            "metadata_state": details["metadata_state"],
            **value_page(value, offset, limit),
        }
    preview: dict[str, Any] = {}
    empty = omitted = 0
    for key, value in (metadata or {}).items():
        if value in (None, "", [], {}):
            empty += 1
            continue
        summary = value_summary(value, limit=200)
        if len(dumps({**preview, key: summary})) > METADATA_PREVIEW_BYTES:
            omitted += 1
            continue
        preview[key] = summary
    parent = details.get("parent")
    return {
        **base,
        **{key: details.get(key) for key in DETAIL_FIELDS},
        **{key: details[key] for key in ("role", "metadata_task") if key in details},
        "lib": {key: details["lib"][key] for key in ("id", "name", "lib_type")},
        "parent": (
            {key: parent.get(key) for key in ("id", "title", "name")}
            if parent
            else None
        ),
        "metadata_state": details["metadata_state"],
        "metadata_issues": value_summary(details["metadata_issues"], limit=600),
        "metadata": preview,
        "metadata_field_count": len(metadata or {}),
        "empty_metadata_fields": empty,
        "omitted_metadata_fields": omitted,
        "children": {
            "total": len(children),
            "items": children[:CHILD_SAMPLE],
            "omitted": max(0, len(children) - CHILD_SAMPLE),
        },
    }


async def download_tasks(
    query: DownloadQuery, scope: Literal["all", "active", "completed"]
) -> dict:
    """Page stored download tasks like the active and completed download pages.

    Visibility matches the shared download pages. Values come from the database,
    so no downloader is polled or controlled.

    Args:
        query: Name, state, downloader, ordering, and pagination filters.
        scope: `active` excludes completed tasks, `completed` keeps only completed
            tasks, and `all` applies no state scope.

    Returns:
        The exact matching total, this page's count and continuation, downloader
        names, and compact task identities, states and progress. Error lengths
        indicate when a detail read can retrieve stored error text. Sizes, speeds,
        raw status and update times remain available through the detail tool.
    """
    if scope == "active":
        query.states = [state for state in DownloadState if state != "completed"]
    elif scope == "completed":
        query.states = [DownloadState.COMPLETED]
    # the service adds `error_kind` from offline jobs after serializing stored fields
    page = await DownloadTaskService.list_tasks(query, include=DOWNLOAD_STORED_FIELDS)
    items = []
    for task in page["items"]:
        item = {
            key: task.get(key)
            for key in ("id", "name", "downloader_id", "state", "percentage")
        }
        error = task.get("error_msg")
        if error:
            item["error_length"] = len(error)
        items.append(item)
    downloaders = await Downloader.filter(
        id__in={task["downloader_id"] for task in page["items"]}
    ).values("id", "name")
    more = query.page_num * query.page_size < page["total"]
    return {
        "scope": scope,
        "filters": query.model_dump(
            include={"name", "state", "downloader_id", "ordering"}
        ),
        "total": page["total"],
        "page_num": query.page_num,
        "page_size": query.page_size,
        "page_counts": {"returned": len(items)},
        "next_page_num": query.page_num + 1 if more else None,
        "downloaders": sorted(downloaders, key=lambda row: row["id"]),
        "items": items,
    }


async def download_task_details(task_id: int, offset: int, limit: int) -> dict:
    """Read one shared task and an exact page of its stored error message.

    Args:
        task_id: Task ID from the displayed selection or a query result.
        offset: Zero-based character offset in the error message.
        limit: Maximum error characters to return.

    Returns:
        Named task fields and a paged error, or null when no error was recorded.
        Values use the same shared visibility and stored status as list queries.

    Raises:
        NotFoundException: If the task no longer exists.
    """
    task = await DownloadTask.get_or_none(id=task_id)
    if task is None:
        raise NotFoundException
    data = await DownloadTaskService.dump(task, include=DOWNLOAD_STORED_FIELDS)
    return {
        **{key: data.get(key) for key in DOWNLOAD_FIELDS},
        "error": value_page(task.error_msg, offset, limit)
        if task.error_msg is not None
        else None,
    }
