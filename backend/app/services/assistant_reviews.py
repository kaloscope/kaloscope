"""Bind browser decisions to the exact settings shown by an assistant review."""

from collections.abc import Collection
from datetime import timedelta
from typing import Any
from uuid import UUID

from tortoise import timezone
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from app.core.ai.tools import (
    MEDIA_REVIEW_TOOLS,
    MediaConfirm,
    ScheduleConfirm,
    TaskActionConfirm,
    apply_media_change,
    apply_schedule,
    apply_task_action,
)
from app.core.decorators import require_browser_admin
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.models.assistant import AssistantToolCall
from app.models.download import (
    DownloadPlan,
    DownloadPlanApply,
    DownloadPlanChange,
    DownloadPlanCreate,
    DownloadPlanCreation,
    DownloadPlanReceipt,
    DownloadTask,
    DownloadTaskCheck,
    DownloadTaskCreate,
    DownloadTaskCreation,
    DownloadTaskReceipt,
    PlanReviewDecision,
)
from app.models.flow import FlowJobReceipt
from app.models.media import MediaChangeReceipt
from app.models.review_decision import AssistantReviewDecision
from app.models.user import UserInfo
from app.services.download import DownloadPlanService, DownloadTaskService
from app.services.review_common import (
    is_media_file_listing,
    load_owned_review,
    recent_review_calls,
    reserve_writer,
    schedule_review_target,
)

REVIEW_TOOLS = {
    "validate_download_plan",
    "validate_download_task",
    "review_download_task_action",
    "review_workflow_schedule",
    *MEDIA_REVIEW_TOOLS,
} - {"review_media_batch"}


def _outcome(result: dict) -> dict:
    """Summarize a domain receipt without exposing server-side arguments.

    Args:
        result: Revalidated creation or mutation outcome.

    Returns:
        Display status, durable outcome and the identity of any created resource.
    """
    outcome = result.get("outcome")
    if result.get("uncertain") or outcome == "uncertain":
        status = "uncertain"
    elif outcome == "partial" or result.get("plan_error") or result.get("submit_error"):
        status = "partial"
    elif outcome == "cancelled":
        status = "cancelled"
    elif result.get("pending") or result.get("running") or outcome == "running":
        status = "pending"
    elif (
        result.get("conflict")
        or result.get("stale")
        or outcome in {"absent", "unavailable"}
    ):
        status = "stale"
    elif result.get("confirmation_required"):
        status = "superseded"
    elif (
        result.get("applied") or result.get("created") or outcome == "already_scanning"
    ):
        status = "applied"
    elif (
        result.get("failed")
        or result.get("valid") is False
        or result.get("existing_download")
    ):
        status = "failed"
    else:
        status = "uncertain"
    return {
        "status": status,
        **(
            {"retryable": result.get("retryable", result.get("valid") is False)}
            if status == "failed"
            else {}
        ),
        "outcome": {
            key: result[key]
            for key in (
                "outcome",
                "created",
                "already_exists",
                "duplicate_plan_id",
                "applied",
                "task_id",
                "plan_id",
                "job_id",
                "item_id",
                "library_id",
                "state",
                "first_execution",
                "issues",
                "ingestion",
            )
            if key in result
        },
    }


async def _recorded(tool: str, key: str, user: UserInfo) -> dict | None:
    """Find committed work, including approvals from chat, MCP or the editor.

    Args:
        tool: Type of the original review.
        key: Account-scoped domain receipt key.
        user: Currently authorized review owner.

    Returns:
        Existing outcome or `None` when no domain effect was recorded.
    """
    if tool == "validate_download_plan":
        row = await DownloadPlanCreation.get_or_none(key=key)
        if row is None:
            return None
        plan = await DownloadPlan.filter(id=row.plan_id).first()
        if plan is None:
            return _outcome(
                {"created": False, "outcome": "absent", "plan_id": row.plan_id}
            )
        result = await DownloadPlanService.creation_result(plan, user, replayed=True)
        if plan.creation_key != key:
            result["already_exists"] = True
        return _outcome(result)
    if tool == "validate_download_task":
        receipt = await DownloadTaskCreation.get_or_none(key=key)
        task = await DownloadTask.get_or_none(creation_key=key)
        if receipt and task is None:
            return _outcome(receipt.removed_result())
        return (
            _outcome(
                {
                    "created": True,
                    "task_id": task.id,
                    "state": task.state,
                    "uncertain": task.state == "submit_unknown",
                }
            )
            if task
            else None
        )
    model = {
        "review_workflow_schedule": FlowJobReceipt,
        "review_download_task_action": DownloadTaskReceipt,
    }.get(tool, MediaChangeReceipt)
    row = await model.get_or_none(key=key)
    if row and row.result:
        return _outcome({**row.result, "retryable": False})
    if row and getattr(row, "claimed", True):
        recent = row.updated_at >= timezone.now() - timedelta(minutes=10)
        return {"status": "pending" if recent else "uncertain", "outcome": {}}
    return None


async def _newer(call: AssistantToolCall, preview: dict) -> bool:
    """Check whether a later review or question supersedes the frozen settings.

    Args:
        call: Completed tool call with a loaded originating turn.
        preview: Frozen domain facts of the original proposal.

    Returns:
        Whether another review now controls the same decision.
    """
    later = AssistantToolCall.filter(
        turn__conversation_id=call.turn.conversation_id, id__gt=call.id
    )
    if call.name == "validate_download_plan":
        # preferences requested after this check must feed a fresh validation
        return await later.filter(
            Q(name=call.name)
            | Q(name="ask_user", turn_id=call.turn_id, status="complete")
        ).exists()
    names = MEDIA_REVIEW_TOOLS if call.name in MEDIA_REVIEW_TOOLS else {call.name}
    rows = later.filter(name__in=names)
    if call.name != "review_workflow_schedule" and call.name not in MEDIA_REVIEW_TOOLS:
        return await rows.exists()
    for row in await rows.only("turn_id", "name", "result"):
        data = (row.result or {}).get("data") or {}
        if call.name == "review_workflow_schedule":
            if "valid" in data and schedule_review_target(
                data
            ) == schedule_review_target(preview):
                return True
        elif not is_media_file_listing(row, call, preview):
            return True
    return False


async def _apply(call: AssistantToolCall, preview: dict, user: UserInfo) -> dict:
    """Apply a frozen review through the same idempotent service used by chat.

    Args:
        call: Server-owned original tool call.
        preview: The validated settings that the user approved.
        user: Currently authorized administrator.

    Returns:
        The domain's authoritative outcome.
    """
    request_id = UUID(preview["request_id"])
    arguments = {"request_id": request_id, "confirmed": True}
    conv = call.turn.conversation_id
    if call.name == "review_workflow_schedule":
        return await apply_schedule(ScheduleConfirm(**arguments), conv, user)
    if call.name == "review_download_task_action":
        return await apply_task_action(TaskActionConfirm(**arguments), conv, user)
    if call.name in MEDIA_REVIEW_TOOLS:
        return await apply_media_change(MediaConfirm(**arguments), conv, user)
    setup = preview.get("ingestion")
    binding = (
        {key: setup[key] for key in ("workflow_id", "revision")} if setup else None
    )
    if call.name == "validate_download_plan":
        return await DownloadPlanService.create_once(
            DownloadPlanCreate(**arguments, plan=preview["plan"], ingestion=binding),
            user,
            review_call_id=call.call_id,
        )
    checked = DownloadTaskCheck.model_validate_json(call.arguments)
    return await DownloadTaskService.create_once(
        DownloadTaskCreate(
            **arguments,
            **checked.model_dump(exclude={"ingest_workflow_id", "transfer_samples"}),
            info_hash=preview["download"]["info_hash"],
            ingestion=binding,
        ),
        user,
    )


async def _load_browser_review(
    call_id: str,
    names: Collection[str],
    user: UserInfo,
    decision: PlanReviewDecision | None,
    *,
    check_allowed: bool,
    reserve_lock: bool,
) -> tuple[AssistantToolCall, dict, UUID, str]:
    """Verify browser administrator ownership and compute the shared receipt key.

    Args:
        call_id: Server-issued tool call identifier.
        names: Accepted review tool names for this endpoint.
        user: Authenticated browser account owning the conversation.
        decision: Submitted decision payload, or `None` when refreshing status.
        check_allowed: Whether to recheck historical turn resource access.
        reserve_lock: Whether to acquire the SQLite writer lock before reading.

    Returns:
        The originating tool call, frozen preview data, request UUID, and
        account-scoped receipt key.

    Raises:
        ForbiddenException: If browser administrator access is unavailable.
        NotFoundException: If no valid owned review matches `call_id`.
        BadRequestException: If `decision.request_id` differs from the preview.
    """
    if reserve_lock:
        # obtain the SQLite writer before checking a competing decision's result
        await reserve_writer(
            AssistantToolCall.filter(
                call_id=call_id,
                turn__conversation__user_id=user.id,
            )
        )
    await require_browser_admin(user)
    call, preview, request_id = await load_owned_review(
        call_id,
        names,
        user,
        expected_request_id=decision.request_id if decision is not None else None,
        check_allowed=check_allowed,
    )
    return (
        call,
        preview,
        request_id,
        DownloadPlanService.change_key(user.id, request_id),
    )


async def recent_plan_reviews(
    conversation_id: int,
) -> list[tuple[AssistantToolCall, dict]]:
    """Load the review window shared by browser and text plan decisions.

    Args:
        conversation_id: Server-owned conversation identity.

    Returns:
        Stored plan reviews in descending creation order, including failed
        validations.
    """
    return await recent_review_calls(
        conversation_id, ("review_download_plan_change",), require_valid=True
    )


def _plan_review_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    """Map a domain plan-change apply result to the browser plan-review shape.

    Args:
        outcome: Result returned by `DownloadPlanService.apply_change`.

    Returns:
        Status fields, marking pre-commit failures retryable while preserving
        non-retryable uncertain outcomes.
    """
    if outcome.get("valid") is False:
        return {
            "status": "failed",
            "retryable": True,
            "issues": outcome.get("issues", []),
        }
    status = outcome.get("outcome")
    if not status:
        status = next(
            (
                name
                for name in ("stale", "pending", "failed", "conflict")
                if outcome.get(name)
            ),
            "superseded",
        )
    return {
        "status": status,
        **({"retryable": True} if status == "failed" else {}),
    }


async def _apply_plan_review(
    call: AssistantToolCall,
    preview: dict,
    request_id: UUID,
    key: str,
    user: UserInfo,
) -> dict[str, Any]:
    """Dispatch an approved plan update or deletion and classify any failure.

    Args:
        call: Server-owned original review tool call.
        preview: Frozen plan-change preview approved by the user.
        request_id: Validated request identifier.
        key: Account-scoped receipt key.
        user: Currently authorized administrator.

    Returns:
        Status payload reflecting a committed receipt, retryable pre-commit
        failure, or non-retryable uncertain outcome.

    Raises:
        ForbiddenException: If administrator access was revoked before commit.
    """
    try:
        outcome = await DownloadPlanService.apply_change(
            DownloadPlanApply(
                request_id=request_id,
                revision=preview["revision"],
                change=DownloadPlanChange.model_validate_json(call.arguments),
                confirmed=True,
            ),
            user,
        )
    except NotFoundException:
        return {"status": "stale"}
    except ForbiddenException:
        raise
    except Exception:
        # inspect storage once before reporting an uncertain post-dispatch state
        try:
            receipt = await DownloadPlanReceipt.get_or_none(key=key)
        except Exception:
            receipt = None
        if receipt is not None:
            return {"status": receipt.result["outcome"]}
        return {"status": "uncertain"}
    return _plan_review_outcome(outcome)


async def plan_review(
    call_id: str, user: UserInfo, decision: PlanReviewDecision | None = None
) -> dict[str, Any]:
    """Read or decide the account's recorded subscription update or deletion.

    The existing chat and MCP apply path checks the same unique receipt. A
    cancellation competes with an application in the database: the mutation
    and its receipt commit together, so only one decision can win.

    Args:
        call_id: Original server-assigned tool call.
        user: Authenticated browser administrator.
        decision: Exact review and decision, or `None` to refresh status.

    Returns:
        The stored review and current decision state, including a retryable
        failure if a change could not be saved before commit.

    Raises:
        ForbiddenException: If the current account cannot decide from the browser.
        NotFoundException: If the owned tool call has no valid recorded review.
        BadRequestException: If the submitted decision identifies another review.
    """
    async with in_transaction():
        call, preview, request_id, key = await _load_browser_review(
            call_id,
            ("review_download_plan_change",),
            user,
            decision,
            check_allowed=False,
            reserve_lock=decision is not None and not decision.approve,
        )
        result = {
            "request_id": str(request_id),
            "plan_id": preview["plan_id"],
            "action": preview["action"],
            "keyword": preview["before"]["keyword"],
            "workflow_name": preview["workflow_name"],
            "changes": preview["changes"],
        }
        receipt = await DownloadPlanReceipt.get_or_none(key=key)
        if receipt is not None:
            return {**result, "status": receipt.result["outcome"]}
        if call.turn.status != "complete":
            return {**result, "status": "incomplete"}
        reviews = await recent_plan_reviews(call.turn.conversation_id)
        newest = next(
            (row for row, data in reviews if data.get("plan_id") == preview["plan_id"]),
            None,
        )
        if newest is None or newest.id != call.id:
            return {**result, "status": "superseded"}
        current = await DownloadPlan.get_or_none(id=preview["plan_id"])
        if current is None:
            return {**result, "status": "absent"}
        if DownloadPlanService.revision(current) != preview["revision"]:
            return {**result, "status": "stale"}
        if decision is None:
            return {**result, "status": "reviewed"}
        if not decision.approve:
            receipt, _ = await DownloadPlanReceipt.get_or_create(
                key=key,
                defaults={
                    "action": preview["action"],
                    "plan_id": preview["plan_id"],
                    "graph_id": preview["workflow_id"],
                    "result": {
                        "action": preview["action"],
                        "plan_id": preview["plan_id"],
                        "applied": False,
                        "outcome": "cancelled",
                    },
                },
            )
            return {**result, "status": receipt.result["outcome"]}
    return {**result, **await _apply_plan_review(call, preview, request_id, key, user)}


async def assistant_review(
    call_id: str, user: UserInfo, decision: PlanReviewDecision | None = None
) -> dict:
    """Read the current proposal or claim the user's exact browser decision.

    Args:
        call_id: Server-issued ID of the reviewed tool call.
        user: Account owning the original conversation.
        decision: Frozen request identity and decision, or `None` to read.

    Returns:
        Original preview and up-to-date status of the shared operation.

    Raises:
        ForbiddenException: If the current browser account lost its grant.
        NotFoundException: If this account has no valid review with that identity.
        BadRequestException: If the displayed and submitted request identities differ.
    """
    async with in_transaction():
        call, preview, request_id, key = await _load_browser_review(
            call_id,
            REVIEW_TOOLS,
            user,
            decision,
            check_allowed=True,
            reserve_lock=decision is not None,
        )
        base = {"tool": call.name, "request_id": str(request_id), "preview": preview}
        if done := await _recorded(call.name, key, user):
            return {**base, **done}
        row = await AssistantReviewDecision.get_or_none(
            owner_id=user.id, request_id=request_id
        )
        if row and not row.approved:
            return {**base, "status": "cancelled"}
        if row and not (row.result or {}).get("retryable"):
            if row.result and row.result.get("status") != "pending":
                return {**base, **row.result}
            uncertain = row.updated_at < timezone.now() - timedelta(minutes=10)
            return {**base, "status": "uncertain" if uncertain else "pending"}
        if call.turn.status != "complete":
            return {**base, "status": "incomplete"}
        if await _newer(call, preview):
            return {**base, "status": "superseded"}
        if call.name == "validate_download_plan" and (
            preview.get("assistant_submission") or {}
        ).get("status") in {
            "needs_confirmation",
            "needs_settings",
            "unverified_term",
            "needs_preferences",
            "no_matches",
        }:
            return {**base, "status": "incomplete"}
        if not decision:
            return {**base, **(row.result if row else {"status": "reviewed"})}
        if not decision.approve:
            row = await AssistantReviewDecision.claim(user.id, request_id, False)
            return {**base, "status": "pending" if row.approved else "cancelled"}
    # the domain claims approval after its checks, together with every other channel
    try:
        result = _outcome(await _apply(call, preview, user))
    except (NotFoundException, BadRequestException):
        result = {"status": "stale", "outcome": {}}
    except ForbiddenException:
        raise
    except Exception:
        row = await AssistantReviewDecision.get_or_none(
            owner_id=user.id, request_id=request_id
        )
        if row and row.result and row.result.get("status") != "pending":
            return {**base, **row.result}
        result = {"status": "uncertain", "outcome": {}}
    return {**base, **result}
