"""Share mechanical review locking, ownership, and supersession helpers."""

from collections.abc import Collection
from uuid import UUID

from tortoise.expressions import F
from tortoise.queryset import QuerySet

from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    NotFoundException,
)
from app.models.assistant import AssistantToolCall
from app.models.user import User, UserInfo, UserRole


async def reserve_writer(queryset: QuerySet, field: str = "updated_at") -> int:
    """Acquire SQLite's deferred-transaction writer lock before a read-then-write.

    Args:
        queryset: Scoped query targeting the row or table participating in the write.
        field: Existing column written to itself without changing its stored value.

    Returns:
        Number of matching rows updated by the no-op write.
    """
    return await queryset.update(**{field: F(field)})


async def require_live_admin(user: UserInfo) -> None:
    """Verify the caller currently holds an administrator role in storage.

    Args:
        user: Authenticated account supplied by the request transport.

    Raises:
        ForbiddenException: With `ErrorCode.PERMISSION_DENIED` when the caller
            identity is not an administrator, or `ErrorCode.FORBIDDEN` when an
            administrator session was demoted in storage.
    """
    if user.role != UserRole.ADMIN:
        raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
    if not await User.exists(id=user.id, role=UserRole.ADMIN):
        raise ForbiddenException


async def recent_review_calls(
    conversation_id: int,
    names: Collection[str],
    *,
    require_valid: bool = False,
) -> list[tuple[AssistantToolCall, dict]]:
    """Load the recent review window shared by chat and browser decision paths.

    Args:
        conversation_id: Server-owned conversation identity.
        names: Review tool names belonging to the same supersession window.
        require_valid: Whether to include only records whose result data contains
            a `valid` field.

    Returns:
        Stored tool calls and their data dictionaries in descending creation order.
    """
    rows = (
        await AssistantToolCall.filter(
            turn__conversation_id=conversation_id, name__in=list(names)
        )
        .order_by("-id")
        .limit(30)
    )
    reviews: list[tuple[AssistantToolCall, dict]] = []
    for record in rows:
        data = (record.result or {}).get("data") or {}
        if not require_valid or "valid" in data:
            reviews.append((record, data))
    return reviews


async def load_owned_review(
    call_id: str,
    names: Collection[str],
    user: UserInfo,
    *,
    expected_request_id: UUID | None = None,
    check_allowed: bool = False,
) -> tuple[AssistantToolCall, dict, UUID]:
    """Load a completed valid review owned by the caller and check its request ID.

    Args:
        call_id: Server-issued tool call identifier.
        names: Accepted review tool names.
        user: Authenticated browser account owning the conversation.
        expected_request_id: Submitted request ID that must match the frozen preview.
        check_allowed: Whether to verify historical turn resource access before
            checking `expected_request_id`.

    Returns:
        The originating tool call with its turn loaded, frozen preview data, and
        validated request UUID.

    Raises:
        NotFoundException: If no completed valid review exists for this account.
        ForbiddenException: If `check_allowed` fails for the originating turn.
        BadRequestException: If `expected_request_id` differs from the preview.
    """
    call = (
        await AssistantToolCall.filter(
            call_id=call_id,
            name__in=names,
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
    if check_allowed:
        from app.services.assistant_sessions import AssistantSessionService

        if not await AssistantSessionService.allowed(call.turn.resource_ids, user):
            raise ForbiddenException
    request_id = UUID(preview["request_id"])
    if expected_request_id is not None and request_id != expected_request_id:
        raise BadRequestException
    return call, preview, request_id


def schedule_review_target(data: dict) -> tuple[int | None, int | None]:
    """Identify the schedule or workflow target controlled by a schedule review.

    Args:
        data: Frozen review payload returned by `review_workflow_schedule`.

    Returns:
        Tuple of `(job_id, None)` for an existing schedule or `(None, workflow_id)`
        for a new schedule.
    """
    job_id = data.get("job_id")
    return job_id, None if job_id else data.get("workflow_id")


def is_media_file_listing(
    later: AssistantToolCall, review: AssistantToolCall, preview: dict
) -> bool:
    """Check whether a later call is only a same-turn file listing for the same library.

    Args:
        later: Subsequent media review tool call in the same conversation.
        review: Original media review being evaluated for supersession.
        preview: Frozen data of the original media review.

    Returns:
        Whether `later` only adds file facts without replacing `review`.
    """
    later_result = later.result or {}
    later_data = later_result.get("data") or {}
    return bool(
        later.turn_id == review.turn_id
        and later.name == "review_media_batch"
        and later_result.get("ok")
        and "matched" in later_data
        and "operation" not in later_data
        and isinstance(preview.get("library_id"), int)
        and later_data.get("library_id") == preview["library_id"]
    )
