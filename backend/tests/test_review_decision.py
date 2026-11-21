"""Exercise competing review decisions across the shared business entry points."""

import asyncio
from uuid import uuid4

import pytest

from app.core.exceptions import ForbiddenException
from app.models.download import (
    DownloadPlan,
    DownloadTask,
    DownloadTaskCheck,
    DownloadTaskCreate,
)
from app.models.flow import FlowJob
from app.models.media import MediaChangeReceipt
from app.models.review_decision import AssistantReviewDecision, review_attempt
from app.models.user import User
from app.services.download import DownloadPlanService, DownloadTaskService
from tests import test_download_task_actions as actions
from tests import test_download_tasks_once as tasks
from tests import test_media_changes as media
from tests import test_workflow_schedules as schedules
from tests.test_assistant_persistence import database
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_tasks_once import sealing as sealing


def test_first_decision():
    async def run():
        async with database() as (owner, other):
            request_id = uuid4()
            decisions = await asyncio.gather(
                *(
                    AssistantReviewDecision.claim(owner.id, request_id, bool(i % 2))
                    for i in range(8)
                )
            )
            assert len({(row.id, row.approved) for row in decisions}) == 1
            winner = decisions[0]
            assert winner.result is None
            later = await AssistantReviewDecision.claim(
                owner.id, str(request_id), not winner.approved
            )
            separate = await AssistantReviewDecision.claim(
                other.id, request_id, not winner.approved
            )
            assert (later.id, later.approved) == (winner.id, winner.approved)
            assert separate.id != winner.id and separate.approved != winner.approved

    asyncio.run(run())


def test_plan_editor(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            preview = await DownloadPlanService.create_once(
                body.model_copy(update={"confirmed": False}), owner
            )
            assert preview["confirmation_required"]
            assert not await AssistantReviewDecision.exists()

            validating = asyncio.Event()
            resume = asyncio.Event()
            validate = DownloadPlanService.validate_creation

            async def paused(check, user):
                validation = await validate(check, user)
                validating.set()
                await resume.wait()
                return validation

            monkeypatch.setattr(DownloadPlanService, "validate_creation", paused)
            editing = asyncio.create_task(DownloadPlanService.create_once(body, owner))
            await asyncio.wait_for(validating.wait(), timeout=5)
            await AssistantReviewDecision.claim(owner.id, body.request_id, False)
            resume.set()
            result = await editing

            assert result["created"] is False and result["outcome"] == "cancelled"
            assert not await DownloadPlan.exists()

    asyncio.run(run())


def test_legacy_plan_receipt():
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            applied = await DownloadPlanService.create_once(body, owner)
            assert applied["created"]
            approved = await AssistantReviewDecision.claim(
                owner.id, body.request_id, False
            )
            assert approved.approved
            # simulate a plan persisted before the shared decision ledger existed
            await approved.delete()
            await AssistantReviewDecision.claim(owner.id, body.request_id, False)

            replayed = await DownloadPlanService.create_once(body, owner)

            assert replayed["replayed"] and replayed["plan_id"] == applied["plan_id"]
            assert await DownloadPlan.all().count() == 1

    asyncio.run(run())


def test_download_submission(monkeypatch):
    driver = tasks.Rpc()
    _, client = tasks.setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await tasks.searched(owner, graph.id, monkeypatch, driver, client)
            fields = tasks.settings(graph.id, items["r1"], downloader.id)
            reviewed = await DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(fields), owner
            )
            body = DownloadTaskCreate(
                request_id=reviewed["request_id"],
                info_hash=reviewed["download"]["info_hash"],
                confirmed=True,
                **fields,
            )
            await AssistantReviewDecision.claim(owner.id, body.request_id, False)

            result = await DownloadTaskService.create_once(body, owner)

            assert result["created"] is False and result["outcome"] == "cancelled"
            assert not driver.added and not await DownloadTask.exists()

    asyncio.run(run())


def test_task_action(monkeypatch):
    async def run():
        async with actions.downloads(monkeypatch) as env:
            change = {"task_id": 21, "action": "pause"}
            reviewed = await actions.review(env.owner, **change)
            await AssistantReviewDecision.claim(
                env.owner.id, reviewed["request_id"], False
            )

            result = await actions.apply(env.owner, reviewed, **change)

            assert result["applied"] is False and result["outcome"] == "cancelled"
            assert not env.remote.requests

    asyncio.run(run())


def test_workflow_schedule(monkeypatch):
    async def run():
        async with (
            database() as (owner, _),
            schedules.scheduling(monkeypatch) as engine,
        ):
            graph = await schedules.workflow()
            change = {
                "action": "create",
                "workflow_id": graph.id,
                "trigger": "cron",
                "cron_expr": "0 1 * * *",
            }
            reviewed = await schedules.review(owner, **change)
            await AssistantReviewDecision.claim(owner.id, reviewed["request_id"], False)

            result = await schedules.apply(owner, reviewed, **change)

            assert result["applied"] is False and result["outcome"] == "cancelled"
            assert not await FlowJob.exists() and engine._events.empty()

    asyncio.run(run())


def test_library_scan(monkeypatch, tmp_path):
    async def run():
        async with media.media(monkeypatch, tmp_path) as env:
            change = {"action": "scan", "library_id": env.movies.id}
            reviewed = await media.scan_review(env.owner, env.movies.id)
            await AssistantReviewDecision.claim(
                env.owner.id, reviewed["request_id"], False
            )

            result = await media.apply(env.owner, reviewed, change)

            assert result["applied"] is False and result["outcome"] == "cancelled"
            assert (
                not await MediaChangeReceipt.exists()
                and not env.watcher._watcher_actions
            )

    asyncio.run(run())


def test_attempt_ownership():
    async def run():
        async with database() as (owner, _):
            key = uuid4()
            with review_attempt():
                first = await AssistantReviewDecision.claim(owner.id, key, True)
                await AssistantReviewDecision.finish(owner.id, key, {"failed": True})
                with review_attempt():
                    next_attempt = await AssistantReviewDecision.claim(
                        owner.id, key, True
                    )
                await AssistantReviewDecision.finish(owner.id, key, {"failed": True})
            await first.refresh_from_db()
            assert (
                first.result == next_attempt.result
                and first.result["status"] == "pending"
            )
            assert (await AssistantReviewDecision.claim(owner.id, key, False)).approved

    asyncio.run(run())


@pytest.mark.parametrize("approve", [False, True])
def test_revoked_claim(approve):
    async def run():
        async with database() as (owner, _):
            await User.filter(id=owner.id).update(role="user")
            with pytest.raises(ForbiddenException):
                await AssistantReviewDecision.claim(owner.id, uuid4(), approve)
            assert not await AssistantReviewDecision.exists()

    asyncio.run(run())
