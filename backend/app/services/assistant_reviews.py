"""Bind browser decisions to the exact settings shown by an assistant review."""

from datetime import timedelta
from uuid import UUID

from tortoise import timezone
from tortoise.expressions import F, Q
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
    DownloadPlanCreate,
    DownloadPlanCreation,
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
from app.services.assistant_sessions import AssistantSessionService
from app.services.download import DownloadPlanService, DownloadTaskService

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
            return _outcome({"created": True, "plan_id": row.plan_id})
        return _outcome(
            await DownloadPlanService.creation_result(plan, user, replayed=True)
        )
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
            if (
                "valid" in data
                and (data.get("job_id") or data.get("workflow_id"))
                == (preview.get("job_id") or preview.get("workflow_id"))
                and bool(data.get("job_id")) == bool(preview.get("job_id"))
            ):
                return True
        elif not (
            row.turn_id == call.turn_id
            and row.name == "review_media_batch"
            and (row.result or {}).get("ok")
            and "matched" in data
            and "operation" not in data
            and preview.get("library_id") == data.get("library_id")
        ):
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
        if decision:
            # obtain the SQLite writer before checking a competing decision's result
            await AssistantToolCall.filter(
                call_id=call_id,
                turn__conversation__user_id=user.id,
            ).update(updated_at=F("updated_at"))
        await require_browser_admin(user)
        call = (
            await AssistantToolCall.filter(
                call_id=call_id,
                name__in=REVIEW_TOOLS,
                status="complete",
                turn__conversation__user_id=user.id,
            )
            .select_related("turn")
            .first()
        )
        preview = ((call.result or {}).get("data") or {}) if call else {}
        if not (
            call
            and (call.result or {}).get("ok")
            and preview.get("valid")
            and preview.get("request_id")
        ):
            raise NotFoundException
        if not await AssistantSessionService.allowed(call.turn.resource_ids, user):
            raise ForbiddenException
        request_id = UUID(preview["request_id"])
        if decision and request_id != decision.request_id:
            raise BadRequestException
        base = {"tool": call.name, "request_id": str(request_id), "preview": preview}
        key = DownloadPlanService.change_key(user.id, request_id)
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
