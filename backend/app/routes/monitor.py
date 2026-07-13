import asyncio
from time import monotonic
from typing import Any

from sanic import Blueprint, HTTPResponse, Request, empty

from app.core.constants import ENCODING
from app.core.decorators import authorize
from app.core.logstream import LogAction, LogSnapshot
from app.models.user import UserRole
from app.utils.json import dumps

_SNAPSHOT_POLL_INTERVAL = 0.5
_STREAM_DURATION = 60
_HEARTBEAT_INTERVAL = 15
_SSE_RECORD_BATCH_SIZE = 50

monitor = Blueprint("monitor", url_prefix="/monitor")


@monitor.get("/logs")
@authorize(role=UserRole.ADMIN)
async def stream_logs(request: Request) -> HTTPResponse | None:
    """Stream recent and new system logs to an administrator."""
    # establish the event stream before polling shared records
    try:
        response = await request.respond(
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
            content_type="text/event-stream",
        )
    except asyncio.CancelledError:
        return None
    except Exception:
        return empty(status=503)
    if response is None:
        return empty(status=503)

    # resume delivery from the client's last acknowledged record
    cursor = request.headers.get("Last-Event-ID")
    state: tuple[bool, int] | None = None
    snapshot: LogSnapshot | None = None
    snapshot_revision: int | None = None
    started = last_activity = monotonic()
    try:
        # reconnect periodically so request middleware revalidates the session
        while monotonic() - started < _STREAM_DURATION:
            # cross the manager boundary only after a snapshot publication
            snapshot_changed = False
            try:
                revision = request.app.shared_ctx.log_snapshot_revision.value
                if revision != snapshot_revision:
                    snapshot = request.app.shared_ctx.log_snapshot["value"]
                    snapshot_revision = revision
                    snapshot_changed = True
            except Exception:
                break
            if snapshot is None:
                break

            if snapshot_changed:
                next_state = (snapshot.paused, snapshot.buffer_id)
                if next_state != state:
                    if not await send_state(response, snapshot):
                        break
                    state = next_state
                    last_activity = monotonic()

                records = records_after(snapshot, cursor)
                if records:
                    last_id = await send_records(response, records, snapshot.run_id)
                    if last_id is None:
                        break
                    # advance the cursor only after the current batch is sent
                    cursor = f"{snapshot.run_id}:{last_id}"
                    last_activity = monotonic()
            if monotonic() - last_activity >= _HEARTBEAT_INTERVAL:
                # keep idle connections alive through buffering proxies
                try:
                    await response.send(": keep-alive\n\n")
                except asyncio.CancelledError:
                    break
                except Exception:
                    break
                last_activity = monotonic()
            await asyncio.sleep(_SNAPSHOT_POLL_INTERVAL)
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    return None


def records_after(
    snapshot: LogSnapshot,
    last_event_id: str | None,
) -> list[dict[str, Any]]:
    """Select retained records newer than an `SSE` cursor.

    Args:
        snapshot: The latest system log snapshot.
        last_event_id: The client cursor from the `Last-Event-ID` header.

    Returns:
        The retained records newer than the validated cursor.
    """
    # accept cursors only from the current backend service lifetime
    try:
        run_id, separator, record_id = (last_event_id or "").rpartition(":")
        cursor = (
            max(0, int(record_id)) if separator and run_id == snapshot.run_id else 0
        )
    except (AttributeError, TypeError, ValueError):
        cursor = 0
    try:
        # reset cursors that point beyond the current snapshot
        if cursor > int(snapshot.last_id):
            cursor = 0
        records = snapshot.records
        return [record for record in records if int(record["id"]) > cursor]
    except (AttributeError, KeyError, TypeError, ValueError):
        # ignore malformed snapshots without terminating the stream
        return []


async def send_state(response: Any, snapshot: LogSnapshot) -> bool:
    """Send the current service-wide log monitor state.

    Args:
        response: The streaming response used to send the event.
        snapshot: The latest system log snapshot.

    Returns:
        Whether the state event was sent successfully.
    """
    data = dumps(
        {
            "paused": snapshot.paused,
            "run_id": snapshot.run_id,
            "buffer_id": snapshot.buffer_id,
        }
    ).decode(ENCODING)
    try:
        await response.send(f"event: state\ndata: {data}\n\n")
    except asyncio.CancelledError:
        return False
    except Exception:
        return False
    return True


async def send_records(
    response: Any,
    records: list[dict[str, Any]],
    run_id: str,
) -> int | None:
    """Send resumable `SSE` records in bounded batches and return the final ID.

    Args:
        response: The streaming response used to send events.
        records: The log records to serialize as `SSE` events.
        run_id: The current backend service lifetime identifier.

    Returns:
        The final sent record ID, or None if sending is interrupted.
    """
    last_id = 0
    events: list[str] = []
    try:
        # preserve individual event IDs while reducing transport writes
        for record in records:
            last_id = int(record["id"])
            data = dumps(record).decode(ENCODING)
            events.append(f"id: {run_id}:{last_id}\ndata: {data}\n\n")
            if len(events) == _SSE_RECORD_BATCH_SIZE:
                await response.send("".join(events))
                events.clear()
        if events:
            await response.send("".join(events))
    except asyncio.CancelledError:
        return None
    except Exception:
        return None
    return last_id


@monitor.post("/logs/pause")
@authorize(role=UserRole.ADMIN)
async def pause_logs(request: Request) -> HTTPResponse:
    """Pause service-wide system log retention."""
    return enqueue_action(request, "pause")


@monitor.post("/logs/resume")
@authorize(role=UserRole.ADMIN)
async def resume_logs(request: Request) -> HTTPResponse:
    """Resume service-wide system log retention."""
    return enqueue_action(request, "resume")


@monitor.post("/logs/clear")
@authorize(role=UserRole.ADMIN)
async def clear_logs(request: Request) -> HTTPResponse:
    """Clear retained service-wide system logs."""
    return enqueue_action(request, "clear")


def enqueue_action(request: Request, action: LogAction) -> HTTPResponse:
    """Queue a service-wide system log action.

    Args:
        request: The current Sanic request.
        action: The system log action to enqueue.

    Returns:
        An accepted response, or a service unavailable response on failure.
    """
    try:
        request.app.shared_ctx.log_actions.put_nowait(action)
    except Exception:
        return empty(status=503)
    return empty(status=202)
