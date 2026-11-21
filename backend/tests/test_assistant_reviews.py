"""Test browser decisions against the stored proposal and domain receipts."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from tortoise.exceptions import IntegrityError

from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
)
from app.models.assistant import AssistantConversation, AssistantToolCall, AssistantTurn
from app.models.download import DownloadPlan, DownloadPlanCheck, PlanReviewDecision
from app.models.review_decision import AssistantReviewDecision
from app.models.user import User
from app.routes.assistant import decide_review
from app.services.assistant_reviews import assistant_review
from app.services.download import DownloadPlanService
from app.utils.json import dumps
from tests import test_download_task_actions as tasks
from tests import test_media_changes as media
from tests import test_workflow_schedules as schedules
from tests.test_assistant_persistence import database
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_routes_permissions import application as application
from tests.test_routes_permissions import request

pytestmark = pytest.mark.usefixtures("application", "isolate")


async def proposal(owner, plan):
    conversation = await AssistantConversation.create(user_id=owner.id, title="test")
    turn = await AssistantTurn.create(
        conversation=conversation, request_id="review", prompt="订阅", status="complete"
    )
    check = DownloadPlanCheck(plan=plan)
    data = await DownloadPlanService.validate_creation(check, owner)
    assert data["valid"]
    call = await AssistantToolCall.create(
        turn=turn,
        call_id=uuid4().hex,
        name="validate_download_plan",
        arguments=check.model_dump_json(),
        status="complete",
        result={"ok": True, "data": data},
    )
    return turn, call, data


@pytest.mark.parametrize("approve", [False, True])
def test_subscription(approve):
    async def run():
        async with database() as (owner, other):
            _, _, settings = await fixture_plan()
            _, call, data = await proposal(owner, settings.plan)
            assert (await assistant_review(call.call_id, owner))["status"] == "reviewed"
            with pytest.raises(NotFoundException):
                await assistant_review(call.call_id, other)
            with pytest.raises(BadRequestException):
                await assistant_review(
                    call.call_id,
                    owner,
                    PlanReviewDecision(request_id=uuid4(), approve=True),
                )
            click = PlanReviewDecision(
                request_id=UUID(data["request_id"]), approve=approve
            )
            outcomes = await asyncio.gather(
                *(assistant_review(call.call_id, owner, click) for _ in range(2))
            )
            expected = "applied" if approve else "cancelled"
            assert {r["status"] for r in outcomes} <= {expected, "pending"}
            editor = settings.model_copy(update={"request_id": click.request_id})
            response = await DownloadPlanService.create_once(editor, owner)
            assert response["created"] is approve
            assert await DownloadPlan.all().count() == int(approve)
            assert (await assistant_review(call.call_id, owner))["status"] == expected
            if approve:
                assert (await assistant_review(call.call_id, owner))["outcome"][
                    "plan_id"
                ] == response["plan_id"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "state", "expected"),
    [
        ("complete", "pending", "superseded"),
        ("complete", "answered", "superseded"),
        ("complete", "deferred", "superseded"),
        ("error", None, "reviewed"),
    ],
)
def test_subscription_question(status, state, expected):
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan()
            turn, call, data = await proposal(owner, settings.plan)
            await AssistantToolCall.create(
                turn=turn,
                call_id=uuid4().hex,
                name="ask_user",
                arguments="{}",
                status=status,
                result={"ok": state is not None, "data": {"state": state}},
            )

            assert (await assistant_review(call.call_id, owner))["status"] == expected
            if expected == "superseded":
                approve = PlanReviewDecision(
                    request_id=UUID(data["request_id"]), approve=True
                )
                assert (await assistant_review(call.call_id, owner, approve))[
                    "status"
                ] == expected
                assert not await DownloadPlan.exists()

    asyncio.run(run())


def test_subscription_history():
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan()
            turn, call, data = await proposal(owner, settings.plan)
            later = await AssistantTurn.create(
                conversation_id=turn.conversation_id,
                request_id="other",
                prompt="What does the interval mean?",
                status="complete",
            )
            await AssistantToolCall.create(
                turn=later,
                call_id=uuid4().hex,
                name="ask_user",
                arguments="{}",
                status="complete",
                result={"ok": True, "data": {"state": "pending"}},
            )
            assert (await assistant_review(call.call_id, owner))["status"] == "reviewed"
            approve = PlanReviewDecision(
                request_id=UUID(data["request_id"]), approve=True
            )
            applied = await assistant_review(call.call_id, owner, approve)
            await AssistantToolCall.create(
                turn=turn,
                call_id=uuid4().hex,
                name="ask_user",
                arguments="{}",
                status="complete",
                result={"ok": True, "data": {"state": "answered"}},
            )
            assert (await assistant_review(call.call_id, owner))["outcome"][
                "plan_id"
            ] == applied["outcome"]["plan_id"]

    asyncio.run(run())


def test_superseded():
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan()
            turn, call, data = await proposal(owner, settings.plan)
            await AssistantToolCall.create(
                turn=turn,
                call_id=uuid4().hex,
                name=call.name,
                arguments=call.arguments,
                status="complete",
                result={"ok": True, "data": {"valid": False}},
            )
            decide = PlanReviewDecision(
                request_id=UUID(data["request_id"]), approve=True
            )
            assert (await assistant_review(call.call_id, owner, decide))[
                "status"
            ] == "superseded"
            assert not await DownloadPlan.all().exists()
            await User.filter(id=owner.id).update(role="user")
            with pytest.raises(ForbiddenException):
                await assistant_review(call.call_id, owner)
            with pytest.raises(UnauthorizedException):
                await decide_review(
                    request(None, "POST", decide.model_dump(mode="json")), call.call_id
                )

    asyncio.run(run())


async def click(owner, name, arguments, data):
    conversation = await AssistantConversation.create(user_id=owner.id, title="review")
    turn = await AssistantTurn.create(
        conversation=conversation, request_id="review", prompt="操作", status="complete"
    )
    call = await AssistantToolCall.create(
        turn=turn,
        call_id=uuid4().hex,
        name=name,
        arguments=dumps(arguments).decode(),
        status="complete",
        result={"ok": True, "data": data},
    )
    decision = PlanReviewDecision(request_id=UUID(data["request_id"]), approve=True)
    first = await assistant_review(call.call_id, owner, decision)
    assert first["status"] == "applied", first
    second = await assistant_review(call.call_id, owner, decision)
    assert first["outcome"]["outcome"] == second["outcome"]["outcome"]
    return first["outcome"]


def test_schedule(monkeypatch):
    async def run():
        async with database() as (owner, _), schedules.scheduling(monkeypatch):
            graph = await schedules.workflow()
            args = {
                "action": "create",
                "workflow_id": graph.id,
                "trigger": "interval",
                "interval_num": 6,
                "interval_unit": "hours",
                "timezone": "Asia/Shanghai",
            }
            data = await schedules.review(owner, **args)
            result = await click(owner, "review_workflow_schedule", args, data)
            assert result["job_id"]

    asyncio.run(run())


def test_task_action(monkeypatch):
    async def run():
        async with tasks.downloads(monkeypatch) as env:
            args = {"action": "pause", "task_id": env.tasks["Ubuntu 24.04"].id}
            data = await tasks.review(env.owner, **args)
            result = await click(env.owner, "review_download_task_action", args, data)
            assert result["task_id"] == args["task_id"]

    asyncio.run(run())


def test_scan(monkeypatch, tmp_path):
    async def run():
        async with media.media(monkeypatch, tmp_path) as env:
            args = {"action": "scan", "library_id": env.movies.id}
            data = await media.scan_review(env.owner, env.movies.id)
            result = await click(env.owner, "review_library_scan", args, data)
            assert result["outcome"] == "scanned"

    asyncio.run(run())


def test_future_subscription():
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan(
                interval_start=datetime.now(UTC) + timedelta(days=1)
            )
            _, call, data = await proposal(owner, settings.plan)
            decision = PlanReviewDecision(
                request_id=UUID(data["request_id"]), approve=True
            )
            first = await assistant_review(call.call_id, owner, decision)
            refreshed = await assistant_review(call.call_id, owner)
            assert (
                first["outcome"]["first_execution"]
                == refreshed["outcome"]["first_execution"]
                == {"status": "waiting"}
            )

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_retry_subscription(monkeypatch, cancel):
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan()
            _, call, data = await proposal(owner, settings.plan)
            click = PlanReviewDecision(
                request_id=UUID(data["request_id"]), approve=True
            )
            body = settings.model_copy(update={"request_id": click.request_id})
            with monkeypatch.context() as patch:
                patch.setattr(
                    DownloadPlan,
                    "create",
                    AsyncMock(side_effect=IntegrityError("constraint")),
                )
                with pytest.raises(IntegrityError):
                    await DownloadPlanService.create_once(body, owner)
            state = await assistant_review(call.call_id, owner)
            assert state["status"] == "failed" and state["retryable"]
            decision = click.model_copy(update={"approve": not cancel})
            first, second = await asyncio.gather(
                *(assistant_review(call.call_id, owner, decision) for _ in range(2))
            )
            assert {first["status"], second["status"]} <= (
                {"cancelled"} if cancel else {"applied", "pending"}
            )
            assert (await assistant_review(call.call_id, owner))["status"] == (
                "cancelled" if cancel else "applied"
            )
            assert await DownloadPlan.all().count() == int(not cancel)

    asyncio.run(run())


def test_forbidden_before_claim(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan()
            _, call, data = await proposal(owner, settings.plan)
            click = PlanReviewDecision(
                request_id=UUID(data["request_id"]), approve=True
            )
            with monkeypatch.context() as patch:
                patch.setattr(
                    DownloadPlanService,
                    "validate_creation",
                    AsyncMock(side_effect=ForbiddenException),
                )
                with pytest.raises(ForbiddenException):
                    await assistant_review(call.call_id, owner, click)
            assert not await AssistantReviewDecision.exists()
            assert (await assistant_review(call.call_id, owner, click))[
                "status"
            ] == "applied"

    asyncio.run(run())


@pytest.mark.parametrize("browser", [True, False])
def test_demotion_during_validation(monkeypatch, browser):
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan()
            _, call, data = await proposal(owner, settings.plan)
            click = PlanReviewDecision(
                request_id=UUID(data["request_id"]), approve=True
            )
            original = DownloadPlanService.validate_creation

            async def demote(body, user):
                valid = await original(body, user)
                await User.filter(id=user.id).update(role="user")
                return valid

            monkeypatch.setattr(DownloadPlanService, "validate_creation", demote)
            with pytest.raises(ForbiddenException):
                if browser:
                    await assistant_review(call.call_id, owner, click)
                else:
                    await DownloadPlanService.create_once(settings, owner)
            assert (
                not await DownloadPlan.exists()
                and not await AssistantReviewDecision.exists()
            )

    asyncio.run(run())
