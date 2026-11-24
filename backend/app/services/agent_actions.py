"""Expose human-approved administrator domain operations and durable receipts."""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from tortoise.exceptions import TransactionManagementError
from tortoise.transactions import in_transaction

from app.core.decorators import require_browser_admin
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
    content_error,
)
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.shelver import update_metadata
from app.core.media.title import nfo_title
from app.models.agent_actions import AdminChange, AgentAdminReview
from app.models.download import DownloadPlan
from app.models.flow import (
    FlowJob,
    FlowTrigger,
    GraphDocument,
    GraphWrite,
)
from app.models.media import (
    LibType,
    MediaLib,
    MediaLibUpsert,
)
from app.models.user import UserInfo
from app.services.flow import FlowGraphService
from app.services.media import MediaItemService, MediaLibService
from app.services.review_common import (
    get_or_create_operation_review,
    load_operation_review,
    lock_operation_decision,
    require_live_admin,
    reserve_writer,
)
from app.services.workspace_queries import inspect_workspace
from app.utils.asyncio import write_in_thread
from app.utils.json import json_digest

__all__ = [
    "action_status",
    "decide_action",
    "inspect_workspace",
    "review_action",
]


async def _preview(change: AdminChange, user: UserInfo) -> dict:
    """Resolve the target and collect its immutable revision and affected records.

    Args:
        change: Schema-checked action and settings.
        user: Administrator account.

    Returns:
        Frozen review with current and proposed values.

    Raises:
        BadRequestException: If the inputs or current target are invalid.
        NotFoundException: If a library is missing.
    """
    fields = {
        "publish_workflow": {"target_id"},
        "create_library": {"name", "directory", "library_type"},
        "delete_library": {"target_id"},
        "edit_metadata": {"target_id", "title"},
    }
    if {
        k for k, v in change.model_dump(exclude={"action"}).items() if v is not None
    } != fields[change.action]:
        raise BadRequestException
    if change.action == "publish_workflow":
        graph = await FlowGraphService.get_accessible(change.target_id, user)
        document = GraphDocument.model_validate(
            graph.draft or {"nodes": [], "edges": []}
        )
        validation = await FlowGraphService.validate_graph(graph.id, document, user)
        previous = {n["id"]: n for n in (graph.definition or {}).get("nodes", [])}
        current = {n["id"]: n for n in (graph.draft or {}).get("nodes", [])}
        changed = sorted(
            k for k in current.keys() & previous.keys() if current[k] != previous[k]
        )
        affected = {
            "added_nodes": sorted(current.keys() - previous.keys()),
            "removed_nodes": sorted(previous.keys() - current.keys()),
            "changed_nodes": changed,
            "bound_resources": await FlowTrigger.filter(graph_id=graph.id)
            .order_by("id")
            .values("category", "rel_id"),
            "schedules": await FlowJob.filter(graph_id=graph.id)
            .order_by("id")
            .values_list("id", flat=True),
            "download_plans": await DownloadPlan.filter(graph_id=graph.id)
            .order_by("id")
            .values_list("id", flat=True),
        }
        return {
            "name": graph.name,
            "workflow_id": graph.id,
            "draft_version": graph.draft_version,
            "revision": graph.revision,
            "valid": validation.valid,
            "issues": [i.model_dump(mode="json") for i in validation.issues[:10]],
            "edges_changed": (graph.draft or {}).get("edges", [])
            != (graph.definition or {}).get("edges", []),
            "source_digest": json_digest(
                {
                    "draft": graph.draft,
                    "definition": graph.definition,
                    "affected": affected,
                }
            ),
            **{key: values[:12] for key, values in affected.items()},
            "additional": {
                key: len(values) - 12
                for key, values in affected.items()
                if len(values) > 12
            },
        }
    if change.action == "create_library":
        body = MediaLibUpsert(
            name=change.name, lib_type=change.library_type, dir=change.directory
        )
        if not Path(body.dir).is_absolute():
            raise BadRequestException
        path = Path(body.dir).resolve()
        for existing in await MediaLib.all().values("name", "dir"):
            other = Path(existing["dir"]).resolve()
            if (
                existing["name"] == body.name
                or other.is_relative_to(path)
                or path.is_relative_to(other)
            ):
                raise BadRequestException
        return {
            "name": body.name,
            "directory": str(path),
            "library_type": body.lib_type.value,
            "scan_on_startup": body.scan_on_startup,
        }
    if change.action == "delete_library":
        lib = await MediaLibService.get_accessible(change.target_id, user)
        return await MediaLibService.deletion_preview(lib.id)
    item = await MediaItemService.get_accessible(change.target_id, user)
    if item.lib.lib_type in (LibType.MOVIE, LibType.TV_SHOW):
        if not item.nfo_path:
            raise BadRequestException
        try:
            state = await asyncio.to_thread(
                nfo_title, item.lib.dir, item.nfo_path, item.lib.lib_type
            )
        except ContentError as exc:
            raise content_error(exc) from None
        except OSError:
            raise content_error(ContentError("media_source_unavailable")) from None
        return {
            "item_id": item.id,
            "library_id": item.lib_id,
            "after_title": change.title,
            **state,
        }
    _, metadata, _, issue = await MediaItemService._get_metadata(
        item, user, with_cover=False
    )
    if issue or metadata.state == "error":
        raise BadRequestException
    return {
        "item_id": item.id,
        "library_id": item.lib_id,
        "before_title": metadata.data.title,
        "after_title": change.title,
        "revision": json_digest(metadata.data.model_dump(mode="json")),
    }


def _receipt(row: AgentAdminReview) -> dict:
    """Expose the essential review fields and current durable outcome.

    Args:
        row: Owned review.

    Returns:
        Operation ID, visible impact and current decision state.
    """
    return {
        "operation_id": str(row.operation_id),
        "action": row.request["action"],
        "status": row.status,
        "digest": row.digest,
        "preview": row.preview,
        "result": row.result,
        "expires_at": row.expires_at.isoformat(),
    }


async def review_action(change: AdminChange, user: UserInfo) -> dict:
    """Freeze the proposed effect without executing the underlying operation.

    Args:
        change: Intended settings.
        user: Authenticated administrator.

    Returns:
        Reusable account-owned pending review or a validation failure.

    Raises:
        ForbiddenException: If the account is no longer an administrator.
    """
    await require_live_admin(user)
    async with in_transaction():
        await reserve_writer(AgentAdminReview.filter(owner_id=user.id, id=0))
        preview = await _preview(change, user)
        request = change.model_dump(mode="json", exclude_none=True)
        digest = json_digest({"request": request, "preview": preview})
        if preview.get("valid") is False:
            return {
                "action": change.action,
                "status": "invalid",
                "preview": preview,
            }
        row = await get_or_create_operation_review(
            AgentAdminReview,
            owner_id=user.id,
            digest=digest,
            active_statuses=("pending", "failed"),
            defaults={"request": request, "preview": preview},
            reserve=False,
        )
        return _receipt(row)


async def action_status(key: UUID, user: UserInfo) -> dict:
    """Read an owned review, expiring pending decisions as needed.

    Args:
        key: Opaque operation ID.
        user: Current administrator.

    Returns:
        Latest persisted state.

    Raises:
        NotFoundException: If the receipt is absent or outside the caller's grant.
    """
    row = await load_operation_review(
        AgentAdminReview,
        key,
        user,
        expirable_statuses=("pending", "failed"),
        stale_executing_after=timedelta(minutes=10),
    )
    return _receipt(row)


async def _apply(change: AdminChange, preview: dict, user: UserInfo) -> dict:
    """Execute the exact preview using the media or workflow domain service.

    Args:
        change: Previously approved action.
        preview: Revalidated settings and target revision.
        user: Approving account.

    Returns:
        The resulting object identity or deletion outcome.
    """
    if change.action == "publish_workflow":
        graph = await FlowGraphService.get_accessible(change.target_id, user)
        document = GraphWrite.model_validate(
            {
                **(graph.draft or {"nodes": [], "edges": []}),
                "draft_version": preview["draft_version"],
            }
        )
        graph = await FlowGraphService.publish_graph(graph.id, document, user)
        return {"workflow_id": graph.id, "revision": graph.revision}
    if change.action == "create_library":
        lib = await MediaLibService.upsert(
            MediaLibUpsert(
                name=change.name, lib_type=change.library_type, dir=preview["directory"]
            )
        )
        return {"library_id": lib.id}
    if change.action == "delete_library":
        await MediaLibService.delete(change.target_id, expected=preview)
        return {"library_id": change.target_id, "deleted": True, "delete_files": False}
    item = await MediaItemService.get_accessible(change.target_id, user)
    if item.lib.lib_type in (LibType.MOVIE, LibType.TV_SHOW):
        async with library_lock(item.lib.dir):
            item = await MediaItemService.get_accessible(item.id, user)
            if not item.nfo_path or item.lib_id != preview["library_id"]:
                raise BadRequestException
            try:
                await write_in_thread(
                    nfo_title,
                    item.lib.dir,
                    item.nfo_path,
                    item.lib.lib_type,
                    title=change.title,
                    revision=preview["revision"],
                )
            except ContentError as exc:
                # the NFO writer validates these conditions before atomic replacement
                raise BadRequestException from exc
            try:
                ids = await update_metadata(item.lib, item.nfo_path)
            except Exception:
                ids = []
            if item.id not in ids:
                return {"item_id": item.id, "saved": True, "metadata_refreshed": False}
        return {"item_id": item.id, "saved": True}
    _, metadata, _, _ = await MediaItemService._get_metadata(
        item, user, with_cover=False
    )
    values = metadata.data.model_dump(mode="json")
    if json_digest(values) != preview["revision"]:
        raise BadRequestException
    saved = await MediaItemService.save_metadata(
        item.id,
        {**values, "title": change.title},
        overwrite=True,
        expected=metadata.data,
    )
    return {"item_id": item.id, "saved": saved}


async def decide_action(key: UUID, digest: str, approve: bool, user: UserInfo) -> dict:
    """Claim one explicit browser decision and record its outcome across retries.

    Args:
        key: Review identity.
        digest: Digest displayed on the trusted confirmation surface.
        approve: Explicit user decision.
        user: Authenticated browser administrator.

    Returns:
        Durable receipt. An interrupted filesystem operation requires inspection.

    Raises:
        ForbiddenException: If the decision comes from an external MCP client.
        BadRequestException: If the digest differs from the displayed review.
    """
    await require_browser_admin(user)
    query = AgentAdminReview.filter(operation_id=key, owner_id=user.id)
    async with in_transaction():
        row = await lock_operation_decision(
            AgentAdminReview,
            key,
            digest,
            user,
            lock_statuses=("pending", "failed"),
            expirable_statuses=("pending", "failed"),
            stale_executing_after=timedelta(minutes=10),
        )
        if row.status not in {"pending", "failed"}:
            return _receipt(row)
        previous = row.status
        change = AdminChange.model_validate(row.request)
        status = "cancelled"
        if approve:
            try:
                same = await _preview(change, user) == row.preview
            except ForbiddenException:
                raise
            except (KaloscopeException, ValueError):
                same = False
            status = "executing" if same else "conflict"
        await query.filter(status=previous).update(status=status)
        if status != "executing":
            return await action_status(key, user)
        if change.action == "publish_workflow":
            action_error = None
            try:
                async with in_transaction():
                    try:
                        result = await _apply(change, row.preview, user)
                    except Exception as exc:
                        action_error = exc
                        raise
            except (ForbiddenException, TransactionManagementError):
                raise
            except Exception as exc:
                if exc is not action_error:
                    # let the outer transaction roll back a failed savepoint operation
                    raise
                # only failures with a completed rollback may be retried
                status = (
                    "conflict"
                    if isinstance(exc, (KaloscopeException, ValueError))
                    else "failed"
                )
                await query.update(status=status)
            else:
                await query.update(status="applied", result=result)
            return await action_status(key, user)
    # filesystem and watcher services own their transaction and lifecycle
    try:
        result = await _apply(change, row.preview, user)
    except ContentError as exc:
        await query.update(
            status="conflict" if exc.code == "content_changed" else "uncertain"
        )
    except KaloscopeException as exc:
        before_write = isinstance(
            exc, (BadRequestException, NotFoundException)
        ) or exc.message in {
            ErrorCode.NAME_ALREADY_EXISTS,
            ErrorCode.DUPLICATE_DIRECTORY,
        }
        await query.update(status="conflict" if before_write else "uncertain")
    except Exception:
        await query.update(status="uncertain")
    else:
        status = "partial" if result.get("metadata_refreshed") is False else "applied"
        await query.update(status=status, result=result)
    return await action_status(key, user)
