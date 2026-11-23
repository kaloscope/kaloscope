"""Review, approve, and recover atomic replacements of download plans."""

import sqlite3
from datetime import timedelta
from uuid import uuid4

from pydantic import ValidationError
from sanic.log import logger
from tortoise import timezone
from tortoise.exceptions import OperationalError
from tortoise.transactions import in_transaction

from app.core.decorators import require_browser_admin
from app.core.dl.syncer import notify_download_changes
from app.core.exceptions import NotFoundException
from app.models.download import (
    DownloadPlan,
    DownloadPlanCheck,
    DownloadPlanDispatch,
    DownloadPlanHistory,
    DownloadPlanReplacement,
)
from app.models.user import User, UserInfo, UserRole
from app.services.download import DownloadPlanService
from app.services.review_common import (
    get_or_create_operation_review,
    load_operation_review,
    lock_operation_decision,
    require_live_admin,
    reserve_writer,
)
from app.utils.json import dumps, json_digest, loads


class PlanReplacementService:
    """Bind an administrator's approval to an immutable replacement preview."""

    @classmethod
    async def _preview(
        cls,
        plan: DownloadPlan,
        keep_history: bool,
        run_now: bool,
        user: UserInfo | User,
    ) -> dict:
        """Validate the current subscription and capture its timing and history.

        Args:
            plan: Stored subscription.
            keep_history: Keep its deduplication records and count.
            run_now: Request an immediate check if the time window and limits allow.
            user: Current administrator.

        Returns:
            A serializable preview with all facts used to detect a stale review.
        """
        settings = DownloadPlanService.settings(plan)
        anchor = plan.last_exec or plan.created_at
        first_queued = bool(plan.creation_key and plan.last_exec is None)
        due = plan.replacement_due or (
            anchor if first_queued else anchor + timedelta(hours=plan.interval_num)
        )
        if plan.interval_start:
            due = max(due, plan.interval_start)
        await plan.fetch_related("graph", "downloader", "transfer_lib")
        before = {
            **settings,
            "workflow_name": plan.graph.name,
            "downloader_name": plan.downloader.name,
            "transfer_lib_name": plan.transfer_lib.name if plan.transfer_lib else None,
            "total_count": plan.total_count,
            "last_exec": plan.last_exec,
            "history_count": await DownloadPlanHistory.filter(plan_id=plan.id).count(),
            "next_due": due,
        }
        after = dict(before)
        if not keep_history:
            after.update(total_count=0, history_count=0)
        now = timezone.now()
        if plan.total_limit is not None and after["total_count"] >= plan.total_limit:
            after.update(schedule="blocked", blocked_by="limit_reached", next_due=None)
        elif (run_now or (first_queued and due <= now)) and (
            (not plan.interval_start or now >= plan.interval_start)
            and (not plan.interval_end or now <= plan.interval_end)
        ):
            after.update(schedule="immediate", next_due=None)
        elif plan.interval_end and due > plan.interval_end:
            after.update(schedule="blocked", blocked_by="outside_window", next_due=None)
        else:
            after["schedule"] = "scheduled"
        try:
            checked = await DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=settings), user, check_duplicate=False
            )
            issues = checked["issues"]
        except ValidationError:
            issues = [{"path": "plan", "message": "Stored plan settings are invalid."}]
        return loads(
            dumps(
                {
                    "valid": not issues,
                    "action": "replace",
                    "plan_id": plan.id,
                    "workflow_id": plan.graph_id,
                    "before": before,
                    "after": after,
                    "issues": issues,
                    "keep_history": keep_history,
                    "run_now": run_now,
                    "revision": json_digest(
                        {"before": before, "schedule": after["schedule"]}
                    ),
                }
            )
        )

    @classmethod
    async def review(
        cls,
        plan_id: int,
        *,
        keep_history: bool = True,
        run_now: bool = False,
        user: UserInfo,
    ) -> dict:
        """Prepare an account-owned replacement without changing the subscription.

        Args:
            plan_id: Original plan ID.
            keep_history: Copy the count and reassign deduplication records.
            run_now: Check immediately when the subscription is eligible.
            user: Authenticated administrator.

        Returns:
            Frozen preview, digest, expiry and operation ID, or validation issues.

        Raises:
            ForbiddenException: If current editor access is unavailable.
            NotFoundException: If the plan is unavailable.
        """
        await require_live_admin(user)
        plan = await DownloadPlan.get_or_none(id=plan_id)
        if not plan:
            raise NotFoundException
        preview = await cls._preview(plan, keep_history, run_now, user)
        if not preview["valid"]:
            return {**preview, "status": "invalid"}
        digest = json_digest(preview)
        async with in_transaction():
            operation = await get_or_create_operation_review(
                DownloadPlanReplacement,
                owner_id=user.id,
                digest=digest,
                active_statuses=("pending",),
                extra_filters={"plan_id": plan.id},
                defaults={
                    "plan_id": plan_id,
                    "operation_id": str(uuid4()),
                    "preview": preview,
                },
            )
        return await cls._result(operation)

    @staticmethod
    async def _result(operation: DownloadPlanReplacement) -> dict:
        """Describe a stored review and any committed first check.

        Args:
            operation: Account-owned replacement.

        Returns:
            Current decision state and separate first-check outcome.
        """
        result = {
            **operation.preview,
            "operation_id": operation.operation_id,
            "digest": operation.digest,
            "expires_at": operation.expires_at.isoformat(),
            "status": operation.status,
            "new_plan_id": operation.new_plan_id,
        }
        if operation.new_plan_id:
            result["first_execution"] = operation.first_execution
        return result

    @classmethod
    async def status(cls, operation_id: str, user: UserInfo) -> dict:
        """Read the latest decision and execution state without applying a change.

        Args:
            operation_id: Opaque review identity.
            user: Authenticated administrator.

        Returns:
            Current state including expiry and a committed replacement's new ID.
        """
        operation = await load_operation_review(
            DownloadPlanReplacement,
            operation_id,
            user,
            expirable_statuses=("pending",),
        )
        return await cls._result(operation)

    @classmethod
    async def decide(
        cls, operation_id: str, digest: str, *, approve: bool, user: UserInfo
    ) -> dict:
        """Accept a trusted user decision once, or cancel an uncommitted replacement.

        Args:
            operation_id: Opaque review identity.
            digest: Exact digest shown in the trusted interface.
            approve: Whether to apply the frozen change.
            user: Authenticated approving administrator.

        Returns:
            Decision and execution outcome, including outstanding submissions.

        Raises:
            BadRequestException: If the digest does not identify the displayed change.
            ForbiddenException: If the caller cannot approve this replacement.
            NotFoundException: If the review is unavailable.
        """
        await require_browser_admin(user)
        async with in_transaction():
            operation = await lock_operation_decision(
                DownloadPlanReplacement,
                operation_id,
                digest,
                user,
                lock_statuses=("pending", "approved", "uncertain"),
                expirable_statuses=("pending",),
            )
            previous = operation.status
            if operation.status in {"pending", "approved", "uncertain"} and not approve:
                operation.status = "cancelled"
            elif operation.status == "pending":
                busy = await DownloadPlanReplacement.filter(
                    plan_id=operation.plan_id, status__in=["approved", "uncertain"]
                ).exists()
                operation.status = "conflict" if busy else "approved"
            if previous != operation.status:
                await DownloadPlanReplacement.filter(
                    id=operation.id, status=previous
                ).update(status=operation.status, decided_at=timezone.now())
        if operation.status in {"approved", "uncertain"}:
            await cls._advance(operation)
        return await cls.status(operation_id, user)

    @classmethod
    async def _advance(cls, operation: DownloadPlanReplacement) -> None:
        """Commit one authorized replacement after all old submissions are settled.

        Args:
            operation: Persisted approved or uncertain replacement.
        """
        try:
            await cls._switch(operation.id)
        except Exception as error:
            cause = error.__context__
            if (
                isinstance(error, OperationalError)
                and isinstance(cause, sqlite3.OperationalError)
                and (getattr(cause, "sqlite_errorcode", 0) & 255)
                in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            ):
                # the next recovery pass can continue the approved request
                return
            # the switching transaction rolled back; let the old generation run again
            logger.exception(
                "Could not switch download plan replacement %s", operation.id
            )
            await DownloadPlanReplacement.filter(
                id=operation.id, status__in=["approved", "uncertain"]
            ).update(status="failed")

    @classmethod
    async def _switch(cls, operation_pk: int) -> None:
        """Revalidate and switch within a single database transaction.

        Args:
            operation_pk: Internal identity of an approved replacement.
        """
        async with in_transaction():
            update = DownloadPlanReplacement.filter(
                id=operation_pk, status__in=["approved", "uncertain"]
            )
            if not await reserve_writer(update):
                return
            operation = await DownloadPlanReplacement.get(id=operation_pk)
            owner = await User.get_or_none(id=operation.owner_id, role=UserRole.ADMIN)
            plan = await DownloadPlan.get_or_none(id=operation.plan_id)
            if owner is None or plan is None:
                await update.update(status="conflict")
                return
            if await DownloadPlanDispatch.exists(plan_id=plan.id):
                await update.update(status="uncertain")
                return
            preview = operation.preview
            current = await cls._preview(
                plan, preview["keep_history"], preview["run_now"], owner
            )
            if not current["valid"] or operation.digest != json_digest(current):
                await update.update(status="conflict")
                return
            now = timezone.now()
            schedule = preview["after"]["schedule"]
            # keep the periodic anchor in case the user subsequently lifts a limit
            due = now if schedule == "immediate" else preview["before"]["next_due"]
            first = {"status": "queued" if schedule == "immediate" else schedule}
            if schedule == "blocked":
                first["reason"] = preview["after"]["blocked_by"]
            new = await DownloadPlan.create(
                **DownloadPlanService.settings(plan),
                last_exec=plan.last_exec,
                replacement_due=due,
                total_count=preview["after"]["total_count"],
                creation_receipt={
                    **{
                        k: v
                        for k, v in (plan.creation_receipt or {}).items()
                        if k in {"release_rule", "ingest_workflow_id"}
                    },
                    "first_execution": first,
                },
            )
            if preview["keep_history"]:
                await DownloadPlanHistory.filter(plan_id=plan.id).update(plan_id=new.id)
            await plan.delete()
            await update.update(
                new_plan_id=new.id, status="applied", first_execution=first
            )
            await DownloadPlanReplacement.filter(
                plan_id=plan.id, status="pending"
            ).update(status="conflict")
        notify_download_changes()

    @classmethod
    async def recover(cls) -> None:
        """Resume approved replacements after a restart or synchronization signal."""
        for operation in await DownloadPlanReplacement.filter(
            status__in=["approved", "uncertain"]
        ):
            await cls._advance(operation)
