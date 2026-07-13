"""Unit tests for the cross-worker log monitor pipeline."""

import asyncio
import logging
import multiprocessing
import queue
import sys
import time
from types import SimpleNamespace
from typing import cast

from sanic import Sanic

from app import main
from app.core.logstream import (
    EMPTY_LOG_SNAPSHOT,
    LOG_MESSAGE_LIMIT,
    LOGGER_NAMES,
    LogBuffer,
    LogCollector,
    LogQueueHandler,
    MonitorAccessFilter,
    create_log_snapshot,
    register_log_monitor,
    unregister_log_monitor,
)


def _record(
    message: str,
    *,
    level: int = logging.INFO,
    exc_info=None,
) -> logging.LogRecord:
    """Build a complete log record for `Handler` tests."""
    return logging.LogRecord(
        name="sanic.root",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )


def _access_record(status: int, request: str) -> logging.LogRecord:
    """Build a Sanic-style access record."""
    record = _record("")
    record.status = status
    record.request = request
    return record


def _wait_for(predicate, timeout: float = 1.0):
    """Wait for a collector state predicate to become true."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_record_fields():
    """The `Handler` must enqueue only compact primitive fields."""
    records = queue.Queue()
    handler = LogQueueHandler(records)
    record = _record("message", level=logging.WARNING)

    handler.emit(record)

    queued = records.get_nowait()
    assert isinstance(queued.pop("_emitted_at"), float)
    assert queued == {
        "created": record.created,
        "level": "WARNING",
        "logger": "sanic.root",
        "message": "message",
        "process_id": record.process,
        "process_name": record.processName,
    }


def test_missing_process_id():
    """A missing process ID must not discard the log record."""
    records = queue.Queue()
    handler = LogQueueHandler(records)
    record = _record("message")
    record.process = None

    handler.emit(record)

    assert records.get_nowait()["process_id"] == 0


def test_access_message():
    """Empty `sanic.access` messages must use their structured fields."""
    records = queue.Queue()
    handler = LogQueueHandler(records)
    record = _record("")
    record.name = "sanic.access"
    access_fields = {
        "host": "127.0.0.1:58319",
        "request": "GET /_api/filesystem/list",
        "status": 200,
        "byte": 327,
        "duration": " 3.1ms",
    }
    for key, value in access_fields.items():
        setattr(record, key, value)

    handler.emit(record)

    assert records.get_nowait()["message"] == (
        "127.0.0.1:58319 GET /_api/filesystem/list 200 327 3.1ms"
    )
    assert {key: getattr(record, key) for key in access_fields} == access_fields


def test_levels():
    """Formatter mutations must not leak into monitoring level names."""
    records = queue.Queue()
    handler = LogQueueHandler(records)
    cases = (
        (logging.DEBUG, "\x1b[34mDEBUG\x1b[0m", "DEBUG"),
        (logging.INFO, "INFO", "INFO"),
        (logging.WARNING, "\x1b[33mWARN\x1b[0m", "WARNING"),
        (logging.ERROR, "\x1b[31mERROR\x1b[0m", "ERROR"),
        (logging.CRITICAL, "\x1b[31m\x1b[1mCRIT\x1b[0m", "CRITICAL"),
    )

    for level, formatted_level, _ in cases:
        record = _record("message", level=level)
        record.levelname = formatted_level
        handler.emit(record)

    assert [records.get_nowait()["level"] for _ in cases] == [
        expected for _, _, expected in cases
    ]


def test_exception_message():
    """The monitor copy must be plain text and include exception details."""
    records = queue.Queue()
    handler = LogQueueHandler(records)
    try:
        raise ValueError("broken")
    except ValueError:
        record = _record("\x1b[31mfailed\x1b[0m", exc_info=sys.exc_info())

    handler.emit(record)

    message = records.get_nowait()["message"]
    assert message.startswith("failed\nTraceback")
    assert "ValueError: broken" in message
    assert "\x1b" not in message


def test_truncation():
    """Large multibyte messages must stay within the byte limit."""
    records = queue.Queue()
    handler = LogQueueHandler(records)

    handler.emit(_record("测" * LOG_MESSAGE_LIMIT))

    message = records.get_nowait()["message"]
    assert len(message.encode()) <= LOG_MESSAGE_LIMIT
    assert message.endswith("… [truncated]")


def test_handler_failure(capsys):
    """Monitoring failures must not raise or write diagnostic output."""

    class ExplodingQueue:
        def put_nowait(self, _):
            raise RuntimeError("queue unavailable")

    handler = LogQueueHandler(ExplodingQueue())
    record = _record("message")

    handler.emit(record)
    handler.handleError(record)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_access_suppression():
    """Successful monitor streams must not create self-observed access logs."""
    access_filter = MonitorAccessFilter()

    assert access_filter.filter(_access_record(200, "GET /_api/monitor/logs")) is False
    assert (
        access_filter.filter(_access_record(200, "GET /_api/monitor/logs?after=1"))
        is False
    )


def test_access_passthrough():
    """Security failures and unrelated access records must remain visible."""
    access_filter = MonitorAccessFilter()

    assert access_filter.filter(_access_record(403, "GET /_api/monitor/logs"))
    assert access_filter.filter(_access_record(500, "GET /_api/monitor/logs"))
    assert access_filter.filter(_access_record(200, "GET /_api/monitor/other"))


def test_buffer_eviction():
    """The local ring must keep only the newest records in ID order."""
    buffer = LogBuffer(limit=3)

    for index in range(5):
        buffer.append({"message": str(index)})

    last_id, records = buffer.snapshot()
    assert last_id == 5
    assert [record["id"] for record in records] == [3, 4, 5]
    assert [record["message"] for record in records] == ["2", "3", "4"]


def test_clear_keeps_ids():
    """Clearing records must preserve the monotonic ID sequence."""
    buffer = LogBuffer()
    buffer.append({"message": "before"})

    buffer.clear()
    buffer.append({"message": "after"})

    last_id, records = buffer.snapshot()
    assert last_id == 2
    assert records == ({"id": 2, "message": "after"},)


def test_pause_resume():
    """Delayed paused records must be discarded after retention resumes."""
    source = queue.Queue()
    actions = queue.Queue()
    shared = {"value": EMPTY_LOG_SNAPSHOT}
    collector = LogCollector(
        source,
        actions,
        shared,
        multiprocessing.Value("Q", 0),
        publish_interval=0.01,
    )

    collector.start()
    source.put({"message": "before"})
    _wait_for(lambda: shared["value"].last_id == 1)
    actions.put("pause")
    _wait_for(lambda: shared["value"].paused)
    delayed = {"message": "discarded", "_emitted_at": time.monotonic()}
    actions.put("resume")
    _wait_for(lambda: not shared["value"].paused)
    source.put(delayed)
    source.put({"message": "after", "_emitted_at": time.monotonic()})
    _wait_for(lambda: shared["value"].last_id >= 2)
    collector.stop()

    assert [record["message"] for record in shared["value"].records] == [
        "before",
        "after",
    ]


def test_clear_drops_delayed():
    """Records emitted before clear must stay discarded if delivery is delayed."""
    source = queue.Queue()
    actions = queue.Queue()
    shared = {"value": EMPTY_LOG_SNAPSHOT}
    collector = LogCollector(
        source,
        actions,
        shared,
        multiprocessing.Value("Q", 0),
        publish_interval=0.01,
    )

    collector.start()
    delayed = {"message": "stale", "_emitted_at": time.monotonic()}
    actions.put("clear")
    _wait_for(lambda: shared["value"].buffer_id == 1)
    source.put(delayed)
    source.put({"message": "fresh", "_emitted_at": time.monotonic()})
    _wait_for(lambda: shared["value"].last_id > 0)
    collector.stop()

    assert [record["message"] for record in shared["value"].records] == ["fresh"]
    assert "_emitted_at" not in shared["value"].records[0]


def test_snapshot_run_id():
    """Each service lifetime must publish a distinct non-empty run ID."""
    first = create_log_snapshot()
    second = create_log_snapshot()
    assert first.run_id
    assert second.run_id
    assert first.run_id != second.run_id


def test_clear_keeps_pause():
    """Clearing records must retain the global pause state."""
    source = queue.Queue()
    actions = queue.Queue()
    shared = {"value": EMPTY_LOG_SNAPSHOT}
    collector = LogCollector(
        source,
        actions,
        shared,
        multiprocessing.Value("Q", 0),
        publish_interval=0.01,
    )

    collector.start()
    source.put({"message": "before"})
    _wait_for(lambda: shared["value"].last_id == 1)
    actions.put("pause")
    _wait_for(lambda: shared["value"].paused)
    actions.put("clear")
    _wait_for(lambda: shared["value"].buffer_id == 1)
    collector.stop()

    snapshot = shared["value"]
    assert snapshot.last_id == 1
    assert snapshot.buffer_id == 1
    assert snapshot.paused is True
    assert snapshot.records == ()


def test_collector_lifecycle(capsys):
    """The background collector must publish detached snapshots and stop."""
    source = queue.Queue()
    actions = queue.Queue()
    shared = {"value": EMPTY_LOG_SNAPSHOT}
    collector = LogCollector(
        source,
        actions,
        shared,
        multiprocessing.Value("Q", 0),
        publish_interval=0.01,
    )

    collector.start()
    source.put({"message": "one"})
    deadline = time.monotonic() + 1
    while shared["value"].last_id == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    collector.stop()

    snapshot = shared["value"]
    assert snapshot.last_id == 1
    assert snapshot.records == ({"id": 1, "message": "one"},)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_publish_advances_revision():
    """A successful snapshot publication must advance its revision."""
    source = queue.Queue()
    shared = {"value": EMPTY_LOG_SNAPSHOT}
    revision = multiprocessing.Value("Q", 0)
    collector = LogCollector(
        source,
        queue.Queue(),
        shared,
        revision,
        publish_interval=0.01,
    )

    collector.start()
    source.put({"message": "one"})
    _wait_for(lambda: revision.value == 1)
    collector.stop()

    assert shared["value"].records == ({"id": 1, "message": "one"},)


def test_failed_publish_keeps_revision():
    """A failed snapshot write must not expose a new revision."""

    class BrokenSnapshot(dict):
        def __setitem__(self, key, value):
            raise RuntimeError("snapshot unavailable")

    revision = multiprocessing.Value("Q", 0)
    collector = LogCollector(
        queue.Queue(),
        queue.Queue(),
        BrokenSnapshot(value=EMPTY_LOG_SNAPSHOT),
        revision,
    )

    assert collector._publish() is False
    assert revision.value == 0


def test_idle_wait():
    """An idle collector must keep blocking instead of polling at zero timeout."""

    class EmptyQueue:
        def __init__(self):
            self.get_calls = 0

        def get(self, *, timeout):
            self.get_calls += 1
            if timeout > 0:
                time.sleep(timeout)
            raise queue.Empty

        def put_nowait(self, _):
            pass

    source = EmptyQueue()
    collector = LogCollector(
        source,
        queue.Queue(),
        {"value": EMPTY_LOG_SNAPSHOT},
        multiprocessing.Value("Q", 0),
        publish_interval=0.01,
    )

    collector.start()
    time.sleep(0.05)
    collector.stop()

    assert source.get_calls <= 10


def test_registration():
    """Repeated setup must not duplicate monitor handlers or filters."""
    loggers = [logging.getLogger(name) for name in LOGGER_NAMES]
    access_logger = logging.getLogger("sanic.access")
    handler_counts = [len(logger.handlers) for logger in loggers]
    filter_count = len(access_logger.filters)

    monitor = register_log_monitor(queue.Queue())
    duplicate = register_log_monitor(queue.Queue())
    try:
        assert monitor is not None
        assert duplicate is None
        assert [len(logger.handlers) for logger in loggers] == [
            count + 1 for count in handler_counts
        ]
        assert len(access_logger.filters) == filter_count + 1
    finally:
        unregister_log_monitor(duplicate)
        unregister_log_monitor(monitor)

    assert [len(logger.handlers) for logger in loggers] == handler_counts
    assert len(access_logger.filters) == filter_count


def test_main_lifecycle(monkeypatch):
    """The main process must stop collection before closing its queue."""
    events = []

    class FakeQueue:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append(f"{self.name}-close")

        def join_thread(self):
            events.append(f"{self.name}-join")

    class FakeCollector:
        def __init__(self, log_queue, log_actions, snapshot, revision):
            assert log_queue is app.shared_ctx.log_queue
            assert log_actions is app.shared_ctx.log_actions
            assert snapshot is app.shared_ctx.log_snapshot
            assert revision is app.shared_ctx.log_snapshot_revision

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

    app = cast(
        Sanic,
        SimpleNamespace(
            ctx=SimpleNamespace(),
            shared_ctx=SimpleNamespace(
                log_queue=FakeQueue("log"),
                log_actions=FakeQueue("actions"),
                log_snapshot={},
                log_snapshot_revision=object(),
            ),
        ),
    )
    monkeypatch.setattr(main, "LogCollector", FakeCollector)

    asyncio.run(main.start_log_collector(app))
    asyncio.run(main.stop_log_collector(app))

    assert events == [
        "start",
        "stop",
        "log-close",
        "log-join",
        "actions-close",
        "actions-join",
    ]


def test_worker_lifecycle(monkeypatch):
    """A worker must remove exactly the monitor it attached."""
    monitor = object()
    removed = []
    app = cast(
        Sanic,
        SimpleNamespace(
            ctx=SimpleNamespace(),
            shared_ctx=SimpleNamespace(log_queue=object()),
        ),
    )
    monkeypatch.setattr(main, "register_log_monitor", lambda _: monitor)
    monkeypatch.setattr(main, "unregister_log_monitor", removed.append)

    asyncio.run(main.attach_log_monitor(app))
    asyncio.run(main.detach_log_monitor(app))

    assert removed == [monitor]


def test_lifecycle_failure(monkeypatch, capsys):
    """Monitor lifecycle failures must not escape or write diagnostics."""

    class BrokenCollector:
        def __init__(self, *_):
            raise RuntimeError("collector unavailable")

    app = cast(
        Sanic,
        SimpleNamespace(
            ctx=SimpleNamespace(),
            shared_ctx=SimpleNamespace(log_queue=object(), log_snapshot={}),
        ),
    )
    monkeypatch.setattr(main, "LogCollector", BrokenCollector)
    monkeypatch.setattr(
        main,
        "register_log_monitor",
        lambda _: (_ for _ in ()).throw(RuntimeError("handler unavailable")),
    )

    asyncio.run(main.start_log_collector(app))
    asyncio.run(main.stop_log_collector(app))
    asyncio.run(main.attach_log_monitor(app))
    asyncio.run(main.detach_log_monitor(app))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
