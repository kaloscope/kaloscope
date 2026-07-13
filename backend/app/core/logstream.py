import logging
import queue
import re
import threading
import traceback
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

LOG_QUEUE_CAPACITY = 1024
LOG_ACTIONS_CAPACITY = 32
LOG_RECORD_LIMIT = 500
LOG_MESSAGE_LIMIT = 16 * 1024
LOG_PUBLISH_INTERVAL = 0.25

_ANSI_PATTERN = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_TRUNCATION_MARKER = "… [truncated]"
_MONITOR_ACCESS_REQUEST = "GET /_api/monitor/logs"
_EMITTED_AT = "_emitted_at"

LOGGER_NAMES = (
    "sanic.root",
    "sanic.error",
    "sanic.server",
    "sanic.access",
    "sanic.websockets",
    "tortoise",
    "tortoise.db_client",
    "httpx",
    "httpcore",
)

type LogAction = Literal["pause", "resume", "clear"]


@dataclass(frozen=True, slots=True)
class LogSnapshot:
    """Expose detached records and service-wide monitor state."""

    run_id: str = ""
    last_id: int = 0
    buffer_id: int = 0
    paused: bool = False
    records: tuple[dict[str, Any], ...] = ()


EMPTY_LOG_SNAPSHOT = LogSnapshot()


def create_log_snapshot() -> LogSnapshot:
    """Create an empty snapshot for one backend service lifetime."""
    return LogSnapshot(run_id=uuid4().hex)


def _truncate_utf8(value: str, limit: int = LOG_MESSAGE_LIMIT) -> str:
    """Truncate a string to a maximum UTF-8 byte length."""
    encoded = value.encode()
    if len(encoded) <= limit:
        return value
    marker = _TRUNCATION_MARKER.encode()
    prefix = encoded[: limit - len(marker)].decode(errors="ignore")
    return prefix + _TRUNCATION_MARKER


def _record_message(record: logging.LogRecord) -> str:
    """Build the plain-text message for a monitoring record."""
    message = record.getMessage()
    if not message and record.name == "sanic.access":
        values = (
            getattr(record, "host", ""),
            getattr(record, "request", ""),
            getattr(record, "status", ""),
            getattr(record, "byte", ""),
            getattr(record, "duration", ""),
        )
        message = " ".join(
            str(value).strip() for value in values if value not in (None, "")
        )
    if record.exc_info:
        exception = "".join(traceback.format_exception(*record.exc_info)).rstrip()
        if exception:
            message = f"{message}\n{exception}" if message else exception
    elif record.exc_text:
        message = f"{message}\n{record.exc_text}" if message else record.exc_text
    return _truncate_utf8(_ANSI_PATTERN.sub("", message))


class LogQueueHandler(logging.Handler):
    """Copy compact log records into a bounded non-blocking queue."""

    def __init__(self, log_queue: Any):
        """Initialize the handler.

        Args:
            log_queue: The shared queue that receives compact log records.
        """
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord):
        """Attempt to enqueue a monitoring copy without affecting the caller."""
        with suppress(Exception):
            self.log_queue.put_nowait(
                {
                    _EMITTED_AT: monotonic(),
                    "created": float(record.created),
                    "level": logging.getLevelName(record.levelno),
                    "logger": record.name,
                    "message": _record_message(record),
                    "process_id": record.process or 0,
                    "process_name": record.processName,
                }
            )

    def handleError(self, record: logging.LogRecord):  # noqa: N802
        """Suppress standard-library `Handler` diagnostics."""


class MonitorAccessFilter(logging.Filter):
    """Suppress only successful access records for the monitor stream."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether an access record should continue to its handlers."""
        request = str(getattr(record, "request", "")).split("?", 1)[0]
        return not (
            getattr(record, "status", None) == 200
            and request == _MONITOR_ACCESS_REQUEST
        )


class LogBuffer:
    """Keep the newest monitoring records in strict sequence order."""

    def __init__(self, limit: int = LOG_RECORD_LIMIT):
        """Initialize the bounded record buffer.

        Args:
            limit: The maximum number of records to retain.
        """
        self._records: deque[dict[str, Any]] = deque(maxlen=limit)
        self._last_id = 0

    def append(self, record: dict[str, Any]):
        """Assign an ID and append a monitoring record."""
        self._last_id += 1
        stored = dict(record)
        stored["id"] = self._last_id
        self._records.append(stored)

    def clear(self):
        """Discard retained records without resetting sequence IDs."""
        self._records.clear()

    def snapshot(self) -> tuple[int, tuple[dict[str, Any], ...]]:
        """Return an immutable container with detached record dictionaries."""
        return self._last_id, tuple(dict(record) for record in self._records)


class LogCollector:
    """Collect worker records and periodically publish a shared snapshot."""

    def __init__(
        self,
        log_queue: Any,
        log_actions: Any,
        shared_snapshot: Any,
        snapshot_revision: Any,
        *,
        publish_interval: float = LOG_PUBLISH_INTERVAL,
    ):
        """Initialize the main-process collector.

        Args:
            log_queue: The shared queue containing worker log records.
            log_actions: The shared queue containing monitor actions.
            shared_snapshot: The shared holder for detached snapshots.
            snapshot_revision: The shared snapshot publication revision.
            publish_interval: The maximum delay between snapshot publications.
        """
        self._queue = log_queue
        self._actions = log_actions
        self._shared_snapshot = shared_snapshot
        self._snapshot_revision = snapshot_revision
        self._publish_interval = publish_interval
        self._buffer = LogBuffer()
        self._buffer_id = 0
        self._paused = False
        try:
            initial_snapshot = shared_snapshot["value"]
        except Exception:
            initial_snapshot = EMPTY_LOG_SNAPSHOT
        self._run_id = initial_snapshot.run_id or uuid4().hex
        self._accept_after: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        """Start the collector thread once."""
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="kaloscope-log-collector",
                daemon=True,
            )
            self._thread.start()
        except Exception:
            self._thread = None

    def stop(self):
        """Stop the collector without blocking application shutdown."""
        self._stop_event.set()
        with suppress(Exception):
            self._queue.put_nowait(None)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            with suppress(Exception):
                thread.join(timeout=1)

    def _run(self):
        """Collect records and publish state until the collector stops."""
        pending = False
        published_at = monotonic()
        try:
            while not self._stop_event.is_set():
                if self._apply_actions():
                    pending = not self._publish()
                    published_at = monotonic()

                # retry failed publications without busy-waiting
                timeout = self._publish_interval
                if pending:
                    timeout = max(
                        0.0,
                        self._publish_interval - (monotonic() - published_at),
                    )
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = ...
                except Exception:
                    break

                if item is None:
                    break
                pending = self._store(item) or pending

                now = monotonic()
                if pending and now - published_at >= self._publish_interval:
                    pending = not self._publish()
                    published_at = now
        except Exception:
            pass
        finally:
            if pending:
                self._publish()

    def _store(self, item: Any) -> bool:
        """Store one eligible record and return whether the buffer changed."""
        if self._paused or not isinstance(item, dict):
            return False
        record = dict(item)
        emitted_at = record.pop(_EMITTED_AT, None)
        # reject records delayed across clear or resume boundaries
        if (
            self._accept_after is not None
            and isinstance(emitted_at, int | float)
            and emitted_at <= self._accept_after
        ):
            return False
        self._buffer.append(record)
        return True

    def _drain(self):
        """Discard all records currently available from the worker queue."""
        while True:
            try:
                self._queue.get_nowait()
            except Exception:
                return

    def _apply_actions(self) -> bool:
        """Apply queued actions and return whether shared state changed."""
        changed = False
        while True:
            try:
                action: LogAction = self._actions.get_nowait()
            except Exception:
                return changed
            if action == "pause":
                changed = changed or not self._paused
                self._paused = True
                self._drain()
            elif action == "resume":
                was_paused = self._paused
                changed = changed or was_paused
                self._paused = False
                if was_paused:
                    self._accept_after = monotonic()
            elif action == "clear":
                self._buffer.clear()
                self._drain()
                self._accept_after = monotonic()
                self._buffer_id += 1
                changed = True

    def _publish(self) -> bool:
        """Publish the current detached snapshot without raising errors."""
        try:
            last_id, records = self._buffer.snapshot()
            self._shared_snapshot["value"] = LogSnapshot(
                run_id=self._run_id,
                last_id=last_id,
                buffer_id=self._buffer_id,
                paused=self._paused,
                records=records,
            )
            with self._snapshot_revision.get_lock():
                self._snapshot_revision.value += 1
            return True
        except Exception:
            return False


@dataclass(frozen=True, slots=True)
class LogMonitor:
    """Track logging objects owned by one monitor registration."""

    handler: LogQueueHandler
    loggers: tuple[logging.Logger, ...]
    access_filter: MonitorAccessFilter
    access_logger: logging.Logger


def register_log_monitor(log_queue: Any) -> LogMonitor | None:
    """Register one monitor `Handler` and access `Filter` with worker loggers."""
    attached: list[logging.Logger] = []
    access_logger: logging.Logger | None = None
    access_filter: MonitorAccessFilter | None = None
    handler: LogQueueHandler | None = None
    try:
        loggers = tuple(logging.getLogger(name) for name in LOGGER_NAMES)
        access_logger = logging.getLogger("sanic.access")
        # avoid duplicate registrations across repeated startup hooks
        if any(
            isinstance(existing, LogQueueHandler)
            for logger in loggers
            for existing in logger.handlers
        ) or any(
            isinstance(existing, MonitorAccessFilter)
            for existing in access_logger.filters
        ):
            return None

        handler = LogQueueHandler(log_queue)
        access_filter = MonitorAccessFilter()
        for logger in loggers:
            logger.addHandler(handler)
            attached.append(logger)
        access_logger.addFilter(access_filter)
        return LogMonitor(handler, loggers, access_filter, access_logger)
    except Exception:
        # roll back partial registration without emitting recursive logs
        if handler is not None:
            for logger in attached:
                with suppress(Exception):
                    logger.removeHandler(handler)
        if access_logger is not None and access_filter is not None:
            with suppress(Exception):
                access_logger.removeFilter(access_filter)
        return None


def unregister_log_monitor(monitor: LogMonitor | None):
    """Unregister logging objects owned by one monitor registration."""
    if monitor is None:
        return
    for logger in monitor.loggers:
        with suppress(Exception):
            logger.removeHandler(monitor.handler)
    with suppress(Exception):
        monitor.access_logger.removeFilter(monitor.access_filter)
