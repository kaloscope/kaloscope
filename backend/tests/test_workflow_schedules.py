"""Unit tests for reviewed workflow schedule changes shared by assistant and MCP."""

import asyncio
import hashlib
import queue
import re
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import ValidationError

from app.core.ai import tools
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.flow.engine import FlowEngine, FlowTask
from app.models.assistant import AssistantToolCall, AssistantTurn, ConversationCreate
from app.models.flow import (
    FlowGraph,
    FlowJob,
    FlowJobReceipt,
    FlowLog,
    JobUpsert,
    ScheduleApply,
    ScheduleChange,
)
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.user import Permissions, UserRole
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowJobService
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_indexer_auth_authoring import runtime as runtime
from tests.test_mcp import application, call
from tests.test_services_assistant import configure
from tests.test_workflow_outputs import JSON_FORMATTER
from tests.test_workflow_runs import converse, names, scripted

SHANGHAI = timezone(timedelta(hours=8))


def cleanup_graph():
    """Build a published schedule workflow that requests one cleanup URL.

    Returns:
        Compressed `schedule_start -> http` graph.
    """
    return {
        "nodes": [
            {
                "id": "start",
                "type": "node",
                "position": {"x": 0, "y": 0},
                "data": {"$schema": "schedule_start"},
            },
            {
                "id": "http",
                "type": "node",
                "position": {"x": 800, "y": 0},
                "data": {
                    "$schema": "http",
                    "method": "GET",
                    "url": "https://cleanup.test/run",
                    "formatter": JSON_FORMATTER,
                },
            },
        ],
        "edges": [
            {
                "id": "start-http",
                "source": "start",
                "sourceHandle": "output",
                "target": "http",
                "targetHandle": "input",
            }
        ],
    }


async def workflow(**fields):
    """Create a schedule workflow whose ID differs from every schedule ID.

    Args:
        **fields: Model overrides such as `state` or `category`.

    Returns:
        Created workflow.
    """
    values = {
        "id": 70 + await FlowGraph.all().count(),
        "name": "Nightly cleanup",
        "category": "schedule",
        "state": "published",
        "definition": cleanup_graph(),
        "draft": cleanup_graph(),
        **fields,
    }
    return await FlowGraph.create(**values)


async def schedule(graph, state="running", **timing):
    """Store a schedule directly, as earlier editor saves did.

    Args:
        graph: Scheduled workflow.
        state: Stored activation state.
        **timing: Trigger and timing fields; defaults to a daily cron.

    Returns:
        Created schedule.
    """
    timing = timing or {"trigger": "cron", "cron_expr": "0 1 * * *"}
    return await FlowJob.create(
        id=500 + await FlowJob.all().count(), graph=graph, state=state, **timing
    )


async def review(user, **change):
    """Review one change through the shared service.

    Args:
        user: Calling account.
        **change: Change fields.

    Returns:
        Shared review.
    """
    return await FlowJobService.review_change(
        ScheduleChange.model_validate(change), user
    )


async def apply(user, reviewed, *, confirmed=True, **change):
    """Apply a review through the shared service.

    Args:
        user: Calling account.
        reviewed: Review whose request ID and revision are confirmed.
        confirmed: The user's approval.
        **change: The reviewed change, possibly altered by the caller.

    Returns:
        Shared outcome.
    """
    return await FlowJobService.apply_change(
        ScheduleApply(
            request_id=reviewed["request_id"],
            change=change,
            revision=reviewed["revision"],
            confirmed=confirmed,
        ),
        user,
    )


async def change(user, **values):
    """Review a change and apply it at once with confirmation.

    Args:
        user: Calling account.
        **values: Change fields.

    Returns:
        Review and outcome.
    """
    reviewed = await review(user, **values)
    assert reviewed["valid"], reviewed
    return reviewed, await apply(user, reviewed, **values)


@asynccontextmanager
async def scheduling(monkeypatch):
    """Run the engine's own registration loops against a paused scheduler.

    Args:
        monkeypatch: Fixture routing the service to this engine.

    Yields:
        Engine whose scheduler computes next run times but never fires.
    """
    engine = FlowEngine.__new__(FlowEngine)
    engine.__dict__["_events"] = queue.Queue()
    engine.__dict__["_job_actions"] = {}
    engine._num_workers = 1
    engine.runs = []
    engine._app = SimpleNamespace(
        add_task=lambda work: engine.runs.append(asyncio.ensure_future(work))
    )
    engine._scheduler = AsyncIOScheduler(timezone=pytz.utc)
    monkeypatch.setattr(
        FlowJobService,
        "app_ctx",
        classmethod(lambda cls: SimpleNamespace(flow_engine=engine)),
    )
    engine._scheduler.start(paused=True)
    loops = [
        asyncio.create_task(engine._event_consumer()),
        asyncio.create_task(engine._job_listener()),
    ]
    try:
        yield engine
    finally:
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        engine._scheduler.shutdown(wait=False)


async def settle(engine):
    """Wait until the engine has registered or removed every changed schedule.

    Args:
        engine: Engine running its registration loops.
    """
    for _ in range(100):
        if (
            engine._events.empty()
            and not engine._job_actions
            and not await FlowJob.exists(state="pending")
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("The scheduler did not settle")


def registered(engine, job_id):
    """Read the scheduler's registration of one schedule.

    Args:
        engine: Engine with a scheduler.
        job_id: Schedule ID.

    Returns:
        The scheduler job, or `None`.
    """
    return engine._scheduler.get_job(str(job_id))


def instant(value):
    """Parse an ISO time returned by a review.

    Args:
        value: ISO 8601 string.

    Returns:
        Aware datetime.
    """
    return datetime.fromisoformat(value)


def soon(**delta):
    """Name a future time with an explicit Shanghai offset.

    Args:
        **delta: Offset from now.

    Returns:
        ISO 8601 string with `+08:00`.
    """
    moment = (datetime.now(UTC) + timedelta(**delta)).replace(microsecond=0)
    return moment.astimezone(SHANGHAI).isoformat()


@pytest.mark.parametrize(
    "timing",
    [
        {"trigger": "cron", "cron_expr": "30 1 * * mon-fri"},
        {"trigger": "date", "run_date": soon(days=2)},
        {"trigger": "interval", "interval_num": 6, "interval_unit": "hours"},
        {
            "trigger": "interval",
            "interval_num": 2,
            "interval_unit": "hours",
            "interval_start": soon(days=1),
        },
        {
            "trigger": "interval",
            "interval_num": 2,
            "interval_unit": "hours",
            "interval_start": soon(hours=-5, minutes=-30),
            "interval_end": soon(days=10),
        },
    ],
    ids=["cron", "date", "interval", "future-start", "past-start"],
)
def test_create(monkeypatch, timing):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            values = {
                "action": "create",
                "workflow_id": graph.id,
                "timezone": "Asia/Shanghai",
                **timing,
            }

            applied_at = datetime.now(UTC)
            reviewed, result = await change(owner, **values)
            replay = await apply(owner, reviewed, **values)
            # the same instants written with another offset are the same change
            respelled = await apply(
                owner,
                reviewed,
                **{
                    **values,
                    **{
                        name: instant(timing[name]).astimezone(UTC).isoformat()
                        for name in ("run_date", "interval_start", "interval_end")
                        if name in timing
                    },
                },
            )
            await settle(engine)

            after = reviewed["after"]
            assert reviewed["before"] is None and reviewed["job_id"] is None
            assert after["state"] == "running"
            assert (after["repeatable"], after["recoverable"]) == (False, True)
            assert reviewed["defaults"]["bootparams"] == {}
            assert reviewed["timezone"] == "UTC"
            assert "Enabled at once" in reviewed["notes"][0]
            assert result["applied"] and result["outcome"] == "created"
            assert result["job_id"] != graph.id
            assert replay["replayed"] and replay["job_id"] == result["job_id"]
            assert respelled["replayed"] and respelled["job_id"] == result["job_id"]
            job = await FlowJob.get(id=result["job_id"])
            assert job.state == "running" and job.bootparams == {}
            assert await FlowJob.all().count() == 1
            # stored times are UTC instants of the offset input
            for name in ("run_date", "interval_start", "interval_end"):
                if name in timing:
                    stored = getattr(job, name)
                    assert stored.utcoffset() == timedelta(0)
                    assert stored == instant(timing[name])
                    assert after[name] == stored.isoformat()
            # the review predicts the registration the scheduler actually made
            scheduled = registered(engine, job.id).next_run_time
            first = reviewed["first_run"]
            estimate = instant(first["estimate"])
            if "interval_start" in timing or timing["trigger"] != "interval":
                assert scheduled == estimate
            else:
                assert abs(scheduled - estimate) < timedelta(seconds=5)
                assert first["rule"].startswith("One interval after registration")
            # and both follow the documented trigger rules
            if timing["trigger"] == "cron":
                assert (scheduled.hour, scheduled.minute) == (1, 30)
                assert scheduled.weekday() < 5
                assert timedelta(0) < scheduled - applied_at < timedelta(days=4)
            elif timing["trigger"] == "date":
                assert scheduled == instant(timing["run_date"])
            elif "interval_start" not in timing:
                expected = applied_at + timedelta(hours=6)
                assert abs(scheduled - expected) < timedelta(seconds=10)
            elif instant(timing["interval_start"]) > applied_at:
                assert scheduled == instant(timing["interval_start"])
            else:
                start = instant(timing["interval_start"])
                assert scheduled == start + timedelta(hours=6)
            local = instant(first["local"])
            assert local == estimate and local.utcoffset() == timedelta(hours=8)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("values", "path", "message"),
    [
        (
            {"trigger": "cron", "cron_expr": "0 9 * * 1-5"},
            "cron_expr",
            "reads 0 as Monday",
        ),
        ({"trigger": "cron", "cron_expr": "61 * * * *"}, "cron_expr", "61"),
        ({"trigger": "cron", "cron_expr": "0 9 * *"}, "trigger", "5 fields"),
        (
            {"trigger": "date", "run_date": soon(days=-1)},
            "run_date",
            "in the future",
        ),
        (
            {"trigger": "date", "run_date": soon(days=1), "cron_expr": "0 1 * * *"},
            "cron_expr",
            "do not use it",
        ),
        (
            {
                "trigger": "interval",
                "interval_num": 1,
                "interval_unit": "days",
                "interval_start": soon(days=3),
                "interval_end": soon(days=2),
            },
            "trigger",
            "interval_start must be before interval_end",
        ),
        (
            {"trigger": "interval", "interval_num": 1},
            "trigger",
            "interval_num and interval_unit required",
        ),
        (
            {
                "trigger": "cron",
                "cron_expr": "0 1 * * *",
                "bootparams": {"$start": "schedule_start"},
            },
            "bootparams",
            "reserved",
        ),
    ],
    ids=[
        "numeric-weekday",
        "cron-range",
        "cron-fields",
        "past-date",
        "other-trigger",
        "range",
        "unit",
        "reserved",
    ],
)
def test_invalid(monkeypatch, values, path, message):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()

            reviewed = await review(
                owner, action="create", workflow_id=graph.id, **values
            )

            assert not reviewed["valid"] and "request_id" not in reviewed
            assert any(
                issue["path"] == path and message in issue["message"]
                for issue in reviewed["issues"]
            ), reviewed["issues"]
            assert not await FlowJob.exists() and engine._events.empty()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"run_date": "2026-11-01T09:00:00"}, "UTC offset"),
        ({"timezone": "Mars/Base"}, "IANA"),
        ({"job_id": 1}, "workflow_id, not job_id"),
        ({"trigger": None}, "requires trigger"),
        ({"action": "pause", "job_id": 1, "workflow_id": None}, "takes no settings"),
        ({"action": "delete", "workflow_id": None}, "takes job_id"),
    ],
    ids=["naive", "zone", "create-job", "trigger", "pause-settings", "delete-id"],
)
def test_arguments(values, message):
    base = {"action": "create", "workflow_id": 7, "trigger": "date"}
    with pytest.raises(ValidationError, match=message):
        ScheduleChange.model_validate({**base, **values})


def test_workflow_rules(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch):
            draft = await workflow(state="draft", definition=None)
            indexer = await workflow(name="Search", category="indexer")
            unpublished = await workflow(name="Later", state="draft")
            paused = await schedule(unpublished, state="paused")
            reader = SimpleNamespace(
                id=owner.id, role=UserRole.USER, perms=Permissions(indexer_ids=[])
            )
            cron = {"trigger": "cron", "cron_expr": "0 1 * * *"}

            drafted = await review(owner, action="create", workflow_id=draft.id, **cron)
            searched = await review(
                owner, action="create", workflow_id=indexer.id, **cron
            )
            resumed = await review(owner, action="enable", job_id=paused.id)
            edited = await review(
                owner, action="update", job_id=paused.id, cron_expr="0 2 * * *"
            )

            assert "Publish the workflow" in drafted["issues"][0]["message"]
            assert "schedule workflow" in searched["issues"][0]["message"]
            # resuming keeps the schedule page's publication rule; edits do not
            assert "Publish the workflow first" in resumed["issues"][0]["message"]
            assert edited["valid"] and "unpublished" in " ".join(edited["notes"])
            assert edited["first_run"]["rule"].startswith("Stays paused")
            with pytest.raises(ForbiddenException):
                await review(reader, action="create", workflow_id=draft.id, **cron)
            with pytest.raises(NotFoundException):
                await review(owner, action="create", workflow_id=999, **cron)
            with pytest.raises(NotFoundException):
                await review(owner, action="pause", job_id=999)

    asyncio.run(run())


def test_update(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            _, created = await change(
                owner,
                action="create",
                workflow_id=graph.id,
                trigger="cron",
                cron_expr="0 1 * * *",
                bootparams={"days": 7},
            )
            job_id = created["job_id"]
            await settle(engine)

            reviewed, updated = await change(
                owner, action="update", job_id=job_id, cron_expr="0 3 * * *"
            )
            await settle(engine)
            unchanged = await review(
                owner, action="update", job_id=job_id, cron_expr="0 3 * * *"
            )
            switched, _ = await change(
                owner,
                action="update",
                job_id=job_id,
                trigger="interval",
                interval_num=12,
                interval_unit="hours",
            )
            await settle(engine)

            assert reviewed["changes"] == [
                {"field": "cron_expr", "before": "0 1 * * *", "after": "0 3 * * *"}
            ]
            assert "re-registers" in " ".join(reviewed["notes"])
            assert updated["applied"] and updated["outcome"] == "updated"
            assert updated["schedule"]["state"] == "pending"
            assert "registering the new settings" in updated["message"]
            assert "Nothing would change" in unchanged["issues"][0]["message"]
            # the previous trigger's timing is cleared and parameters are kept
            assert {item["field"] for item in switched["changes"]} == {
                "trigger",
                "cron_expr",
                "interval_num",
                "interval_unit",
            }
            job = await FlowJob.get(id=job_id)
            assert job.cron_expr is None and job.bootparams == {"days": 7}
            assert job.state == "running"
            scheduled = registered(engine, job_id).next_run_time
            estimate = instant(switched["first_run"]["estimate"])
            assert abs(scheduled - estimate) < timedelta(seconds=5)

            # a paused schedule keeps its state and stays unregistered
            _, paused = await change(owner, action="pause", job_id=job_id)
            await settle(engine)
            edit, result = await change(
                owner, action="update", job_id=job_id, interval_num=1
            )
            await settle(engine)
            assert edit["first_run"] == {
                "estimate": None,
                "rule": "Stays paused; no runs until enabled.",
            }
            assert result["schedule"]["state"] == "paused"
            assert registered(engine, job_id) is None

    asyncio.run(run())


def test_running_workflow(monkeypatch, runtime):
    gate = asyncio.Event()
    calls = []

    async def remote(request):
        calls.append(request.url.host)
        await gate.wait()
        return httpx.Response(200, json={"removed": 3})

    runtime.ctx.httpx = httpx.AsyncClient(transport=httpx.MockTransport(remote))
    monkeypatch.setattr(FlowTask, "_running_tasks", [])
    monkeypatch.setattr(FlowTask, "_running_lock", threading.Lock())

    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            _, created = await change(
                owner,
                action="create",
                workflow_id=graph.id,
                trigger="interval",
                interval_num=1,
                interval_unit="days",
            )
            job_id = created["job_id"]
            await settle(engine)
            # fire the registered job exactly as the scheduler would
            job = registered(engine, job_id)
            await job.func(*job.args, **job.kwargs)
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.05)

            reviewed, paused = await change(owner, action="pause", job_id=job_id)
            await settle(engine)

            assert "not cancelled" in reviewed["first_run"]["rule"]
            assert paused["applied"] and paused["schedule"]["state"] == "paused"
            assert registered(engine, job_id) is None
            # pausing stops future triggers but never the run in progress
            assert calls == ["cleanup.test"] and not engine.runs[0].done()
            gate.set()
            await asyncio.gather(*engine.runs)
            log = await FlowLog.get(graph_id=graph.id)
            assert log.exc_info is None and log.ended_at is not None

            enabled_review, enabled = await change(
                owner, action="enable", job_id=job_id
            )
            await settle(engine)
            again = await review(owner, action="enable", job_id=job_id)

            assert "not made up" in " ".join(enabled_review["notes"])
            assert enabled["outcome"] == "enabled"
            assert registered(engine, job_id) is not None
            assert "already enabled" in again["issues"][0]["message"]

    asyncio.run(run())


def test_expired_date(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            paused = await schedule(
                graph,
                state="paused",
                trigger="date",
                run_date=datetime.now(UTC) - timedelta(hours=1),
            )

            reviewed = await review(owner, action="enable", job_id=paused.id)

            assert not reviewed["valid"]
            assert "run_date has passed" in reviewed["issues"][0]["message"]
            assert engine._events.empty()

    asyncio.run(run())


def test_delete(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            _, created = await change(
                owner,
                action="create",
                workflow_id=graph.id,
                trigger="cron",
                cron_expr="0 1 * * *",
            )
            job_id = created["job_id"]
            await settle(engine)
            values = {"action": "delete", "job_id": job_id}
            first = await review(owner, **values)
            second = await review(owner, **values)

            deleted = await apply(owner, first, **values)
            await settle(engine)
            replay = await apply(owner, first, **values)
            other = await apply(owner, second, **values)

            assert "not cancelled" in first["first_run"]["rule"]
            assert first["after"] is None
            assert deleted["applied"] and deleted["outcome"] == "deleted"
            assert deleted["schedule"] is None
            assert registered(engine, job_id) is None
            assert not await FlowJob.exists(id=job_id)
            assert replay["replayed"] and replay["outcome"] == "deleted"
            # another request never reports a deletion it did not perform
            assert other["applied"] is False and other["outcome"] == "absent"
            assert "changed nothing" in other["message"]
            assert await FlowJobReceipt.all().count() == 2
            with pytest.raises(NotFoundException):
                await review(owner, **values)

    asyncio.run(run())


def test_large_parameters(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch):
            graph = await workflow()
            stored = {"blob": "x" * 40000}
            job = await schedule(
                graph, trigger="cron", cron_expr="0 1 * * *", bootparams=stored
            )

            reviewed = await tools.execute_tool(
                "review_workflow_schedule",
                dumps(
                    {"action": "update", "job_id": job.id, "cron_expr": "0 2 * * *"}
                ).decode(),
                owner,
                conversation_id=1,
                call_id="review",
            )
            data = reviewed["data"]
            result = await apply(
                owner, data, action="update", job_id=job.id, cron_expr="0 2 * * *"
            )

            # repeated settings stay within the tool result limit
            assert reviewed["ok"], reviewed
            size = len(dumps(stored))
            assert data["before"]["bootparams"] == {"omitted_bytes": size}
            assert [item["field"] for item in data["changes"]] == ["cron_expr"]
            assert result["schedule"]["bootparams"] == {"omitted_bytes": size}
            assert (await FlowJob.get(id=job.id)).bootparams == stored

    asyncio.run(run())


def test_stale(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            _, created = await change(
                owner,
                action="create",
                workflow_id=graph.id,
                trigger="cron",
                cron_expr="0 1 * * *",
            )
            job_id = created["job_id"]
            await settle(engine)
            values = {"action": "update", "job_id": job_id, "cron_expr": "0 5 * * *"}
            reviewed = await review(owner, **values)
            paused = await review(owner, action="pause", job_id=job_id)
            # another editor saves through the schedule page meanwhile
            await FlowJobService.upsert(
                JobUpsert(
                    id=job_id, graph_id=graph.id, trigger="cron", cron_expr="0 6 * * *"
                )
            )
            await settle(engine)

            result = await apply(owner, reviewed, **values)
            later = await apply(owner, paused, action="pause", job_id=job_id)
            unconfirmed = await apply(owner, reviewed, confirmed=False, **values)

            for outcome in (result, later, unconfirmed):
                assert outcome["applied"] is False and outcome["stale"]
                assert outcome["current"]["cron_expr"] == "0 6 * * *"
            job = await FlowJob.get(id=job_id)
            assert (job.cron_expr, job.state) == ("0 6 * * *", "running")
            assert await FlowJobReceipt.all().count() == 1

            # an edit landing after the recheck is caught inside the transaction
            fresh = await review(owner, **values)
            original = FlowJobService._review.__func__

            async def racing(cls, *args):
                outcome = await original(cls, *args)
                await FlowJob.filter(id=job_id).update(cron_expr="0 8 * * *")
                return outcome

            monkeypatch.setattr(FlowJobService, "_review", classmethod(racing))
            raced = await apply(owner, fresh, **values)

            assert raced["stale"] and raced["current"]["cron_expr"] == "0 6 * * *"
            assert (await FlowJob.get(id=job_id)).cron_expr == "0 8 * * *"

    asyncio.run(run())


def test_conflict(monkeypatch):
    async def run():
        async with database() as (owner, other), scheduling(monkeypatch):
            graph = await workflow()
            job = await schedule(graph)
            values = {"action": "update", "job_id": job.id, "cron_expr": "0 5 * * *"}
            reviewed = await review(owner, **values)

            changed = await apply(owner, reviewed, **{**values, "cron_expr": "0 7 * *"})
            revised = await FlowJobService.apply_change(
                ScheduleApply(
                    request_id=reviewed["request_id"],
                    change=values,
                    revision="0" * 16,
                    confirmed=True,
                ),
                owner,
            )
            guessed = await apply(
                owner, {**reviewed, "request_id": str(uuid4())}, **values
            )
            borrowed = await apply(other, reviewed, **values)
            displayed = await apply(
                owner, reviewed, **{**values, "timezone": "Europe/Paris"}
            )

            for result in (changed, revised, guessed, borrowed):
                assert result == {
                    "applied": False,
                    "conflict": True,
                    "message": "This request_id was reviewed for another change or "
                    "revision; review the change again.",
                }
            # the display timezone is not part of the reviewed change
            assert displayed["applied"]
            assert (await FlowJob.get(id=job.id)).cron_expr == "0 5 * * *"

    asyncio.run(run())


def test_confirmation(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            values = {
                "action": "create",
                "workflow_id": graph.id,
                "trigger": "cron",
                "cron_expr": "0 1 * * *",
            }
            reviewed = await review(owner, **values)

            result = await apply(owner, reviewed, confirmed=False, **values)

            assert result["confirmation_required"] and result["applied"] is False
            assert result["request_id"] == reviewed["request_id"]
            assert not await FlowJob.exists() and engine._events.empty()

    asyncio.run(run())


def test_concurrent(monkeypatch):
    async def run():
        from app.core.dl.syncer import submission_lock

        async with database() as (owner, _), scheduling(monkeypatch):
            graph = await workflow()
            values = {
                "action": "create",
                "workflow_id": graph.id,
                "trigger": "cron",
                "cron_expr": "0 1 * * *",
            }
            reviewed = await review(owner, **values)
            key = hashlib.sha256(f"{owner.id}:{reviewed['request_id']}".encode())
            # another worker process holds the request's submission lock
            with submission_lock(f"schedule:{key.hexdigest()}"):
                elsewhere = await apply(owner, reviewed, **values)

            results = await asyncio.gather(
                *(apply(owner, reviewed, **values) for _ in range(3))
            )

            assert elsewhere["pending"] and elsewhere["applied"] is False
            assert sorted(result["replayed"] for result in results) == [
                False,
                True,
                True,
            ]
            assert {result["job_id"] for result in results} == {results[0]["job_id"]}
            assert await FlowJob.all().count() == 1
            assert await FlowJobReceipt.all().count() == 1

    asyncio.run(run())


def test_cancelled(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            values = {
                "action": "create",
                "workflow_id": graph.id,
                "trigger": "cron",
                "cron_expr": "0 1 * * *",
            }
            reviewed = await review(owner, **values)
            reached, release = asyncio.Event(), asyncio.Event()
            add_job = engine.add_job

            async def held(job_id):
                reached.set()
                await release.wait()
                await add_job(job_id)

            engine.add_job = held
            caller = asyncio.create_task(apply(owner, reviewed, **values))
            await reached.wait()

            caller.cancel()
            await asyncio.sleep(0.05)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
            retry = await apply(owner, reviewed, **values)

            # the interrupted call still committed its change and receipt
            assert retry["replayed"] and retry["outcome"] == "created"
            assert await FlowJob.all().count() == 1
            assert engine._events.qsize() + len(engine._scheduler.get_jobs()) == 1

    asyncio.run(run())


class RefusedQueue(queue.Queue):
    """Refuse every queued scheduler event."""

    def put(self, *args, **kwargs):
        """Fail like a closed inter-process queue.

        Args:
            *args: Ignored event.
            **kwargs: Ignored options.

        Raises:
            OSError: Always.
        """
        raise OSError("queue closed")


class RefusedActions(dict):
    """Refuse every scheduler action."""

    def __setitem__(self, key, value):
        """Fail like a stopped shared-state manager.

        Args:
            key: Ignored schedule ID.
            value: Ignored action.

        Raises:
            OSError: Always.
        """
        raise OSError("manager stopped")


@pytest.mark.parametrize(
    ("action", "slot", "refused"),
    [("create", "_events", RefusedQueue), ("pause", "_job_actions", RefusedActions)],
    ids=["create", "pause"],
)
def test_refused_notice(monkeypatch, action, slot, refused):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            job = await schedule(graph)
            values = (
                {
                    "action": "create",
                    "workflow_id": graph.id,
                    "trigger": "cron",
                    "cron_expr": "0 4 * * *",
                }
                if action == "create"
                else {"action": "pause", "job_id": job.id}
            )
            reviewed = await review(owner, **values)
            original = engine.__dict__[slot]
            engine.__dict__[slot] = refused()

            result = await apply(owner, reviewed, **values)
            engine.__dict__[slot] = original
            retried = await apply(owner, reviewed, **values)

            # a refused notice rolls the change back, so the same request may retry
            assert result["applied"] is False
            assert "nothing was saved" in result["message"]
            assert retried["applied"] and not retried["replayed"]
            assert await FlowJob.all().count() == (2 if action == "create" else 1)
            assert await FlowJobReceipt.all().count() == 1

    asyncio.run(run())


def test_uncertain(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch) as engine:
            graph = await workflow()
            values = {
                "action": "create",
                "workflow_id": graph.id,
                "trigger": "cron",
                "cron_expr": "0 1 * * *",
            }
            reviewed = await review(owner, **values)
            create = FlowJobReceipt.create
            attempts = []

            async def flaky(*args, **kwargs):
                attempts.append(kwargs["result"]["outcome"])
                if len(attempts) == 1:
                    raise OSError("disk full")
                return await create(*args, **kwargs)

            monkeypatch.setattr(FlowJobReceipt, "create", flaky)

            uncertain = await apply(owner, reviewed, **values)
            await settle(engine)
            replay = await apply(owner, reviewed, **values)

            assert uncertain["applied"] is None
            assert uncertain["outcome"] == "uncertain"
            assert "instead of repeating" in uncertain["message"]
            assert replay["replayed"] and replay["outcome"] == "uncertain"
            # the scheduler was notified once and never again
            assert attempts == ["created", "uncertain"]
            assert len(engine._scheduler.get_jobs()) == 1
            assert not await FlowJob.exists()

    asyncio.run(run())


def test_permissions(monkeypatch):
    async def run():
        async with database() as (owner, _), scheduling(monkeypatch):
            graph = await workflow()
            job = await schedule(graph)
            account = SimpleNamespace(id=owner.id, role=UserRole.ADMIN)
            reader = SimpleNamespace(
                id=owner.id,
                role=UserRole.USER,
                perms=Permissions(indexer_ids=[graph.id]),
            )
            values = {"action": "pause", "job_id": job.id}
            created, applied = await change(
                account,
                action="create",
                workflow_id=graph.id,
                trigger="cron",
                cron_expr="0 4 * * *",
            )
            reviewed = await review(account, **values)
            create = {
                "action": "create",
                "workflow_id": graph.id,
                "trigger": "cron",
                "cron_expr": "0 4 * * *",
            }

            # ordinary accounts may read schedules but never manage them
            with pytest.raises(ForbiddenException):
                await review(reader, **values)
            account.role = UserRole.USER
            account.perms = Permissions(indexer_ids=[graph.id])
            with pytest.raises(ForbiddenException):
                await apply(account, reviewed, **values)
            with pytest.raises(ForbiddenException):
                await apply(account, created, **create)

            assert applied["applied"]
            assert (await FlowJob.get(id=job.id)).state == "running"
            assert await FlowJobReceipt.all().count() == 1

    asyncio.run(run())


def test_mcp(monkeypatch):
    async def run():
        async with application() as app, scheduling(monkeypatch):
            admin = app.shared_ctx.sessions["admin"]
            graph = await workflow()
            values = {
                "action": "create",
                "workflow_id": graph.id,
                "trigger": "cron",
                "cron_expr": "30 1 * * mon-fri",
                "timezone": "Asia/Shanghai",
            }

            denied = await call(app, "review_workflow_schedule", {"change": values})
            reviewed = await call(
                app, "review_workflow_schedule", {"change": values}, token="admin"
            )
            data = reviewed["structuredContent"]
            application_args = {
                "application": {
                    "request_id": data["request_id"],
                    "change": values,
                    "revision": data["revision"],
                    "confirmed": True,
                }
            }
            read_only = await call(
                app, "apply_workflow_schedule", application_args, token="admin"
            )
            write = await MCPAccessTokenService.create(
                admin.id,
                MCPAccessTokenCreate(name="schedules", scope=MCPTokenScope.READ_WRITE),
            )
            applied = await call(
                app, "apply_workflow_schedule", application_args, token=write["token"]
            )
            replay = await call(
                app, "apply_workflow_schedule", application_args, token=write["token"]
            )
            changed = await call(
                app,
                "apply_workflow_schedule",
                {
                    "application": {
                        **application_args["application"],
                        "change": {**values, "cron_expr": "0 2 * * *"},
                    }
                },
                token=write["token"],
            )

            assert denied["isError"] and "not allowed" in str(denied)
            assert data["valid"] and data["workflow_id"] == graph.id
            assert read_only["isError"] and "read-only" in str(read_only)
            assert applied["structuredContent"]["outcome"] == "created"
            assert replay["structuredContent"]["replayed"]
            assert changed["structuredContent"]["conflict"]
            assert await FlowJob.all().count() == 1

    asyncio.run(run())


def latest_request(body):
    """Read the newest schedule request ID from the model's visible context.

    Args:
        body: Model request, whose tool results may be compacted.

    Returns:
        Request ID of the latest review.
    """
    found = re.findall(
        r'request_id\\?": ?\\?"([0-9a-f-]{36})', dumps(body["messages"]).decode()
    )
    return found[-1]


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_conversation(monkeypatch, verbose):
    streams, offered = [], []
    target = {}

    def offer(body, *calls):
        offered.append(names(body) & tools.SCHEDULE_TOOLS)
        return list(calls)

    steps = [
        # turn 1: resolve the workflow and schedules, review, then wait
        lambda body: offer(
            body,
            ("list_workflows", {"name": "Nightly", "category": "schedule"}),
            ("list_workflow_jobs", {"name": "Nightly"}),
        ),
        lambda body: offer(body, ("review_workflow_schedule", target)),
        lambda body: offer(
            body,
            (
                "apply_workflow_schedule",
                {"request_id": latest_request(body), "confirmed": True},
            ),
        ),
        lambda body: offer(body),
        # turn 2: the user confirms the shown review
        lambda body: offer(
            body,
            (
                "apply_workflow_schedule",
                {"request_id": latest_request(body), "confirmed": True},
            ),
        ),
        lambda body: offer(body),
        # turn 3: a repeated confirmation after a lost response reads the outcome
        lambda body: offer(body, ("list_workflow_jobs", {"name": "Nightly"})),
        lambda body: offer(
            body,
            (
                "apply_workflow_schedule",
                {"request_id": latest_request(body), "confirmed": True},
            ),
        ),
        lambda body: offer(body),
    ]
    configure(monkeypatch, scripted(steps, streams, verbose=verbose))

    async def run():
        async with application() as app, scheduling(monkeypatch) as engine:
            admin = app.shared_ctx.sessions["admin"]
            graph = await workflow()
            target.update(
                action="create",
                workflow_id=graph.id,
                trigger="cron",
                cron_expr="30 1 * * mon-fri",
                timezone="Asia/Shanghai",
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), admin
            )
            first, reviewed = await converse(
                conversation["id"],
                admin,
                "Run Nightly cleanup on weekdays at 09:30 Shanghai time.",
                key="turn-1",
            )
            mcp = await call(
                app,
                "review_workflow_schedule",
                {"change": target},
                token="admin",
            )
            second, confirmed = await converse(
                conversation["id"], admin, "Yes, create it.", key="turn-2"
            )
            await settle(engine)
            third, repeated = await converse(
                conversation["id"], admin, "Did that work? Create it.", key="turn-3"
            )
            duplicate = await review(admin, **target)

            assert (first.event, second.event, third.event) == ("done",) * 3
            # schedule tools appear after listing, or after a shown review
            assert offered[0] == set() and offered[1] == tools.SCHEDULE_TOOLS
            assert offered[4] == tools.SCHEDULE_TOOLS
            assert offered[6] == set() and offered[7] == tools.SCHEDULE_TOOLS
            review_data = reviewed[2]["data"]
            same_turn = reviewed[3]["data"]
            assert review_data["valid"] and review_data["first_run"]["local"]
            assert same_turn["confirmation_required"] and not same_turn["applied"]
            created = confirmed[0]["data"]
            assert created["applied"] and created["outcome"] == "created"
            assert repeated[1]["data"]["replayed"]
            assert repeated[1]["data"]["job_id"] == created["job_id"]
            job = await FlowJob.get(id=created["job_id"])
            assert job.cron_expr == "30 1 * * mon-fri" and job.state == "running"
            assert await FlowJob.all().count() == 1
            assert registered(engine, job.id) is not None
            assert f"Schedule {job.id} already has" in duplicate["issues"][0]["message"]
            # assistant and MCP share the review apart from request identities
            shared = mcp["structuredContent"]
            for result in (review_data, shared):
                result.pop("request_id")
                result["first_run"].pop("estimate")
                result["first_run"].pop("local")
            assert loads(dumps(review_data)) == shared
            # workflow sources are recorded; schedule IDs never are
            for turn in await AssistantTurn.filter(conversation_id=conversation["id"]):
                assert graph.id in turn.resource_ids
                assert job.id not in turn.resource_ids

    asyncio.run(run())


def test_superseded(monkeypatch):
    streams = []
    reviews = {}

    def first_turn(body):
        return [("list_workflow_jobs", {"name": "Nightly"})]

    def reviewed(body):
        return [
            (
                "review_workflow_schedule",
                {"action": "update", "job_id": reviews["job"], "cron_expr": cron},
            )
            for cron in ("0 2 * * *", "0 3 * * *")
        ] + [
            (
                "review_workflow_schedule",
                {"action": "pause", "job_id": reviews["other"]},
            )
        ]

    def confirm(index):
        def step(body):
            # compacted history keeps the newest review; older IDs come from
            # read_conversation_history, which this script stands in for
            return [
                (
                    "apply_workflow_schedule",
                    {"request_id": reviews["ids"][index], "confirmed": True},
                )
            ]

        return step

    steps = [
        first_turn,
        reviewed,
        lambda body: None,
        confirm(0),
        confirm(1),
        confirm(2),
        lambda body: None,
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with application() as app, scheduling(monkeypatch):
            admin = app.shared_ctx.sessions["admin"]
            graph = await workflow()
            job = await schedule(graph)
            other = await schedule(await workflow(name="Weekly report"))
            reviews.update(job=job.id, other=other.id)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), admin
            )
            await converse(conversation["id"], admin, "Move it to 3:00 UTC.", key="a")
            reviews["ids"] = [
                call.result["data"]["request_id"]
                for call in await AssistantToolCall.filter(
                    name="review_workflow_schedule"
                ).order_by("id")
            ]
            done, results = await converse(
                conversation["id"], admin, "Confirmed, both.", key="b"
            )

            older, newer, paused = (result["data"] for result in results)
            assert done.event == "done"
            # a newer review of the same schedule replaces the older one
            assert (
                older["confirmation_required"] and "newer review" in (older["message"])
            )
            assert newer["applied"] and newer["schedule"]["cron_expr"] == "0 3 * * *"
            # reviews of different schedules are confirmed independently
            assert paused["applied"] and paused["outcome"] == "paused"
            assert (await FlowJob.get(id=other.id)).state == "paused"
            calls = await AssistantToolCall.filter(name="apply_workflow_schedule")
            assert len(calls) == 3
            # an unlisted schedule's workflow is recorded from its review alone
            turn = await AssistantTurn.get(request_id="a")
            assert other.graph_id in turn.resource_ids
            assert other.id not in turn.resource_ids

    asyncio.run(run())


def test_page_selection(monkeypatch):
    streams, seen = [], {}

    def listed(body):
        return [("list_workflow_jobs", {"name": "Cleanup"})]

    def selected(body):
        # the schedule page sends its filters and existing selection as context
        context = loads(body["messages"][-2]["content"].split("\n", 1)[1])
        seen["context"] = context
        return [("list_workflow_jobs", {"name": context["filters"]["name"]})]

    def pause(body):
        job_id = seen["context"]["selected_job"]["job_id"]
        return [("review_workflow_schedule", {"action": "pause", "job_id": job_id})]

    def confirm(body):
        return [
            (
                "apply_workflow_schedule",
                {"request_id": latest_request(body), "confirmed": True},
            )
        ]

    steps = [listed, None, selected, pause, None, confirm, None]
    configure(
        monkeypatch,
        scripted([step or (lambda body: None) for step in steps], streams),
    )

    async def run():
        async with application() as app, scheduling(monkeypatch):
            admin = app.shared_ctx.sessions["admin"]
            first = await workflow(name="Cleanup A")
            second = await workflow(name="Cleanup B")
            kept = await schedule(first)
            chosen = await schedule(second)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), admin
            )
            context = {
                "page_type": "workflow_jobs",
                "route": "/settings/workflows/schedules",
                "selected_job": {"job_id": chosen.id, "workflow_id": second.id},
                "filters": {
                    "name": "Cleanup",
                    "state": None,
                    "trigger": None,
                    "ordering": "",
                    "page_num": 1,
                    "page_size": 15,
                },
            }

            _, ambiguous = await converse(
                conversation["id"], admin, "Pause the cleanup schedule.", key="a"
            )
            _, reviewed = await converse(
                conversation["id"],
                admin,
                "Pause the selected one.",
                context=context,
                key="b",
            )
            done, applied = await converse(conversation["id"], admin, "Yes.", key="c")

            # two matches are listed with distinct identities and nothing is reviewed
            items = ambiguous[0]["data"]["items"]
            assert {(item["job_id"], item["workflow_id"]) for item in items} == {
                (kept.id, first.id),
                (chosen.id, second.id),
            }
            assert [result["name"] for result in ambiguous] == ["list_workflow_jobs"]
            assert reviewed[1]["data"]["job_id"] == chosen.id
            assert done.event == "done" and applied[0]["data"]["outcome"] == "paused"
            assert (await FlowJob.get(id=chosen.id)).state == "paused"
            assert (await FlowJob.get(id=kept.id)).state == "running"
            selection = await AssistantTurn.get(request_id="b")
            assert second.id in selection.resource_ids
            assert chosen.id not in selection.resource_ids

            # history of the selected workflow follows current permissions
            admin.role = UserRole.USER
            admin.perms = Permissions(indexer_ids=[first.id])
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": selection.id}).decode(),
                admin,
                conversation_id=conversation["id"],
                call_id="history",
            )
            assert denied["error"]["code"] == "permission_denied"
            refused = await tools.execute_tool(
                "review_workflow_schedule",
                dumps({"action": "enable", "job_id": chosen.id}).decode(),
                admin,
                conversation_id=conversation["id"],
                call_id="review",
            )
            assert refused["error"]["code"] == "forbidden"

    asyncio.run(run())
