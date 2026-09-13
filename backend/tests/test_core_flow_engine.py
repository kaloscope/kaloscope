"""Unit tests for the flow engine."""

import asyncio
from datetime import UTC, datetime
from queue import Queue
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.flow.context import OUTPUT_KEY, Context
from app.core.flow.engine import EventWrapper, FlowEngine, NodeWrapper, TransientTask
from app.core.flow.nodes.base import CancellationSignal
from app.utils.dict import TrackableDict


def _task() -> TransientTask:
    task = TransientTask(1, {}, True)
    context = Context.__new__(Context)
    context.globalvars = {}
    context.localvars = {}
    context.bootparams = {}
    context.storage = TrackableDict()
    context.union()
    task._context = context
    return task


def test_event_trigger(monkeypatch):
    run_date = datetime(2026, 9, 13, tzinfo=UTC)
    events: Queue[EventWrapper] = Queue()
    events.put(EventWrapper("date", 1, job_id=1))
    events.put(EventWrapper("date", 1, job_id=2, run_date=run_date))
    events.put(EventWrapper("cron", 1, job_id=3))
    scheduler = Mock()
    engine = FlowEngine.__new__(FlowEngine)
    engine._num_workers = 1
    monkeypatch.setitem(engine.__dict__, "_events", events)
    monkeypatch.setattr(engine, "_scheduler", scheduler, raising=False)

    async def sleep(_delay):
        if events.empty():
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", sleep)

    asyncio.run(engine._event_consumer())

    scheduler.add_job.assert_called_once()
    call = scheduler.add_job.call_args
    assert call is not None
    assert call.kwargs["id"] == "2"
    assert call.args[1].run_date == run_date


@pytest.mark.parametrize("method", ["copy", "bind_loop"])
def test_context_init_error(monkeypatch, method):
    task = _task()
    task._nodes = {"loop": NodeWrapper("loop", node_type="loop", node_data={})}
    node = NodeWrapper(
        "node", node_type="text", node_data={OUTPUT_KEY: {"id": "output"}}
    ).bind("input", "loop")
    error = RuntimeError("initialization failed")
    log_error = AsyncMock()
    footprint = AsyncMock()
    monkeypatch.setattr(Context, method, Mock(side_effect=error))
    monkeypatch.setattr(TransientTask, "log_error", log_error)
    monkeypatch.setattr(TransientTask, "footprint", footprint)

    async def run():
        async with task.context(node):
            pytest.fail("Context initialization should fail")

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(run())

    assert raised.value is error
    log_error.assert_awaited_once()
    footprint.assert_not_awaited()
    assert OUTPUT_KEY in node.node_data


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (None, False),
        (ValueError("execution failed"), True),
        (CancellationSignal(), False),
        (asyncio.CancelledError(), False),
    ],
)
def test_context_cleanup(monkeypatch, error: BaseException | None, logged: bool):
    task = _task()
    node = NodeWrapper("node", node_type="text", node_data={})
    log_error = AsyncMock()
    monkeypatch.setattr(TransientTask, "log_error", log_error)

    async def run():
        async with task.context(node) as context:
            context["count"] = 1
            node.node_data[OUTPUT_KEY] = {"id": "output"}
            if error is not None:
                raise error

    if error is None:
        asyncio.run(run())
    else:
        with pytest.raises(type(error)) as raised:
            asyncio.run(run())
        assert raised.value is error

    assert task._context["count"] == 1
    assert OUTPUT_KEY not in node.node_data
    assert log_error.await_count == int(logged)
