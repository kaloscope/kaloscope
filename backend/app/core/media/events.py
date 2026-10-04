"""Coalesce persisted reading events into work-scoped reconciliation tasks."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from tortoise.transactions import in_transaction
from watchdog.events import FileSystemEvent

from app.core.media.common import ContentError
from app.core.media.coordination import library_lock, notify_media_events
from app.core.media.handlers.base import get_handler
from app.models.media import LibType, MediaEvent, MediaLib

_SOURCE_EVENTS = ("created", "modified", "deleted", "moved")


class ReadingMove(BaseModel):
    """Retain a filesystem move and its persisted arrival order."""

    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: int = Field(gt=0)
    src_path: str
    dest_path: str
    is_directory: bool


class ReadingReconcile(BaseModel):
    """Validate the affected containers and moves stored in a reconcile event."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    targets: list[str]
    moves: list[ReadingMove] = Field(default_factory=list)


async def coalesce_reading_events(lib_id: int) -> list[MediaEvent]:
    """Atomically replace selected raw events with one task per affected work.

    Call outside the library lock, from its serial consumer. Existing reconcile
    tasks are merged without reading sources or invoking content indexing. Only
    selected IDs are removed; later arrivals remain available for the next call.
    This preparation step does not execute or acknowledge reconciliation tasks.

    Args:
        lib_id: The reading library whose persisted events should be coalesced.

    Returns:
        The created or updated work tasks, ordered by their first selected event.
        An empty list means no selected event affected a supported reading scope.

    Raises:
        ValueError: If the library is not a reading type or a saved payload is invalid.
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
        handler = get_handler(lib.lib_type)
        events = await MediaEvent.filter(
            lib_id=lib_id, event_type__in=_SOURCE_EVENTS
        ).order_by("id")
        if not events:
            return []

        grouped: dict[str, ReadingReconcile] = {}
        for event in events:
            source = FileSystemEvent(event.src_path, event.dest_path or "")
            source.event_type = event.event_type
            source.is_directory = event.is_directory
            for work, targets in handler.resolve_event_targets(
                source, base_path=lib.dir
            ).items():
                batch = grouped.setdefault(str(work), ReadingReconcile(targets=[]))
                batch.targets.extend(str(target) for target in targets)
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
            pending = await MediaEvent.filter(
                lib_id=lib_id, event_type="reconcile", src_path=work
            ).order_by("id")
            for event in pending:
                previous = ReadingReconcile.model_validate(event.payload)
                batch.targets.extend(previous.targets)
                batch.moves.extend(previous.moves)
            batch.targets = (
                [work] if work in batch.targets else sorted(set(batch.targets))
            )
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
            task.payload = batch.model_dump(mode="json")
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
