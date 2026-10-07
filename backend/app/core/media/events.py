"""Coalesce, observe and finalize reading tasks with bounded retries."""

from asyncio import to_thread
from pathlib import Path
from time import time
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from tortoise.transactions import in_transaction
from watchdog.events import FileSystemEvent

from app.core.media.common import ContentError
from app.core.media.coordination import library_lock, notify_media_events
from app.core.media.handlers.base import get_handler
from app.core.media.handlers.reading import is_ignored_name
from app.models.media import LibType, MediaEvent, MediaLib

_SOURCE_EVENTS = ("created", "modified", "deleted", "moved")
_STABILITY_SECONDS = 2
_RETRY_DELAYS = (2, 5, 15, 30)


class ReadingMove(BaseModel):
    """Retain a filesystem move and its persisted arrival order."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: int = Field(gt=0)
    src_path: str
    dest_path: str
    is_directory: bool


class ReadingReconcile(BaseModel):
    """Validate reading task scopes, moves, observations and retry state."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    targets: list[str]
    # tasks without body-change facts must rebuild their selected scope
    force_targets: list[str] = Field(
        validation_alias=AliasChoices("force_targets", "targets")
    )
    moves: list[ReadingMove] = Field(default_factory=list)
    observed_snapshot: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    not_before: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    state: Literal["pending", "deferred", "failed"] = "pending"
    attempts: int = Field(default=0, ge=0, le=len(_RETRY_DELAYS) + 1)
    error_code: str | None = Field(default=None, min_length=1, max_length=64)


def _reading_event_identity(event: MediaEvent) -> tuple:
    """Identify the persisted task version used during unlocked work.

    Args:
        event: The task captured before observation or execution.

    Returns:
        The ownership, scope, payload and revision that must still match at completion.
    """
    return (
        event.lib_id,
        event.event_type,
        event.src_path,
        event.dest_path,
        event.is_directory,
        event.payload,
        event.updated_at,
    )


def _defer_reading_task(payload: ReadingReconcile, error: ContentError):
    """Apply the shared retry limit without changing scopes or source observations.

    Args:
        payload: The current task payload whose retry state will be updated.
        error: The controlled source or execution failure to persist.
    """
    payload.attempts = min(payload.attempts + 1, len(_RETRY_DELAYS) + 1)
    payload.error_code = error.code
    payload.state = "failed" if payload.attempts > len(_RETRY_DELAYS) else "deferred"
    payload.not_before = (
        None
        if payload.state == "failed"
        else time() + _RETRY_DELAYS[payload.attempts - 1]
    )


async def coalesce_reading_events(
    lib_id: int, *, scan_works: set[Path] | None = None
) -> list[MediaEvent]:
    """Merge filesystem events and scan scopes into one task per affected work.

    Call outside the library lock, from the serial consumer or scanner. Existing
    reconcile tasks merge without reading sources or invoking content indexing. Only
    selected IDs are removed; later arrivals remain available for the next call.
    This preparation step does not execute or acknowledge reconciliation tasks.

    Args:
        lib_id: The reading library whose persisted events should be coalesced.
        scan_works: Work directories to check incrementally, including missing
            registered works. None merges only filesystem events. Scans preserve
            pending moves and forced rebuilds without forcing unchanged bodies.

    Returns:
        Tasks ordered by scan path, then by their first selected filesystem event.
        An empty list means neither input selected a supported reading scope.

    Raises:
        ValueError: If the library type, scan scopes or a saved payload is invalid.
        ContentError: If the library path or type changes while acquiring its lock.
        DoesNotExist: If the library no longer exists.
    """
    lib = await MediaLib.get(id=lib_id)
    if lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
        raise ValueError("unsupported reading library type")

    tasks: list[MediaEvent] = []
    async with library_lock(lib.dir), in_transaction("default"):
        current = await MediaLib.get(id=lib_id)
        if (current.dir, current.lib_type) != (lib.dir, lib.lib_type):
            raise ContentError("content_changed")
        root = Path(lib.dir)
        if scan_works is not None and (
            not root.is_absolute()
            or ".." in root.parts
            or any(
                work.parent != root or is_ignored_name(work.name) for work in scan_works
            )
        ):
            raise ValueError("scan works must be visible direct library directories")
        handler = get_handler(lib.lib_type)
        events = await MediaEvent.filter(
            lib_id=lib_id, event_type__in=_SOURCE_EVENTS
        ).order_by("id")
        if not events and not scan_works:
            return []

        grouped = {
            str(work): ReadingReconcile(targets=[str(work)], force_targets=[])
            for work in sorted(scan_works or ())
        }
        for event in events:
            source = FileSystemEvent(event.src_path, event.dest_path or "")
            source.event_type = event.event_type
            source.is_directory = event.is_directory
            content = handler.resolve_content_targets(source, base_path=lib.dir)
            for work, targets in handler.resolve_event_targets(
                source, base_path=lib.dir
            ).items():
                batch = grouped.setdefault(
                    str(work), ReadingReconcile(targets=[], force_targets=[])
                )
                batch.targets.extend(str(target) for target in targets)
                batch.force_targets.extend(
                    str(target) for target in content.get(work, ())
                )
                if event.event_type == "moved":
                    batch.moves.append(
                        ReadingMove(
                            event_id=event.id,
                            src_path=event.src_path,
                            dest_path=event.dest_path or "",
                            is_directory=event.is_directory,
                        )
                    )

        for work, batch in grouped.items():
            forced = list(batch.force_targets)
            pending = await MediaEvent.filter(
                lib_id=lib_id, event_type="reconcile", src_path=work
            ).order_by("id")
            for event in pending:
                previous = ReadingReconcile.model_validate(event.payload)
                batch.targets.extend(previous.targets)
                forced.extend(previous.force_targets)
                batch.moves.extend(previous.moves)
            batch.targets = (
                [work] if work in batch.targets else sorted(set(batch.targets))
            )
            batch.force_targets = [work] if work in forced else sorted(set(forced))
            # a move spanning works belongs to both tasks with the same event ID
            moves: dict[int, ReadingMove] = {}
            for move in batch.moves:
                if move.event_id in moves and moves[move.event_id] != move:
                    raise ValueError("conflicting reading move records")
                moves[move.event_id] = move
            batch.moves = [moves[key] for key in sorted(moves)]
            task = (
                pending[0]
                if pending
                else MediaEvent(
                    lib_id=lib_id,
                    event_type="reconcile",
                    src_path=work,
                    is_directory=True,
                )
            )
            # new events reset observations, deadlines and exhausted retries
            task.payload = batch.model_dump(mode="json", exclude_none=True)
            await task.save()
            tasks.append(task)
            if len(pending) > 1:
                await MediaEvent.filter(
                    id__in=[event.id for event in pending[1:]]
                ).delete()

        await MediaEvent.filter(id__in=[event.id for event in events]).delete()

    if tasks:
        notify_media_events(lib_id)
    return tasks


async def prepare_reading_event(event_id: int) -> bool:
    """Check a saved task's sources twice across a persisted quiet interval.

    The serial library consumer calls this outside its lock. Filesystem work runs
    outside locks and transactions; future-dated and failed tasks return immediately.
    Source errors retry after 2, 5, 15 and 30 seconds, stopping at the fifth failure.
    New events reset retries and invalidate any in-flight observation. Readiness
    neither executes nor acknowledges a task, and does not establish source ownership.

    Args:
        event_id: The persisted reconcile event to inspect, reloaded on every call.

    Returns:
        True for two matching observations at least two seconds apart. False for
        deferred, failed, removed, replaced or non-reconcile events. Retry state and
        deadlines persist in the existing payload, including across restarts.

    Raises:
        ValueError: If the library, saved payload or selected scope is invalid.
        ContentError: If sources are unavailable or the library changes. Source
            failures clear the observation and save retry or failed state before
            propagating. Failed tasks need a new event before checking again.
    """
    event = await MediaEvent.get_or_none(id=event_id).select_related("lib")
    if event is None or event.event_type != "reconcile":
        return False
    lib = event.lib
    if lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
        raise ValueError("unsupported reading library type")
    payload = ReadingReconcile.model_validate(event.payload)
    if payload.state == "failed" or (
        payload.not_before is not None and time() < payload.not_before
    ):
        return False

    snapshot = None
    failure = None
    try:
        snapshot = await to_thread(
            get_handler(lib.lib_type).snapshot_sources,
            lib.dir,
            work_path=Path(event.src_path),
            targets={Path(path) for path in payload.targets},
        )
    except ContentError as error:
        failure = error

    async with library_lock(lib.dir):
        current = await MediaEvent.get_or_none(id=event_id).select_related("lib")
        if current is None or _reading_event_identity(
            current
        ) != _reading_event_identity(event):
            return False
        if (current.lib.dir, current.lib.lib_type) != (lib.dir, lib.lib_type):
            raise ContentError("content_changed")
        if (
            snapshot is not None
            and snapshot == payload.observed_snapshot
            and payload.not_before is not None
            and time() >= payload.not_before
        ):
            return True
        payload.observed_snapshot = snapshot
        if failure is None:
            payload.state = "pending"
            payload.attempts = 0
            payload.error_code = None
            payload.not_before = time() + _STABILITY_SECONDS
        else:
            _defer_reading_task(payload, failure)
        current.payload = payload.model_dump(mode="json", exclude_none=True)
        await current.save(update_fields=["payload", "updated_at"])

    if failure is not None:
        raise failure
    return False


async def finish_reading_event(
    event: MediaEvent, *, error: ContentError | None = None
) -> bool:
    """Acknowledge completed work or persist a bounded execution retry.

    Call outside the library lock after serially processing every task scope and
    move. Capture the event with its library loaded after preparation and before
    execution; do not reload that snapshot on completion. New events, changed task
    revisions and source changes keep the task pending. This step neither performs
    ingestion nor decides whether a missing source is safe to remove.

    Args:
        event: The unmodified, prepared task snapshot with its library loaded.
        error: A controlled execution failure; None reports successful processing
            of every scope, including any required moves and deletion cleanup.

    Returns:
        True only when this exact task is acknowledged. False for obsolete,
        unprepared, deferred or failed work, or after saving an execution retry.

    Raises:
        ValueError: If the library, payload or selected scope is invalid.
        ContentError: If the library changes or source rechecking fails. Recheck
            failures persist their retry state before propagating; reported
            execution errors are persisted without being raised again.
    """
    if event.event_type != "reconcile":
        return False
    lib = event.lib
    if lib.lib_type not in (LibType.NOVEL, LibType.COMIC):
        raise ValueError("unsupported reading library type")
    payload = ReadingReconcile.model_validate(event.payload)
    if (
        payload.state == "failed"
        or payload.observed_snapshot is None
        or payload.not_before is None
        or time() < payload.not_before
    ):
        return False

    snapshot = None
    failure = None
    try:
        snapshot = await to_thread(
            get_handler(lib.lib_type).snapshot_sources,
            lib.dir,
            work_path=Path(event.src_path),
            targets={Path(path) for path in payload.targets},
        )
    except ContentError as observed:
        failure = observed

    async with library_lock(lib.dir):
        current = await MediaEvent.get_or_none(id=event.id).select_related("lib")
        if current is None or _reading_event_identity(
            current
        ) != _reading_event_identity(event):
            return False
        if (current.lib.dir, current.lib.lib_type) != (lib.dir, lib.lib_type):
            raise ContentError("content_changed")
        if time() < payload.not_before:
            return False
        if snapshot is not None and snapshot != payload.observed_snapshot:
            # changed sources start a new quiet interval, even after failed execution
            payload.state = "pending"
            payload.attempts = 0
            payload.error_code = None
            payload.not_before = time() + _STABILITY_SECONDS
        elif (retry_error := failure or error) is not None:
            _defer_reading_task(payload, retry_error)
        else:
            await current.delete()
            return True
        # stable execution failures retain the snapshot so readiness keeps the counter
        payload.observed_snapshot = snapshot
        current.payload = payload.model_dump(mode="json", exclude_none=True)
        await current.save(update_fields=["payload", "updated_at"])

    if failure is not None:
        raise failure
    return False
