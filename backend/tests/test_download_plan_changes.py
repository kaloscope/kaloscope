"""Unit tests for reviewed download plan updates and deletions."""

import asyncio
import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.ai import tools
from app.core.dl import syncer
from app.core.dl.driver import DownloadSnapshot, DownloadState
from app.core.exceptions import ForbiddenException, NotFoundException
from app.models.assistant import AssistantToolCall, AssistantTurn, ConversationCreate
from app.models.download import (
    DownloadPlan,
    DownloadPlanApply,
    DownloadPlanChange,
    DownloadPlanHistory,
    DownloadPlanReceipt,
    DownloadTask,
    IndexerSearch,
)
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import MediaLib
from app.models.user import Permissions, UserRole
from app.services import download as service
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_conversation import engine_fixture
from tests.test_mcp import application, call
from tests.test_services_assistant import configure
from tests.test_workflow_runs import converse, names, scripted

LAST_EXEC = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


async def stored_plan(**overrides):
    """Store a plan whose ID differs from its workflow and downloader IDs.

    Args:
        **overrides: Stored fields that differ from the valid fixture.

    Returns:
        Indexer, downloader and the stored plan.
    """
    graph, downloader, body = await fixture_plan()
    plan = await DownloadPlan.create(
        id=40, **{**body.plan.model_dump(exclude={"id"}), **overrides}
    )
    return graph, downloader, plan


async def review(user, **change):
    """Review one change through the shared service.

    Args:
        user: Calling account.
        **change: Change fields.

    Returns:
        Shared review.
    """
    return await service.DownloadPlanService.review_change(
        DownloadPlanChange.model_validate(change), user
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
    return await service.DownloadPlanService.apply_change(
        DownloadPlanApply(
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


def paths(result):
    """List the located issues of a review.

    Args:
        result: Review or refused outcome.

    Returns:
        Issue paths.
    """
    return {issue["path"] for issue in result["issues"]}


class LocalDriver:
    """Accept magnet submissions locally, as a reachable downloader would."""

    source_types = frozenset({"magnet"})

    def __init__(self):
        self.submitted = []

    def prepare(self, request):
        return None

    async def add(self, request):
        self.submitted.append(request)
        return DownloadSnapshot(
            identity=replace(request.identity, remote_id=f"r{len(self.submitted)}"),
            state=DownloadState.DOWNLOADING,
        )


def test_update(monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(service, "execute_download_plan", execute)

    async def run():
        async with database() as (owner, _):
            _, downloader, plan = await stored_plan(
                total_count=3,
                last_exec=LAST_EXEC,
                creation_key="k" * 64,
                creation_receipt={"digest": "d", "first_execution": {"added": 3}},
            )
            await DownloadPlanHistory.create(plan=plan, info_hash="a" * 40)
            before = await DownloadPlan.get(id=plan.id).values()

            reviewed, applied = await change(
                owner, action="update", plan_id=plan.id, interval_num=12, batch_limit=4
            )
            stored = await DownloadPlan.get(id=plan.id).values()

            assert reviewed["plan_id"] == 40 and reviewed["workflow_id"] != 40
            assert reviewed["changes"] == [
                {"field": "interval_num", "before": 6, "after": 12},
                {"field": "batch_limit", "before": 2, "after": 4},
            ]
            assert reviewed["before"]["downloader_name"] == downloader.name
            assert reviewed["runtime"]["total_count"] == 3
            assert reviewed["timing"]["before"] == reviewed["timing"]["after"]
            # hourly checks start it after the interval; that is no exact run time
            elapses = datetime.fromisoformat(reviewed["timing"]["interval_elapses_at"])
            assert elapses == LAST_EXEC + timedelta(hours=12)
            assert "search_preview" not in reviewed
            assert any("next attempt" in note for note in reviewed["notes"])
            assert applied["applied"] and applied["outcome"] == "updated"
            assert not applied["replayed"]
            assert applied["plan"]["interval_num"] == 12
            # only the reviewed settings change; counters and records stay
            changed = {key for key in stored if stored[key] != before[key]} - {
                "updated_at"
            }
            assert changed == {"interval_num", "batch_limit"}
            assert await DownloadPlanHistory.filter(plan_id=plan.id).count() == 1
            # an update never starts the creation-time attempt
            execute.assert_not_awaited()
            service.notify_download_changes.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "values,message",
    [
        ({"action": "delete", "plan_id": 1, "dir": "/x"}, "delete takes only"),
        ({"action": "update", "plan_id": 1, "graph_id": 2}, "Extra inputs"),
        ({"action": "update", "plan_id": 0}, "greater than 0"),
        ({"action": "update", "plan_id": 1, "interval_num": 0}, "greater than 0"),
    ],
)
def test_arguments(values, message):
    with pytest.raises(ValidationError, match=message):
        DownloadPlanChange.model_validate(values)


@pytest.mark.parametrize(
    "stored,values,path",
    [
        ({}, {"downloader_id": 999}, "downloader_id"),
        ({}, {"dir": None}, "dir"),
        ({}, {"interval_num": None}, "interval_num"),
        ({}, {"interval_end": "2020-01-01T00:00:00Z"}, "interval_end"),
        ({}, {"interval_start": "2099-01-01T00:00:00"}, "interval_start"),
        (
            {},
            {
                "interval_start": "2100-01-01T00:00:00Z",
                "interval_end": "2099-01-01T00:00:00Z",
            },
            "interval_start",
        ),
        ({}, {"sub_pattern": "(.+)"}, "transfer_lib_id"),
        ({}, {"filters": {"category": "unknown"}}, "filters.category"),
        ({}, {"keyword": "Lantern"}, "action"),
        ({}, {}, "action"),
        # an ended plan is saved again only with a new or cleared end, as in the editor
        (
            {"interval_end": datetime(2020, 1, 1, tzinfo=UTC)},
            {"dir": "/synthetic/other"},
            "interval_end",
        ),
    ],
)
def test_invalid(stored, values, path):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan(**stored)
            before = await DownloadPlan.get(id=plan.id).values()

            reviewed = await review(owner, action="update", plan_id=plan.id, **values)

            assert not reviewed["valid"] and "request_id" not in reviewed
            assert path in paths(reviewed), reviewed["issues"]
            assert await DownloadPlan.get(id=plan.id).values() == before

    asyncio.run(run())


def test_missing():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            reviewed = await review(owner, action="update", plan_id=plan.id, dir="/b")
            await DownloadPlan.filter(id=plan.id).delete()

            with pytest.raises(NotFoundException):
                await review(owner, action="delete", plan_id=plan.id)
            absent = await apply(
                owner, reviewed, action="update", plan_id=plan.id, dir="/b"
            )

            assert absent["outcome"] == "absent" and absent["applied"] is False
            assert not await DownloadPlanReceipt.exists()

    asyncio.run(run())


def test_search(monkeypatch):
    execute = engine_fixture(
        monkeypatch, {"items": [{"id": "r1", "title": "Lantern Two"}], "total": 1}
    )

    async def run():
        async with database() as (owner, _):
            graph, _, plan = await stored_plan()
            values = {
                "action": "update",
                "plan_id": plan.id,
                "keyword": "Lantern Two",
                "filters": {"category": "tv", "quality": ["hd"]},
            }

            reviewed = await review(owner, **values)
            # the review only names the search; previewing it is a separate call
            execute.assert_not_awaited()
            preview = reviewed["search_preview"]
            searched = await service.DownloadPlanService.search_indexer(
                IndexerSearch.model_validate(preview), owner
            )
            graph.state = "draft"
            await graph.save()
            unrelated = await review(
                owner, action="update", plan_id=plan.id, dir="/synthetic/other"
            )
            unpublished = await review(owner, **values)

            assert reviewed["valid"]
            assert preview == {
                "workflow_id": graph.id,
                "keyword": "Lantern Two",
                "filters": {"category": "tv", "quality": ["hd"]},
                "page_size": 2,
            }
            assert any("search_indexer" in note for note in reviewed["notes"])
            assert searched["status"] == "ok" and execute.await_count == 1
            # settings unrelated to the search keep the stored search as it is
            assert unrelated["valid"] and "search_preview" not in unrelated
            assert any("unpublished" in note for note in unrelated["notes"])
            assert unrelated["timing"]["before"] == "indexer_unpublished"
            assert not unpublished["valid"] and "graph_id" in paths(unpublished)
            assert execute.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "stored,values,schedule,note",
    [
        ({"total_count": 3}, {"total_limit": 3}, "limit_reached", "not above the 3"),
        ({"total_count": 3}, {"total_limit": 5}, "active", "At most 2 more"),
        ({}, {"interval_start": "2099-01-01T08:00:00+08:00"}, "not_started", "Waits"),
        (
            {"interval_end": datetime(2020, 1, 1, tzinfo=UTC)},
            {"interval_end": None},
            "active",
            "next attempt",
        ),
    ],
)
def test_timing(stored, values, schedule, note):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan(**stored)

            reviewed = await review(owner, action="update", plan_id=plan.id, **values)

            assert reviewed["valid"], reviewed["issues"]
            assert reviewed["timing"]["after"] == schedule
            assert "exact" in reviewed["timing"]["rule"]
            assert any(note in text for text in reviewed["notes"]), reviewed["notes"]
            if "interval_start" in values:
                assert reviewed["after"]["interval_start"].startswith(
                    "2099-01-01T00:00:00"
                )

    asyncio.run(run())


def test_queued_first_attempt():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan(
                interval_start=datetime(2099, 1, 1, tzinfo=UTC),
                creation_key="k" * 64,
                creation_receipt={
                    "digest": "d",
                    "first_execution": {"status": "queued"},
                },
            )

            reviewed, applied = await change(
                owner, action="update", plan_id=plan.id, interval_start=None
            )
            stored = await DownloadPlan.get(id=plan.id)

            assert "queued first attempt" in reviewed["timing"]["rule"]
            assert "interval_elapses_at" not in reviewed["timing"]
            assert reviewed["runtime"]["first_execution"] == {"status": "queued"}
            # creation recovery still owns the first attempt
            assert stored.creation_key == "k" * 64 and stored.last_exec is None
            assert stored.creation_receipt["first_execution"]["status"] == "queued"
            assert applied["plan"]["schedule"] == "active"

    asyncio.run(run())


def test_transfer():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            library = await MediaLib.create(
                id=60, name="Movies", lib_type="movie", dir="/synthetic/lib", priority=1
            )

            missing = await review(
                owner, action="update", plan_id=plan.id, transfer_lib_id=library.id
            )
            added, applied = await change(
                owner,
                action="update",
                plan_id=plan.id,
                transfer_lib_id=library.id,
                transfer_method="hardlink",
                sub_pattern="(.+)",
                sub_repl="Films/\\1",
            )
            cleared, _ = await change(
                owner, action="update", plan_id=plan.id, transfer_lib_id=None
            )
            stored = await DownloadPlan.get(id=plan.id)

            assert "transfer_method" in paths(missing)
            assert added["after"]["transfer_lib_name"] == "Movies"
            assert added["transfer"]["library"] == "Movies"
            assert any("ingestion bindings" in note for note in added["notes"])
            assert any("Existing tasks keep" in note for note in added["notes"])
            assert applied["plan"]["transfer_method"] == "hardlink"
            # removing the library drops its options, as the editor saves them
            assert {item["field"] for item in cleared["changes"]} == {
                "transfer_lib_id",
                "transfer_method",
                "sub_pattern",
                "sub_repl",
            }
            assert stored.transfer_lib_id is None and stored.transfer_method is None
            assert stored.sub_pattern is None and stored.sub_repl is None

    asyncio.run(run())


def test_delete():
    async def run():
        async with database() as (owner, _):
            _, downloader, plan = await stored_plan(total_count=1, last_exec=LAST_EXEC)
            await DownloadPlanHistory.create(plan=plan, info_hash="a" * 40)
            task = await DownloadTask.create(
                downloader=downloader,
                dir="/synthetic/downloads",
                name="Lantern",
                info_hash="a" * 40,
                state=DownloadState.COMPLETED,
            )
            values = {"action": "delete", "plan_id": plan.id}

            reviewed, deleted = await change(owner, **values)
            repeated = await apply(owner, reviewed, **values)
            with pytest.raises(NotFoundException):
                await review(owner, **values)

            assert reviewed["after"] is None and reviewed["changes"] == []
            assert reviewed["timing"]["after"] == "deleted"
            assert "not cancelled" in reviewed["timing"]["rule"]
            assert any("downloaded files stay" in note for note in reviewed["notes"])
            assert deleted["outcome"] == "deleted" and deleted["plan"] is None
            # the recorded deletion is read back after the plan is gone
            assert repeated["replayed"] and repeated["outcome"] == "deleted"
            assert not await DownloadPlan.exists(id=plan.id)
            assert not await DownloadPlanHistory.exists()
            assert await DownloadTask.filter(id=task.id).exists()
            assert await DownloadPlanReceipt.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("action", ["update", "delete"])
def test_running_attempt(monkeypatch, action):
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
    monkeypatch.setattr(syncer, "notify_download_changes", lambda: None)

    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan(total_count=1, last_exec=LAST_EXEC)
            driver = LocalDriver()
            attempt = asyncio.create_task(syncer.execute_download_plan(plan, driver))
            await started.wait()
            values = {"action": action, "plan_id": plan.id}
            if action == "update":
                values.update(dir="/synthetic/new", batch_limit=5)

            _, applied = await change(owner, **values)
            release.set()
            outcome = (await asyncio.gather(attempt, return_exceptions=True))[0]
            stored = await DownloadPlan.get_or_none(id=plan.id)
            tasks = await DownloadTask.all()

            assert applied["applied"]
            if action == "update":
                # the attempt kept its earlier settings and its counts are kept
                assert outcome["added"] == 2
                assert {task.dir for task in tasks} == {"/synthetic/downloads"}
                assert stored is not None and stored.dir == "/synthetic/new"
                assert stored.batch_limit == 5 and stored.total_count == 3
                assert stored.last_exec > LAST_EXEC
                assert await DownloadPlanHistory.filter(plan_id=plan.id).count() == 2
            else:
                # deletion does not cancel it; it stops at its first history write
                assert isinstance(outcome, Exception) and stored is None
                assert len(driver.submitted) == len(tasks) == 1
                assert not await DownloadPlanHistory.exists()

    asyncio.run(run())


def test_stale():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {"action": "update", "plan_id": plan.id, "batch_limit": 3}
            reviewed = await review(owner, **values)

            # an attempt that only advances counters leaves the review current
            await DownloadPlan.filter(id=plan.id).update(
                total_count=4, last_exec=LAST_EXEC
            )
            current = await review(owner, **values)
            applied = await apply(owner, reviewed, **values)
            later = await review(owner, action="update", plan_id=plan.id, dir="/c")
            # an editor save after the review requires a new review
            await service.DownloadPlanService.upsert(
                service.DownloadPlanUpsert.model_validate(
                    {
                        **service.DownloadPlanService.settings(plan),
                        "id": plan.id,
                        "dir": "/synthetic/editor",
                        "batch_limit": 3,
                    }
                )
            )
            stale = await apply(
                owner, later, action="update", plan_id=plan.id, dir="/c"
            )
            stored = await DownloadPlan.get(id=plan.id)

            assert current["revision"] == reviewed["revision"]
            assert applied["applied"] and applied["plan"]["total_count"] == 4
            assert stale["stale"] and not stale["applied"]
            assert stale["current"]["dir"] == "/synthetic/editor"
            assert stored.dir == "/synthetic/editor" and stored.total_count == 4

    asyncio.run(run())


def test_concurrent_editor(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {"action": "update", "plan_id": plan.id, "batch_limit": 3}
            reviewed = await review(owner, **values)
            original = service.DownloadPlanService._review_change

            async def raced(change, user):
                result = await original(change, user)
                # an editor saves between the recheck and the transaction
                await DownloadPlan.filter(id=plan.id).update(dir="/synthetic/raced")
                return result

            monkeypatch.setattr(service.DownloadPlanService, "_review_change", raced)
            stale = await apply(owner, reviewed, **values)
            stored = await DownloadPlan.get(id=plan.id)

            assert stale["stale"] and not await DownloadPlanReceipt.exists()
            assert stored.dir == "/synthetic/raced" and stored.batch_limit == 2

    asyncio.run(run())


def test_confirmation():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {"action": "delete", "plan_id": plan.id}
            reviewed = await review(owner, **values)

            pending = await apply(owner, reviewed, confirmed=False, **values)
            conflict = await apply(
                owner, reviewed, action="update", plan_id=plan.id, dir="/x"
            )

            assert pending["confirmation_required"] and not pending["applied"]
            assert pending["request_id"] == reviewed["request_id"]
            assert conflict["conflict"] and not conflict["applied"]
            assert await DownloadPlan.exists(id=plan.id)
            assert not await DownloadPlanReceipt.exists()

    asyncio.run(run())


def test_equivalent_offsets():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {
                "action": "update",
                "plan_id": plan.id,
                "interval_start": "2099-01-01T08:00:00+08:00",
            }
            reviewed, applied = await change(owner, **values)

            respelled = await apply(
                owner, reviewed, **{**values, "interval_start": "2099-01-01T00:00Z"}
            )
            stored = await DownloadPlan.get(id=plan.id)

            assert applied["applied"] and respelled["replayed"]
            assert stored.interval_start == datetime(2099, 1, 1, tzinfo=UTC)

    asyncio.run(run())


def test_concurrent():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {"action": "update", "plan_id": plan.id, "batch_limit": 7}
            reviewed = await review(owner, **values)

            results = await asyncio.gather(
                *(apply(owner, reviewed, **values) for _ in range(3))
            )

            assert sorted(result["replayed"] for result in results) == [
                False,
                True,
                True,
            ]
            assert all(result["outcome"] == "updated" for result in results)
            assert await DownloadPlanReceipt.all().count() == 1

    asyncio.run(run())


def test_pending():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {"action": "update", "plan_id": plan.id, "batch_limit": 7}
            reviewed = await review(owner, **values)
            key = hashlib.sha256(
                f"{owner.id}:{reviewed['request_id']}".encode()
            ).hexdigest()

            # another worker holds this request's submission lock
            with syncer.submission_lock(f"plan-change:{key}"):
                pending = await apply(owner, reviewed, **values)
            applied = await apply(owner, reviewed, **values)

            assert pending["pending"] and not pending["applied"]
            assert applied["applied"] and not applied["replayed"]

    asyncio.run(run())


def test_cancelled(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {"action": "delete", "plan_id": plan.id}
            reviewed = await review(owner, **values)
            entered, release = asyncio.Event(), asyncio.Event()
            original = DownloadPlanReceipt.create

            async def slow(**fields):
                entered.set()
                await release.wait()
                return await original(**fields)

            monkeypatch.setattr(DownloadPlanReceipt, "create", slow)
            caller = asyncio.create_task(apply(owner, reviewed, **values))
            await entered.wait()
            caller.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
            # the lost response is read back instead of deleting again
            repeated = await apply(owner, reviewed, **values)

            assert repeated["replayed"] and repeated["outcome"] == "deleted"
            assert not await DownloadPlan.exists(id=plan.id)
            assert await DownloadPlanReceipt.all().count() == 1

    asyncio.run(run())


def test_failed(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {"action": "update", "plan_id": plan.id, "batch_limit": 7}
            reviewed = await review(owner, **values)
            original = DownloadPlanReceipt.create
            monkeypatch.setattr(
                DownloadPlanReceipt,
                "create",
                AsyncMock(side_effect=OSError("disk full")),
            )

            failed = await apply(owner, reviewed, **values)
            unchanged = await DownloadPlan.get(id=plan.id)
            monkeypatch.setattr(DownloadPlanReceipt, "create", original)
            repeated = await apply(owner, reviewed, **values)

            # the change rolled back with its receipt, so repeating applies it once
            assert failed["failed"] and failed["applied"] is False
            assert unchanged.batch_limit == 2
            assert repeated["applied"] and not repeated["replayed"]
            assert (await DownloadPlan.get(id=plan.id)).batch_limit == 7

    asyncio.run(run())


def test_uncertain(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            values = {"action": "update", "plan_id": plan.id, "batch_limit": 7}
            reviewed = await review(owner, **values)
            original = DownloadPlanReceipt.get_or_none
            calls = []

            async def unreadable(**fields):
                calls.append(fields)
                if len(calls) > 1:
                    raise OSError("database unavailable")
                return await original(**fields)

            monkeypatch.setattr(
                DownloadPlanReceipt, "create", AsyncMock(side_effect=OSError("lost"))
            )
            monkeypatch.setattr(DownloadPlanReceipt, "get_or_none", unreadable)
            uncertain = await apply(owner, reviewed, **values)

            assert uncertain["outcome"] == "uncertain" and uncertain["applied"] is None
            assert "instead of repeating" in uncertain["message"]
            assert (await DownloadPlan.get(id=plan.id)).batch_limit == 2

    asyncio.run(run())


def test_permissions():
    async def run():
        async with database() as (owner, _):
            graph, _, plan = await stored_plan()
            library = await MediaLib.create(
                id=60, name="Movies", lib_type="movie", dir="/synthetic/lib", priority=1
            )
            await DownloadPlan.filter(id=plan.id).update(
                transfer_lib_id=library.id, transfer_method="hardlink"
            )
            values = {"action": "update", "plan_id": plan.id, "batch_limit": 3}
            reviewed = await review(owner, **values)
            earlier, _ = await change(owner, action="update", plan_id=plan.id, dir="/d")
            account = SimpleNamespace(
                id=owner.id,
                role=UserRole.USER,
                perms=Permissions(indexer_ids=[graph.id], media_lib_ids=[library.id]),
            )

            # ordinary accounts may list plans but never change them
            with pytest.raises(ForbiddenException):
                await review(account, **values)
            with pytest.raises(ForbiddenException):
                await apply(account, reviewed, **values)
            # nor read a recorded outcome
            with pytest.raises(ForbiddenException):
                await apply(
                    account, earlier, action="update", plan_id=plan.id, dir="/d"
                )

            assert (await DownloadPlan.get(id=plan.id)).batch_limit == 2
            assert await DownloadPlanReceipt.all().count() == 1

    asyncio.run(run())


def latest_request(body):
    """Read the newest plan request ID from the model's visible context.

    Args:
        body: Model request, whose tool results may be compacted.

    Returns:
        Request ID of the latest review.
    """
    found = re.findall(
        r'request_id\\?": ?\\?"([0-9a-f-]{36})', dumps(body["messages"]).decode()
    )
    return found[-1]


def listed(body, keyword):
    """Pick a plan from the newest listing as the model sees it.

    Args:
        body: Model request whose last message is the listing result.
        keyword: Plan keyword to select.

    Returns:
        Matching plan ID; compacted listings no longer show it.
    """
    items = loads(body["messages"][-1]["content"])["data"]["items"]
    return next(item["plan_id"] for item in items if item["keyword"] == keyword)


def lost(body):
    """Fail the provider after a tool result, as a dropped connection would.

    Args:
        body: Model request.

    Raises:
        OSError: Always.
    """
    raise OSError("connection lost")


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_conversation(monkeypatch, verbose):
    streams, offered, catalogs = [], [], []
    target = {}

    def offer(body, *calls):
        offered.append(names(body) & tools.PLAN_CHANGE_TOOLS)
        catalogs.append(names(body))
        return list(calls)

    def confirm(body):
        return offer(
            body,
            (
                "apply_download_plan_change",
                {"request_id": latest_request(body), "confirmed": True},
            ),
        )

    steps = [
        # turn 1: unlock plan tools, find and read the plan, review, then wait
        lambda body: offer(body, ("list_workflows", {"category": "indexer"})),
        lambda body: offer(body, ("list_download_plans", {"keyword": "Lantern"})),
        lambda body: offer(
            body, ("get_download_plan", {"plan_id": listed(body, "Lantern")})
        ),
        lambda body: offer(body, ("review_download_plan_change", target)),
        confirm,
        lambda body: offer(body),
        # turn 2: the user confirms, but the response after the change is lost
        confirm,
        lost,
        # turn 3: rereading workflows keeps the offered changes in a plan focus
        lambda body: offer(body, ("list_workflows", {"category": "indexer"})),
        confirm,
        lambda body: offer(body),
    ]
    configure(monkeypatch, scripted(steps, streams, verbose=verbose))

    async def run():
        async with application() as app:
            admin = app.shared_ctx.sessions["admin"]
            graph, _, plan = await stored_plan(total_count=2, last_exec=LAST_EXEC)
            target.update(
                action="update", plan_id=plan.id, interval_num=12, dir="/synthetic/new"
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), admin
            )
            first, reviewed = await converse(
                conversation["id"],
                admin,
                "Check the Lantern plan every 12 hours and save to /synthetic/new.",
                key="turn-1",
            )
            mcp = await call(
                app, "review_download_plan_change", {"change": target}, token="admin"
            )
            second, confirmed = await converse(
                conversation["id"], admin, "Yes, apply it.", key="turn-2"
            )
            third, repeated = await converse(
                conversation["id"], admin, "Did that work? Apply it.", key="turn-3"
            )
            stored = await DownloadPlan.get(id=plan.id)

            assert (first.event, second.event, third.event) == ("done", "error", "done")
            # listing plans focuses the turn on plan tools, keeping results intact
            assert "list_node_types" in catalogs[1]
            assert "list_node_types" not in catalogs[2]
            assert "get_download_plan" in catalogs[2]
            # change tools appear after reading the plan, or after a shown review
            assert offered[:3] == [set()] * 3
            assert offered[3] == offered[6] == offered[8] == tools.PLAN_CHANGE_TOOLS
            assert offered[9] == tools.PLAN_CHANGE_TOOLS
            assert "list_node_types" not in catalogs[9]
            review_data, same_turn = reviewed[3]["data"], reviewed[4]["data"]
            assert review_data["valid"] and len(review_data["changes"]) == 2
            assert same_turn["confirmation_required"] and not same_turn["applied"]
            assert confirmed[0]["data"]["applied"]
            assert confirmed[0]["data"]["outcome"] == "updated"
            assert repeated[1]["data"]["replayed"]
            assert (stored.interval_num, stored.dir, stored.total_count) == (
                12,
                "/synthetic/new",
                2,
            )
            assert await DownloadPlanReceipt.all().count() == 1
            # assistant and MCP share the review apart from request identities
            shared = mcp["structuredContent"]
            review_data.pop("request_id")
            shared.pop("request_id")
            assert loads(dumps(review_data)) == shared
            # indexers are recorded as sources; plan IDs never are
            for turn in await AssistantTurn.filter(conversation_id=conversation["id"]):
                assert graph.id in turn.resource_ids
                assert plan.id not in turn.resource_ids

    asyncio.run(run())


def test_superseded(monkeypatch):
    streams = []
    reviews = {}

    def reviewed(body):
        return [
            (
                "review_download_plan_change",
                {"action": "update", "plan_id": 40, "batch_limit": limit},
            )
            for limit in (3, 4)
        ] + [("review_download_plan_change", {"action": "delete", "plan_id": 41})]

    def confirm(index):
        def step(body):
            # compacted history keeps the newest review; older IDs come from
            # read_conversation_history, which this script stands in for
            return [
                (
                    "apply_download_plan_change",
                    {"request_id": reviews["ids"][index], "confirmed": True},
                )
            ]

        return step

    steps = [
        lambda body: [("list_workflows", {"category": "indexer"})],
        lambda body: [("get_download_plan", {"plan_id": 40})],
        reviewed,
        lambda body: None,
        confirm(0),
        confirm(1),
        confirm(2),
        lambda body: None,
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            other = await DownloadPlan.create(
                **{**service.DownloadPlanService.settings(plan), "id": 41}
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            await converse(conversation["id"], owner, "Limit it to 4.", key="a")
            reviews["ids"] = [
                call.result["data"]["request_id"]
                for call in await AssistantToolCall.filter(
                    name="review_download_plan_change"
                ).order_by("id")
            ]
            done, results = await converse(
                conversation["id"], owner, "Confirmed, both.", key="b"
            )

            older, newer, deleted = (result["data"] for result in results)
            assert done.event == "done"
            # a newer review of the same plan replaces the older one
            assert older["confirmation_required"] and "newer review" in older["message"]
            assert newer["applied"] and newer["plan"]["batch_limit"] == 4
            # reviews of different plans are confirmed independently
            assert deleted["outcome"] == "deleted"
            assert not await DownloadPlan.exists(id=other.id)
            assert (await DownloadPlan.get(id=plan.id)).batch_limit == 4

    asyncio.run(run())


def test_invalid_review_supersedes():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            turn = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="review-changes",
                status="complete",
                prompt="Change the limit.",
            )
            original = None
            for index, limit in enumerate([3, None]):
                args = {"action": "update", "plan_id": plan.id, "batch_limit": limit}
                result = await review(owner, **args)
                await AssistantToolCall.create(
                    turn_id=turn.id,
                    call_id=f"review-{index}",
                    name="review_download_plan_change",
                    arguments=dumps(args).decode(),
                    status="complete",
                    result={"ok": True, "data": result},
                )
                original = original or result
            confirmed = await tools.apply_plan_change(
                tools.PlanChangeConfirm(
                    request_id=original["request_id"], confirmed=True
                ),
                conversation["id"],
                owner,
            )

            assert confirmed["confirmation_required"] and not confirmed["applied"]
            assert (await DownloadPlan.get(id=plan.id)).batch_limit == 2

    asyncio.run(run())


def test_page_selection(monkeypatch):
    streams, seen = [], {}

    def selected(body):
        # the plan page sends its filters and the plan opened in the editor
        context = loads(body["messages"][-2]["content"].split("\n", 1)[1])
        seen["context"] = context
        return [("list_workflows", {"category": "indexer"})]

    def read(body):
        return [
            (
                "get_download_plan",
                {"plan_id": seen["context"]["selected_plan"]["plan_id"]},
            )
        ]

    def delete(body):
        plan_id = seen["context"]["selected_plan"]["plan_id"]
        return [
            ("review_download_plan_change", {"action": "delete", "plan_id": plan_id})
        ]

    def confirm(body):
        return [
            (
                "apply_download_plan_change",
                {"request_id": latest_request(body), "confirmed": True},
            )
        ]

    steps = [
        lambda body: [("list_workflows", {"category": "indexer"})],
        lambda body: [("list_download_plans", {"keyword": "Lantern"})],
        None,
        selected,
        read,
        delete,
        None,
        confirm,
        None,
    ]
    configure(
        monkeypatch,
        scripted([step or (lambda body: None) for step in steps], streams),
    )

    async def run():
        async with application() as app:
            admin = app.shared_ctx.sessions["admin"]
            graph, _, kept = await stored_plan()
            library = await MediaLib.create(
                id=60, name="Movies", lib_type="movie", dir="/synthetic/lib", priority=1
            )
            chosen = await DownloadPlan.create(
                **{
                    **service.DownloadPlanService.settings(kept),
                    "id": 41,
                    "transfer_lib_id": library.id,
                    "transfer_method": "hardlink",
                }
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), admin
            )
            context = {
                "page_type": "download_plans",
                "route": "/downloads/plans",
                "selected_plan": {"plan_id": chosen.id, "workflow_id": graph.id},
                "filters": {
                    "workflow_id": None,
                    "keyword": "Lantern",
                    "ordering": "",
                    "page_num": 1,
                    "page_size": 15,
                },
            }

            _, ambiguous = await converse(
                conversation["id"], admin, "Delete the Lantern plan.", key="a"
            )
            _, reviewed = await converse(
                conversation["id"],
                admin,
                "Delete the selected one.",
                context=context,
                key="b",
            )
            done, applied = await converse(conversation["id"], admin, "Yes.", key="c")
            mismatch = {
                **context,
                "selected_plan": {"plan_id": chosen.id, "workflow_id": graph.id + 1},
            }
            refused, _ = await converse(
                conversation["id"], admin, "Delete it.", context=mismatch, key="d"
            )

            # a selection that does not match its indexer starts no turn
            assert refused.event == "error" and refused.data["code"] == "bad_request"
            assert not await AssistantTurn.exists(request_id="d")
            # two matches are listed with distinct identities and nothing is reviewed
            items = ambiguous[1]["data"]["items"]
            assert {(item["plan_id"], item["workflow_id"]) for item in items} == {
                (kept.id, graph.id),
                (chosen.id, graph.id),
            }
            assert [result["name"] for result in ambiguous] == [
                "list_workflows",
                "list_download_plans",
            ]
            assert reviewed[2]["data"]["plan_id"] == chosen.id
            assert done.event == "done" and applied[0]["data"]["outcome"] == "deleted"
            assert not await DownloadPlan.exists(id=chosen.id)
            assert await DownloadPlan.exists(id=kept.id)
            selection = await AssistantTurn.get(request_id="b")
            assert graph.id in selection.resource_ids
            assert f"media_lib:{library.id}" in selection.resource_ids
            assert chosen.id not in selection.resource_ids

            # history of the selected plan's library follows current permissions
            admin.role = UserRole.USER
            admin.perms = Permissions(indexer_ids=[graph.id])
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": selection.id}).decode(),
                admin,
                conversation_id=conversation["id"],
                call_id="history",
            )
            assert denied["error"]["code"] == "permission_denied"
            refused = await tools.execute_tool(
                "review_download_plan_change",
                dumps({"action": "delete", "plan_id": kept.id}).decode(),
                admin,
                conversation_id=conversation["id"],
                call_id="review",
            )
            assert refused["error"]["code"] == "forbidden"

    asyncio.run(run())


def test_mcp():
    async def run():
        async with application() as app:
            admin = app.shared_ctx.sessions["admin"]
            _, _, plan = await stored_plan()
            values = {"action": "update", "plan_id": plan.id, "total_limit": 20}

            denied = await call(app, "review_download_plan_change", {"change": values})
            reviewed = await call(
                app, "review_download_plan_change", {"change": values}, token="admin"
            )
            data = reviewed["structuredContent"]
            arguments = {
                "application": {
                    "request_id": data["request_id"],
                    "change": values,
                    "revision": data["revision"],
                    "confirmed": True,
                }
            }
            read_only = await call(
                app, "apply_download_plan_change", arguments, token="admin"
            )
            write = await MCPAccessTokenService.create(
                admin.id,
                MCPAccessTokenCreate(name="plans", scope=MCPTokenScope.READ_WRITE),
            )
            applied = await call(
                app, "apply_download_plan_change", arguments, token=write["token"]
            )
            replay = await call(
                app, "apply_download_plan_change", arguments, token=write["token"]
            )
            changed = await call(
                app,
                "apply_download_plan_change",
                {
                    "application": {
                        **arguments["application"],
                        "change": {**values, "total_limit": 30},
                    }
                },
                token=write["token"],
            )

            assert denied["isError"] and "not allowed" in str(denied)
            assert data["valid"] and data["plan_id"] == plan.id
            assert read_only["isError"] and "read-only" in str(read_only)
            assert applied["structuredContent"]["outcome"] == "updated"
            assert replay["structuredContent"]["replayed"]
            assert changed["structuredContent"]["conflict"]
            assert (await DownloadPlan.get(id=plan.id)).total_limit == 20

    asyncio.run(run())
