"""Resolve browser decisions against the assistant's stored plan reviews."""

from typing import Any

from tortoise.transactions import in_transaction

from app.core.decorators import require_browser_admin
from app.core.exceptions import NotFoundException
from app.models.assistant import AssistantToolCall
from app.models.download import (
    DownloadPlan,
    DownloadPlanApply,
    DownloadPlanChange,
    DownloadPlanReceipt,
    PlanReviewDecision,
)
from app.models.user import UserInfo
from app.services.download import DownloadPlanService
from app.services.review_common import (
    load_owned_review,
    recent_review_calls,
    reserve_writer,
)


async def recent_plan_reviews(
    conversation_id: int,
) -> list[tuple[AssistantToolCall, dict]]:
    """Load the review window shared by browser and text decisions.

    Args:
        conversation_id: Server-owned conversation identity.

    Returns:
        Stored reviews in descending creation order, including failed validation.
    """
    return await recent_review_calls(
        conversation_id, ("review_download_plan_change",), require_valid=True
    )


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
        failure if a change could not be saved.

    Raises:
        ForbiddenException: If the current account cannot decide from the browser.
        NotFoundException: If the owned tool call has no valid recorded review.
        BadRequestException: If the submitted decision identifies another review.
    """
    if decision is not None and not decision.approve:
        async with in_transaction():
            await reserve_writer(
                AssistantToolCall.filter(
                    call_id=call_id, turn__conversation__user_id=user.id
                )
            )
            return await _plan_review(call_id, user, decision)
    return await _plan_review(call_id, user, decision)


async def _plan_review(
    call_id: str,
    user: UserInfo,
    decision: PlanReviewDecision | None,
) -> dict[str, Any]:
    """Recheck the recorded review and resolve a browser decision.

    Args:
        call_id: Server-issued tool call in the user's conversation.
        user: Authenticated user submitting this request.
        decision: Optional decision, under the SQLite writer lock for cancellation.

    Returns:
        Live approval state or the decision's durable outcome.

    Raises:
        NotFoundException: If this account has no valid preview.
        BadRequestException: If the submitted request differs from the shown review.
    """
    await require_browser_admin(user)
    call, preview, request_id = await load_owned_review(
        call_id,
        ("review_download_plan_change",),
        user,
        expected_request_id=decision.request_id if decision is not None else None,
    )
    key = DownloadPlanService.change_key(user.id, request_id)
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
    if decision is not None and not decision.approve:
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
        return {**result, "status": "stale"}
    if outcome.get("valid") is False:
        return {
            **result,
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
        **result,
        "status": status,
        **({"retryable": True} if status == "failed" else {}),
    }
