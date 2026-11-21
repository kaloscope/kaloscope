"""Describe persisted subscription, download, transfer, and media-index evidence."""

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator
from tortoise.expressions import Q

from app.core.dl.access import ADMIN_PREVIEW
from app.core.dl.driver import DownloadState
from app.core.exceptions import NotFoundException
from app.models.download import (
    DownloadPlan,
    DownloadPlanAttempt,
    DownloadTask,
    OfflineDownloadJob,
)
from app.models.flow import GraphState
from app.models.media import MediaEvent, MediaItem
from app.models.user import UserInfo


class IngestionQuery(BaseModel):
    """Select a subscription, a specific check, or a single download task."""

    model_config = ConfigDict(extra="forbid")
    plan_id: PositiveInt | None = None
    attempt_id: PositiveInt | None = None
    task_id: PositiveInt | None = None
    page: int = Field(ge=1, default=1)

    @model_validator(mode="after")
    def select_target(self) -> Self:
        """Select exactly one source for the diagnosis.

        Returns:
            The validated diagnostic request.

        Raises:
            ValueError: If the target is missing or ambiguous.
        """
        if (
            sum(v is not None for v in (self.plan_id, self.attempt_id, self.task_id))
            != 1
        ):
            raise ValueError("Supply exactly one plan_id, attempt_id or task_id.")
        return self


async def task_evidence(task: DownloadTask, page: int = 1) -> dict:
    """Read a task and the current index for a page of recorded transfer paths.

    Args:
        task: Download task selected after checking the caller's access.
        page: One-based destination page; each page contains up to 25 unique paths.

    Returns:
        Transfer, index, and pending event evidence without exposing source URLs.
    """
    targets = list(dict.fromkeys((task.transfer_targets or {}).values()))
    paths = targets[(page - 1) * 25 : page * 25]
    pending = task.transfer_pending or (
        task.state == DownloadState.COMPLETED
        and await OfflineDownloadJob.exists(
            download_id=task.id, completion_due_at__not_isnull=True
        )
    )
    result = {
        "task_id": task.id,
        "attempt_id": task.plan_attempt_id,
        "library_id": task.transfer_lib_id,
        "name": task.name,
        "download_state": str(task.state),
        "transfer_pending": pending,
        "transfer_failed": task.last_transfer_failed,
        "target_count": len(targets),
        "next_page": page + 1 if page * 25 < len(targets) else None,
    }
    if task.state == DownloadState.ERROR:
        stage = "download_failed"
    elif task.state in {DownloadState.SUBMIT_UNKNOWN, DownloadState.SUBMITTING}:
        stage = "submission_unconfirmed"
    elif task.state == DownloadState.PAUSED:
        stage = "download_paused"
    elif task.state != DownloadState.COMPLETED:
        stage = "download_in_progress"
    elif not task.transfer_lib_id or not task.transfer_method:
        stage = "no_transfer_configured"
    elif task.last_transfer_failed:
        stage = "transfer_failed"
    elif pending:
        stage = "transfer_pending"
    else:
        stage = "transfer_evidence_missing"
    if targets and stage == "transfer_evidence_missing":
        stage = "transfer_recorded"
    result["stage"] = stage
    missing = []
    if not task.plan_attempt_id:
        missing.append("source_plan_attempt")
    if task.last_transfer_failed:
        missing.append("transfer_failure_reason")
    if (
        task.transfer_lib_id
        and task.transfer_method
        and task.state == DownloadState.COMPLETED
        and not targets
    ):
        missing.append("transfer_destinations")
    result["missing_evidence"] = missing
    if paths and task.transfer_lib_id:
        rows = await MediaItem.filter(
            lib_id=task.transfer_lib_id, path__in=paths
        ).values("id", "path", "visible", "index_state")
        indexed = {row["path"]: row for row in rows}
        events = (
            await MediaEvent.filter(
                Q(src_path__in=paths) | Q(dest_path__in=paths),
                lib_id=task.transfer_lib_id,
            )
            .distinct()
            .values("src_path", "dest_path", "event_type")
        )
        pending = {path: set() for path in paths}
        for event in events:
            for path in (event["src_path"], event["dest_path"]):
                if path in pending:
                    pending[path].add(event["event_type"])
        destinations = []
        for path in paths:
            row = indexed.get(path)
            state = "event_pending" if pending[path] else "no_index_record"
            if row:
                state = "indexed" if row["visible"] else "hidden"
            destinations.append(
                {
                    "filename": Path(path).name[:256],
                    "media_item_id": row["id"] if row else None,
                    "state": state,
                    "pending_events": sorted(pending[path]),
                    "reading_index_state": row["index_state"] if row else None,
                }
            )
        result["targets"] = destinations
    return result


async def diagnose_ingestion(query: IngestionQuery, user: UserInfo) -> dict:
    """Trace persisted evidence for a subscription, check, or download.

    Args:
        query: Explicit source identity and one-based page.
        user: Authenticated account.

    Returns:
        Recorded stages with pagination. Legacy sources can lack attempt links.

    Raises:
        ForbiddenException: If the caller cannot inspect shared downloads.
        NotFoundException: If no record of the requested source exists.
    """
    ADMIN_PREVIEW.require(user)
    offset = (query.page - 1) * 10
    if query.task_id:
        task = await DownloadTask.get_or_none(id=query.task_id)
        if not task:
            raise NotFoundException
        facts = await task_evidence(task, query.page)
        if task.plan_attempt_id:
            attempt = await DownloadPlanAttempt.get_or_none(id=task.plan_attempt_id)
            if attempt:
                facts["plan_id"] = attempt.plan_id
        return facts
    if query.attempt_id:
        attempt = await DownloadPlanAttempt.get_or_none(id=query.attempt_id)
        if not attempt:
            raise NotFoundException
        tasks = DownloadTask.filter(plan_attempt_id=attempt.id).order_by("id")
        total = await tasks.count()
        return {
            "plan_id": attempt.plan_id,
            "attempt_id": attempt.id,
            "stage": attempt.stage(detailed=False),
            "started_at": attempt.created_at.isoformat(),
            "status": attempt.outcome_status(),
            "results_seen": attempt.results_seen,
            "submitted": attempt.submitted,
            "errors": attempt.errors,
            "uncertain": attempt.uncertain,
            "linked_task_count": total,
            "tasks": await tasks.offset(offset).limit(10).values("id", "name", "state"),
            "next_page": query.page + 1 if offset + 10 < total else None,
        }
    plan = await DownloadPlan.get_or_none(id=query.plan_id).select_related("graph")
    attempts = DownloadPlanAttempt.filter(plan_id=query.plan_id).order_by("-id")
    total = await attempts.count()
    if not plan and not total:
        raise NotFoundException
    entries = await attempts.offset(offset).limit(10)
    first = (plan.creation_receipt or {}).get("first_execution") if plan else None
    schedule = (
        plan.schedule_state(plan.graph.state != GraphState.DRAFT) if plan else None
    )
    if not plan:
        stage = "plan_removed"
    elif total:
        stage = "checks_recorded"
    elif schedule != "active":
        stage = schedule
    elif first and first.get("status") == "queued":
        stage = "first_check_queued"
    else:
        stage = "no_check_record"
    return {
        "stage": stage,
        "plan_id": query.plan_id,
        "plan_exists": plan is not None,
        "schedule": schedule,
        "first_execution": first,
        "last_execution": plan.last_exec.isoformat()
        if plan and plan.last_exec
        else None,
        "attempt_count": total,
        "attempts": [
            {
                "id": attempt.id,
                "workflow_id": attempt.workflow_id,
                "started_at": attempt.created_at.isoformat(),
                "status": attempt.outcome_status(),
                "stage": attempt.stage(detailed=False),
                "submitted": attempt.submitted,
            }
            for attempt in entries
        ],
        "next_page": query.page + 1 if offset + 10 < total else None,
    }
