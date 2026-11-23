"""Share mechanical review locking, ownership, lifecycle, and supersession helpers."""

from collections.abc import Callable, Collection
from datetime import timedelta
from typing import Any
from uuid import UUID

from tortoise import timezone
from tortoise.expressions import F
from tortoise.models import Model
from tortoise.queryset import QuerySet

from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    NotFoundException,
)
from app.models.assistant import AssistantToolCall
from app.models.user import User, UserInfo, UserRole

REVIEW_TTL = timedelta(minutes=30)


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


async def get_or_create_operation_review[T: Model](
    model: type[T],
    *,
    owner_id: int,
    digest: str,
    active_statuses: Collection[str],
    defaults: dict[str, Any],
    extra_filters: dict[str, Any] | None = None,
    ttl: timedelta = REVIEW_TTL,
    reserve: bool = True,
) -> T:
    """Reuse an unexpired operation review for the same digest or create one.

    Callers should invoke this inside an active database transaction.

    Args:
        model: ORM model storing durable operation reviews.
        owner_id: Account owning the review.
        digest: Canonical digest of the frozen review payload.
        active_statuses: Review statuses eligible for reuse while unexpired.
        defaults: Model fields populated when inserting a new review.
        extra_filters: Optional scope filters narrowing active review lookup.
        ttl: Expiry window applied when creating a new review.
        reserve: Whether to acquire the SQLite writer lock on the active query
            before reading.

    Returns:
        Existing unexpired review or the newly persisted row.
    """
    active = model.filter(
        owner_id=owner_id,
        digest=digest,
        status__in=list(active_statuses),
        expires_at__gt=timezone.now(),
        **(extra_filters or {}),
    )
    if reserve:
        # SQLite uses deferred transactions; reserve the writer before lookup
        await reserve_writer(active)
    row = await active.first()
    if row is None:
        row = await model.create(
            owner_id=owner_id,
            digest=digest,
            expires_at=timezone.now() + ttl,
            **defaults,
        )
    return row


async def load_operation_review[T: Model](
    model: type[T],
    operation_id: UUID | str,
    user: UserInfo,
    *,
    expirable_statuses: Collection[str],
    allowed: Callable[[T], bool] | None = None,
    stale_executing_after: timedelta | None = None,
) -> T:
    """Load an administrator-owned operation review and settle due expirations.

    Args:
        model: ORM model storing durable operation reviews.
        operation_id: Opaque review identifier.
        user: Authenticated administrator reading the review.
        expirable_statuses: Statuses that transition to `expired` once
            `expires_at` has passed.
        allowed: Optional channel or domain predicate that must hold for the row.
        stale_executing_after: Optional duration after which an `executing`
            review transitions to `uncertain`.

    Returns:
        The refreshed owned review row.

    Raises:
        ForbiddenException: If the caller is no longer a live administrator.
        NotFoundException: If no matching review is accessible to `user`.
    """
    await require_live_admin(user)
    query = model.filter(owner_id=user.id, operation_id=operation_id)
    row = await query.first()
    if row is None or (allowed is not None and not allowed(row)):
        raise NotFoundException
    now = timezone.now()
    refresh = False
    if row.status in expirable_statuses and row.expires_at <= now:
        await query.filter(
            status__in=list(expirable_statuses), expires_at__lte=now
        ).update(status="expired")
        refresh = True
    if (
        stale_executing_after is not None
        and row.status == "executing"
        and row.updated_at < now - stale_executing_after
    ):
        await query.filter(
            status="executing",
            updated_at__lt=now - stale_executing_after,
        ).update(status="uncertain")
        refresh = True
    if refresh:
        await row.refresh_from_db()
    return row


async def lock_operation_decision[T: Model](
    model: type[T],
    operation_id: UUID | str,
    digest: str,
    user: UserInfo,
    *,
    lock_statuses: Collection[str],
    expirable_statuses: Collection[str],
    allowed: Callable[[T], bool] | None = None,
    stale_executing_after: timedelta | None = None,
) -> T:
    """Acquire the SQLite writer lock, expire due state, and verify digest.

    Callers should verify browser administrator access and open an active
    database transaction before calling this helper.

    Args:
        model: ORM model storing durable operation reviews.
        operation_id: Opaque review identifier.
        digest: Digest displayed on the trusted confirmation surface.
        user: Authenticated browser administrator submitting the decision.
        lock_statuses: Active statuses scoped for writer reservation.
        expirable_statuses: Statuses that expire when `expires_at` has passed.
        allowed: Optional channel or domain predicate that must hold for the row.
        stale_executing_after: Optional timeout marking stuck `executing` rows
            as `uncertain`.

    Returns:
        The locked, refreshed operation review matching `digest`.

    Raises:
        ForbiddenException: If the caller is no longer a live administrator.
        NotFoundException: If no accessible review exists for `operation_id`.
        BadRequestException: If `digest` differs from the frozen review digest.
    """
    await reserve_writer(
        model.filter(
            operation_id=operation_id,
            owner_id=user.id,
            status__in=list(lock_statuses),
        )
    )
    row = await load_operation_review(
        model,
        operation_id,
        user,
        expirable_statuses=expirable_statuses,
        allowed=allowed,
        stale_executing_after=stale_executing_after,
    )
    if row.digest != digest:
        raise BadRequestException
    return row
