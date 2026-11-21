"""Coordinate confirmed review attempts and cancellation across entry points."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Self
from uuid import UUID, uuid4

from tortoise.expressions import F
from tortoise.fields import BooleanField, IntField, JSONField, UUIDField
from tortoise.transactions import in_transaction

from app.core.exceptions import ForbiddenException
from app.models.base import TortoiseModel
from app.models.user import User, UserRole

_attempt: ContextVar[str | None] = ContextVar("review_execution", default=None)


@contextmanager
def review_attempt() -> Iterator[None]:
    """Bind one domain execution to the receipt it can release.

    Yields:
        Control under the request lock, including to any shielded child task.
    """
    token = _attempt.set(uuid4().hex)
    try:
        yield
    finally:
        _attempt.reset(token)


class AssistantReviewDecision(TortoiseModel):
    """Keep an account's decision until its attempted change is known to have failed."""

    owner_id = IntField()
    request_id = UUIDField()
    approved = BooleanField()
    result = JSONField(null=True)

    class Meta:
        table = "assistant_review_decision"
        unique_together = (("owner_id", "request_id"),)

    @classmethod
    async def claim(cls, owner_id: int, request_id: UUID | str, approve: bool) -> Self:
        """Claim a new decision or retry a failure that had no side effects.

        Args:
            owner_id: Authenticated account that owns the review.
            request_id: Stable UUID shared by all entry points for the review.
            approve: Whether this caller confirms the operation.

        Returns:
            The winning decision, including when this caller requested its opposite.

        Raises:
            ValueError: If the supplied string does not represent a UUID.
            ForbiddenException: If the deciding account lost administrator access.
        """
        identity = {"owner_id": owner_id, "request_id": UUID(str(request_id))}
        attempt = _attempt.get()
        pending = (
            {"status": "pending", "attempt": attempt} if approve and attempt else None
        )
        async with in_transaction():
            query = cls.filter(**identity)
            # serialize a retry against cancellation without refreshing the lease
            await query.update(updated_at=F("updated_at"))
            if not await User.exists(id=owner_id, role=UserRole.ADMIN):
                raise ForbiddenException
            decision, _ = await cls.get_or_create(
                **identity, defaults={"approved": approve, "result": pending}
            )
            if decision.approved and (decision.result or {}).get("retryable") is True:
                decision.approved, decision.result = approve, pending
                await decision.save(update_fields=["approved", "result", "updated_at"])
            return decision

    @classmethod
    async def finish(cls, owner_id: int, request_id: UUID, result: dict) -> dict:
        """Record known failures before releasing the domain's request lock.

        Args:
            owner_id: Authenticated administrator that owns the operation.
            request_id: Identity used to claim this exact execution.
            result: Domain result after validation, commit or rollback.

        Returns:
            The domain result with retry eligibility for a definite failure.
        """
        if result.get("plan_error") or result.get("submit_error"):
            state = {
                "status": "partial",
                "retryable": False,
                "outcome": {"ingestion": result.get("ingestion")},
            }
        elif result.get("uncertain") or result.get("outcome") == "uncertain":
            state = {"status": "uncertain", "outcome": {}}
        elif result.get("stale") or result.get("outcome") == "absent":
            state = {"status": "stale", "outcome": {"outcome": result.get("outcome")}}
        elif (
            result.get("failed")
            or result.get("valid") is False
            or result.get("existing_download")
        ) and not result.get("replayed"):
            retryable = result.get("retryable", True)
            state = {
                "status": "failed",
                "retryable": retryable,
                "outcome": {
                    "issues": result.get("issues", []),
                    **(
                        {"duplicate_plan_id": result["duplicate_plan_id"]}
                        if "duplicate_plan_id" in result
                        else {}
                    ),
                },
            }
            result = {**result, "retryable": retryable}
        else:
            return result
        # a validator or an older completion cannot release another execution
        if attempt := _attempt.get():
            await cls.filter(
                owner_id=owner_id,
                request_id=request_id,
                approved=True,
                result={"status": "pending", "attempt": attempt},
            ).update(result=state)
        return result
