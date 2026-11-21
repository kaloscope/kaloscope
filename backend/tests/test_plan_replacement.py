"""Unit tests for durable, account-owned subscription replacement."""

import asyncio
import sqlite3
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise, connections, timezone

from app.core.dl import syncer
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
)
from app.models.download import (
    DownloadPlan,
    DownloadPlanDispatch,
    DownloadPlanHistory,
    DownloadPlanReplacement,
    DownloadPlanUpsert,
    DownloadTask,
)
from app.models.user import User
from app.services.download import DownloadPlanService
from app.services.plan_replacement import PlanReplacementService as Replacement
from tests import test_download_plan_changes as edits
from tests.test_assistant_persistence import database
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_changes import LocalDriver, stored_plan

pytestmark = pytest.mark.usefixtures("isolate")


async def decide(preview, owner, approve=True):
    return await Replacement.decide(
        preview["operation_id"], preview["digest"], approve=approve, user=owner
    )


@pytest.mark.parametrize("keep_history", [True, False])
def test_replace_history(keep_history):
    async def run():
        async with database() as (owner, other):
            _, _, plan = await stored_plan(total_count=3)
            await DownloadPlanHistory.create(plan=plan, info_hash="a" * 40)
            a, b = await asyncio.gather(
                *(
                    Replacement.review(plan.id, keep_history=keep_history, user=owner)
                    for _ in range(2)
                )
            )
            assert a == b and a["status"] == "pending"
            with pytest.raises(NotFoundException):
                await Replacement.status(a["operation_id"], other)
            with pytest.raises(BadRequestException):
                await Replacement.decide(
                    a["operation_id"], "0" * 64, approve=True, user=owner
                )
            results = await asyncio.gather(decide(a, owner), decide(a, owner))
            assert results[0] == results[1]
            result = results[0]
            assert result["status"] == "applied"
            assert result["first_execution"] == {"status": "scheduled"}
            new = await DownloadPlan.get(id=result["new_plan_id"])
            assert not await DownloadPlan.exists(id=plan.id)
            assert new.total_count == (3 if keep_history else 0)
            assert await DownloadPlanHistory.filter(plan_id=new.id).count() == int(
                keep_history
            )
            assert await DownloadPlan.all().count() == 1
            assert new.replacement_due == plan.created_at + timedelta(
                hours=plan.interval_num
            )
            assert not await DownloadTask.all().exists()
            await new.delete()
            assert await Replacement.status(a["operation_id"], owner) == result

    asyncio.run(run())


@pytest.mark.parametrize("reason", ["cancel", "expired", "stale", "revoked", "mcp"])
def test_decision(reason):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            a = await Replacement.review(plan.id, user=owner)
            if reason == "expired":
                await DownloadPlanReplacement.filter(
                    operation_id=a["operation_id"]
                ).update(expires_at=timezone.now() - timedelta(minutes=1))
            elif reason == "stale":
                await DownloadPlan.filter(id=plan.id).update(total_count=1)
            elif reason == "revoked":
                await User.filter(id=owner.id).update(role="user")
            elif reason == "mcp":
                owner.mcp_scope = "read_write"
            if reason in {"revoked", "mcp"}:
                with pytest.raises(ForbiddenException):
                    await decide(a, owner)
            else:
                result = await decide(a, owner, approve=reason != "cancel")
                assert result["status"] == {
                    "cancel": "cancelled",
                    "expired": "expired",
                }.get(reason, "conflict")
                if reason == "expired":
                    assert (
                        await DownloadPlanReplacement.get(
                            operation_id=a["operation_id"]
                        )
                    ).decided_at is None
            assert await DownloadPlan.exists(id=plan.id)

    asyncio.run(run())


def test_independent_previews():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            a = await Replacement.review(plan.id, user=owner)
            b = await Replacement.review(plan.id, keep_history=False, user=owner)
            assert a["operation_id"] != b["operation_id"]
            assert (await Replacement.status(a["operation_id"], owner))[
                "status"
            ] == "pending"
            results = await asyncio.gather(decide(a, owner), decide(b, owner))
            assert {entry["status"] for entry in results} == {"applied", "conflict"}
            assert await DownloadPlan.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("decision", [True, False], ids=["approve", "recover"])
def test_competing_sqlite_writer(tmp_path, decision):
    async def run():
        path = tmp_path / "worker.sqlite3"
        await Tortoise.init(
            db_url=f"sqlite://{path}", modules={"models": ["app.models"]}
        )
        try:
            await Tortoise.generate_schemas()
            owner = await User.create(username="Owner", password="unused", role="admin")
            _, _, plan = await stored_plan()
            a = await Replacement.review(plan.id, user=owner)
            if not decision:
                await DownloadPlanReplacement.filter(
                    operation_id=a["operation_id"]
                ).update(status="approved")
            writer_attempt = asyncio.Event()
            loop = asyncio.get_running_loop()

            def mark_write(sql):
                if sql.startswith('UPDATE "download_plan_replacement"'):
                    loop.call_soon_threadsafe(writer_attempt.set)

            async with connections.get("default").acquire_connection() as connection:
                await connection.set_trace_callback(mark_write)
            with sqlite3.connect(path) as other:
                other.execute("BEGIN IMMEDIATE")
                other.execute(
                    "UPDATE user SET avatar = 'fixture' WHERE id = ?", (owner.id,)
                )
                worker = asyncio.create_task(
                    decide(a, owner) if decision else Replacement.recover()
                )
                try:
                    await asyncio.wait_for(writer_attempt.wait(), timeout=3)
                finally:
                    other.commit()
                    await worker
            result = await Replacement.status(a["operation_id"], owner)
            assert result["status"] == "applied"
            assert (
                await DownloadPlan.all().count() == 1
                and not await DownloadPlan.exists(id=plan.id)
            )
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("counted", [False, True])
def test_removed_submission(counted):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan(total_count=int(counted))
            if counted:
                await DownloadPlanHistory.create(plan=plan, info_hash="a" * 40)
            await DownloadPlanDispatch.create(
                key="a",
                plan_id=plan.id,
                info_hash="a" * 40,
                task_id=99,
                counted=counted,
            )
            await syncer.recover_plan_submissions()
            await syncer.recover_plan_submissions()
            assert not await DownloadPlanDispatch.exists()
            assert await DownloadPlanHistory.filter(plan_id=plan.id).count() == 1
            assert (await DownloadPlan.get(id=plan.id)).total_count == 1
            review = await Replacement.review(plan.id, user=owner)
            assert (await decide(review, owner))["status"] == "applied"

    asyncio.run(run())


@pytest.mark.parametrize("items", [[], [{"link": "a" * 40}]])
def test_approval_stops_check(monkeypatch, items):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            preview = await Replacement.review(plan.id, user=owner)

            async def search(**_):
                await DownloadPlanReplacement.filter(
                    operation_id=preview["operation_id"]
                ).update(status="approved")
                return {"items": items}

            monkeypatch.setattr(
                syncer.Sanic,
                "get_app",
                lambda: SimpleNamespace(
                    ctx=SimpleNamespace(flow_engine=SimpleNamespace(execute=search))
                ),
            )
            result = await syncer.execute_download_plan(plan, LocalDriver())
            assert (await DownloadPlan.get(id=plan.id)).last_exec == plan.last_exec
            assert result["status"] == "unconfirmed"
            await Replacement.recover()
            assert (await Replacement.status(preview["operation_id"], owner))[
                "status"
            ] == "applied"

    asyncio.run(run())


@pytest.mark.parametrize("editor", [False, True])
@pytest.mark.parametrize("field", ["interval_num", "interval_start", "keyword"])
def test_reschedule_replacement(editor, field):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            review = await Replacement.review(plan.id, user=owner)
            made = await decide(review, owner)
            plan = await DownloadPlan.get(id=made["new_plan_id"])
            start = timezone.now() + timedelta(days=2)
            value, expected = {
                "interval_num": (25, plan.created_at + timedelta(hours=25)),
                "interval_start": (start, start),
                "keyword": ("New title", plan.replacement_due),
            }[field]
            change = {"action": "update", "plan_id": plan.id, field: value}
            reviewed = await edits.review(owner, **change)
            assert reviewed["timing"]["interval_elapses_at"] == expected.isoformat()
            if editor:
                fields = {
                    **DownloadPlanService.settings(plan),
                    "id": plan.id,
                    field: value,
                }
                await DownloadPlanService.upsert(
                    DownloadPlanUpsert.model_validate(fields)
                )
            else:
                assert (await edits.apply(owner, reviewed, **change))["applied"]
            await plan.refresh_from_db()
            assert plan.replacement_due == expected

    asyncio.run(run())


@pytest.mark.parametrize("deletion", ["replace", "http"])
def test_late_creation(deletion):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            created = await DownloadPlanService.create_once(body, owner)
            if deletion == "replace":
                a = await Replacement.review(created["plan_id"], user=owner)
                assert (await decide(a, owner))["status"] == "applied"
            else:
                # the existing administrator route deletes the matching plan rows
                await DownloadPlan.filter(id=created["plan_id"]).delete()
            for request in (
                DownloadPlanService.create_once(body, owner),
                DownloadPlanService.find_creation(body.request_id, owner),
            ):
                with pytest.raises(NotFoundException):
                    await request
            assert await DownloadPlan.all().count() == (deletion == "replace")

    asyncio.run(run())


@pytest.mark.parametrize(
    "immediate,window,limit",
    [
        (True, False, False),
        (False, False, False),
        (True, True, False),
        (True, False, True),
    ],
)
def test_first_check(monkeypatch, immediate, window, limit):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan(
                interval_start=timezone.now() + timedelta(hours=2) if window else None,
                total_count=10 if limit else 2,
                creation_key="oldkey",
            )
            check = AsyncMock(
                return_value={"status": "completed", "added": 0, "errors": 0}
            )
            driver = SimpleNamespace(supports_version=False, close=AsyncMock())
            monkeypatch.setattr(syncer, "load_driver", lambda _: driver)
            monkeypatch.setattr(syncer, "execute_download_plan", check)
            a = await Replacement.review(plan.id, run_now=immediate, user=owner)
            if limit:
                assert a["after"]["schedule"] == "blocked"
                assert a["after"]["blocked_by"] == "limit_reached"
                assert a["after"]["next_due"] is None
            new = await decide(a, owner)
            await asyncio.gather(
                syncer.check_download_plans(first_only=True),
                syncer.check_download_plans(first_only=True),
            )
            await syncer.check_download_plans()
            expected = immediate and not window and not limit
            assert check.await_count == int(expected), new
            await DownloadPlan.filter(id=new["new_plan_id"]).delete()
            status = await Replacement.status(a["operation_id"], owner)
            assert status["first_execution"]["status"] == (
                "completed" if expected else "blocked" if limit else "scheduled"
            )

    asyncio.run(run())


def test_approved_recovery(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            a = await Replacement.review(plan.id, user=owner)
            dispatch = await DownloadPlanDispatch.create(key="f" * 64, plan_id=plan.id)
            assert (await decide(a, owner))["status"] == "uncertain"
            await syncer.recover_plan_submissions()
            assert not await DownloadPlanDispatch.exists(id=dispatch.id)
            await Replacement.recover()
            assert (await Replacement.status(a["operation_id"], owner))[
                "status"
            ] == "applied"

    asyncio.run(run())


def test_collected_before_replacement(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            started, release = asyncio.Event(), asyncio.Event()

            async def search(**_):
                started.set()
                await release.wait()
                return {"items": [{"link": "a" * 40}, {"link": "b" * 40}]}

            monkeypatch.setattr(
                syncer.Sanic,
                "get_app",
                lambda: SimpleNamespace(
                    ctx=SimpleNamespace(flow_engine=SimpleNamespace(execute=search))
                ),
            )
            driver = LocalDriver()
            check = asyncio.create_task(syncer.execute_download_plan(plan, driver))
            await started.wait()
            a = await Replacement.review(plan.id, user=owner)
            assert (await decide(a, owner))["status"] == "applied"
            release.set()
            assert (await check)["added"] == 0
            assert not driver.submitted
            assert not await DownloadTask.all().exists()

    asyncio.run(run())


def test_in_flight(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            started, release = asyncio.Event(), asyncio.Event()
            driver = LocalDriver()
            original = driver.add

            async def add(request):
                started.set()
                await release.wait()
                return await original(request)

            driver.add = add
            search = AsyncMock(
                return_value={"items": [{"link": "a" * 40}, {"link": "b" * 40}]}
            )
            monkeypatch.setattr(
                syncer.Sanic,
                "get_app",
                lambda: SimpleNamespace(
                    ctx=SimpleNamespace(flow_engine=SimpleNamespace(execute=search))
                ),
            )
            check = asyncio.create_task(syncer.execute_download_plan(plan, driver))
            await started.wait()
            a = await Replacement.review(plan.id, user=owner)
            assert (await decide(a, owner))["status"] == "uncertain"
            await syncer.recover_plan_submissions()
            assert await DownloadPlanDispatch.filter(plan_id=plan.id).count() == 1
            release.set()
            assert (await check)["added"] == 1
            assert len(driver.submitted) == 1
            await Replacement.recover()
            assert (await Replacement.status(a["operation_id"], owner))[
                "status"
            ] == "conflict"
            assert await DownloadPlanHistory.filter(plan_id=plan.id).count() == 1
            assert (await DownloadPlan.get(id=plan.id)).total_count == 1
            a = await Replacement.review(plan.id, user=owner)
            assert (await decide(a, owner))["status"] == "applied"

    asyncio.run(run())


def test_rollback(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            await DownloadPlanHistory.create(plan_id=plan.id, info_hash="a" * 40)
            a = await Replacement.review(plan.id, user=owner)
            monkeypatch.setattr(
                DownloadPlan, "delete", AsyncMock(side_effect=RuntimeError("rollback"))
            )
            assert (await decide(a, owner))["status"] == "failed"
            assert await DownloadPlan.all().count() == 1
            assert await DownloadPlanHistory.filter(plan_id=plan.id).count() == 1
            assert not await DownloadPlanReplacement.filter(
                status__in=["approved", "uncertain"]
            ).exists()

    asyncio.run(run())


@pytest.mark.parametrize("state", ["approved", "uncertain"])
def test_withdraw_waiting(state):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            review = await Replacement.review(plan.id, user=owner)
            assert review["before"]["downloader_name"] == "Fixture downloader"
            assert review["before"]["workflow_name"] == "Published indexer"
            await DownloadPlanReplacement.filter(
                operation_id=review["operation_id"]
            ).update(status=state)
            assert (await decide(review, owner, approve=False))["status"] == "cancelled"
            await Replacement.recover()
            assert await DownloadPlan.exists(id=plan.id)
            assert (await decide(review, owner))["status"] == "cancelled"

    asyncio.run(run())


def test_window_closes_before_due():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan(
                interval_end=timezone.now() + timedelta(minutes=10)
            )
            review = await Replacement.review(plan.id, user=owner)
            assert review["after"]["schedule"] == "blocked"
            assert review["after"]["blocked_by"] == "outside_window"
            new = await decide(review, owner)
            assert new["first_execution"] == {
                "status": "blocked",
                "reason": "outside_window",
            }

    asyncio.run(run())


def test_reset_reopens_limit():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan(total_limit=5, total_count=5)
            review = await Replacement.review(
                plan.id, keep_history=False, run_now=True, user=owner
            )
            assert review["after"]["total_count"] == 0
            assert review["after"]["schedule"] == "immediate"
            assert (await decide(review, owner))["first_execution"] == {
                "status": "queued"
            }

    asyncio.run(run())
