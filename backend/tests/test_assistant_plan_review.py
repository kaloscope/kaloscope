"""Verify durable browser decisions and their shared conversational receipts."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.ai.tools import PlanChangeConfirm, apply_plan_change
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
)
from app.models.assistant import AssistantConversation, AssistantToolCall, AssistantTurn
from app.models.download import (
    DownloadPlan,
    DownloadPlanApply,
    DownloadPlanChange,
    DownloadPlanReceipt,
    PlanReviewDecision,
)
from app.models.user import User
from app.routes.assistant import decide_plan_review, read_plan_review
from app.services.assistant_plan_review import plan_review
from app.services.download import DownloadPlanService
from app.utils.json import loads
from tests.test_assistant_persistence import database
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_changes import stored_plan
from tests.test_routes_permissions import application as application
from tests.test_routes_permissions import request

pytestmark = pytest.mark.usefixtures("application", "isolate")


async def reviewed(owner, plan, action, *, conversation=None):
    if conversation is None:
        conversation = await AssistantConversation.create(
            user_id=owner.id, title="review"
        )
    turn = await AssistantTurn.create(
        conversation=conversation,
        request_id=uuid4().hex,
        prompt="review",
        status="complete",
    )
    args = DownloadPlanChange.model_validate(
        {
            "plan_id": plan.id,
            "action": action,
            **({"interval_num": 12} if action == "update" else {}),
        }
    )
    result = await DownloadPlanService.review_change(args, owner)
    assert result["valid"]
    call = await AssistantToolCall.create(
        turn=turn,
        call_id=uuid4().hex,
        name="review_download_plan_change",
        arguments=args.model_dump_json(exclude_unset=True),
        status="complete",
        result={"ok": True, "data": result},
    )
    body = DownloadPlanApply(
        request_id=result["request_id"],
        revision=result["revision"],
        change=args,
        confirmed=True,
    )
    return conversation, call.call_id, body


@pytest.mark.parametrize("action", ["delete", "update"])
@pytest.mark.parametrize("approve", [True, False])
def test_browser_decision(action, approve):
    async def run():
        async with database() as (owner, other):
            _, _, plan = await stored_plan()
            conv, call_id, body = await reviewed(owner, plan, action)
            first = loads((await read_plan_review(request(owner), call_id)).body)
            assert first["status"] == "reviewed"
            if action == "update":
                assert first["changes"] == [
                    {"field": "interval_num", "before": plan.interval_num, "after": 12}
                ]
            with pytest.raises(NotFoundException):
                await plan_review(call_id, other)
            with pytest.raises(BadRequestException):
                await plan_review(
                    call_id,
                    owner,
                    PlanReviewDecision(request_id=uuid4(), approve=approve),
                )
            payload = {"request_id": str(body.request_id), "approve": approve}
            result, repeated = await asyncio.gather(
                *(
                    decide_plan_review(request(owner, "POST", payload), call_id)
                    for _ in range(2)
                )
            )
            expected = (
                {"update": "updated", "delete": "deleted"}[action]
                if approve
                else "cancelled"
            )
            for response in (result, repeated):
                assert loads(response.body)["status"] == expected
            assert (await plan_review(call_id, owner))["status"] == expected
            text = await apply_plan_change(
                PlanChangeConfirm(request_id=body.request_id, confirmed=True),
                conv.id,
                owner,
            )
            direct = await DownloadPlanService.apply_change(body, owner)
            assert text["outcome"] == direct["outcome"] == expected
            assert text["replayed"] and direct["replayed"]
            stored = await DownloadPlan.get_or_none(id=plan.id)
            if expected == "deleted":
                assert stored is None
            else:
                assert stored.interval_num == (
                    12 if expected == "updated" else plan.interval_num
                )
            assert await DownloadPlanReceipt.all().count() == 1

    asyncio.run(run())


def test_approval_constraints():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            conversation, old, old_review = await reviewed(owner, plan, "delete")
            _, current, body = await reviewed(
                owner, plan, "update", conversation=conversation
            )
            assert (await plan_review(old, owner))["status"] == "superseded"
            rejected = await plan_review(
                old,
                owner,
                PlanReviewDecision(request_id=old_review.request_id, approve=False),
            )
            assert rejected["status"] == "superseded"
            assert not await DownloadPlanReceipt.exists()
            call = await AssistantToolCall.get(call_id=current)
            await AssistantTurn.filter(id=call.turn_id).update(status="interrupted")
            assert (await plan_review(current, owner))["status"] == "incomplete"
            await AssistantTurn.filter(id=call.turn_id).update(status="complete")
            await DownloadPlan.filter(id=plan.id).update(keyword="new")
            assert (await plan_review(current, owner))["status"] == "stale"
            assert not await DownloadPlanReceipt.all().exists()

            for user, exc in (
                (None, UnauthorizedException),
                (SimpleNamespace(role="user"), ForbiddenException),
            ):
                with pytest.raises(exc):
                    await decide_plan_review(
                        request(
                            user,
                            "POST",
                            {"request_id": str(body.request_id), "approve": True},
                        ),
                        current,
                    )
            await User.filter(id=owner.id).update(role="user")
            with pytest.raises(ForbiddenException):
                await plan_review(current, owner)

    asyncio.run(run())


def test_cancel_races_chat():
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            conversation, call_id, body = await reviewed(owner, plan, "delete")
            result, chat = await asyncio.gather(
                plan_review(
                    call_id,
                    owner,
                    PlanReviewDecision(request_id=body.request_id, approve=False),
                ),
                apply_plan_change(
                    PlanChangeConfirm(request_id=body.request_id, confirmed=True),
                    conversation.id,
                    owner,
                ),
            )
            stored = await plan_review(call_id, owner)
            assert stored["status"] == result["status"] == chat["outcome"]
            assert await DownloadPlan.exists(id=plan.id) == (
                stored["status"] == "cancelled"
            )
            assert await DownloadPlanReceipt.all().count() == 1

    asyncio.run(run())


def test_retry_after_failure(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            _, call_id, body = await reviewed(owner, plan, "update")
            decision = PlanReviewDecision(request_id=body.request_id, approve=True)
            with monkeypatch.context() as patch:

                async def offline(**kwargs):
                    raise RuntimeError("write unavailable")

                patch.setattr(DownloadPlanReceipt, "create", offline)
                failed = await plan_review(call_id, owner, decision)
                assert failed["status"] == "failed" and failed["retryable"] is True
            assert (
                await DownloadPlan.get(id=plan.id)
            ).interval_num == plan.interval_num
            assert (await plan_review(call_id, owner))["status"] == "reviewed"
            assert (await plan_review(call_id, owner, decision))["status"] == "updated"

    asyncio.run(run())


def test_lock_failure_retryable(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            _, call_id, body = await reviewed(owner, plan, "update")
            decision = PlanReviewDecision(request_id=body.request_id, approve=True)

            class BrokenLock:
                def __enter__(self):
                    raise OSError("lock directory unavailable")

                def __exit__(self, *args):
                    return False

            with monkeypatch.context() as patch:
                patch.setattr(
                    "app.services.download.submission_lock",
                    lambda _name: BrokenLock(),
                )
                failed = await plan_review(call_id, owner, decision)
                assert failed["status"] == "failed" and failed["retryable"] is True
            assert (
                await DownloadPlan.get(id=plan.id)
            ).interval_num == plan.interval_num
            assert not await DownloadPlanReceipt.exists()
            assert (await plan_review(call_id, owner, decision))["status"] == "updated"

    asyncio.run(run())


@pytest.mark.parametrize(
    "mode", ["unreadable_receipt", "post_dispatch_error", "committed_error"]
)
def test_uncertain_outcome(monkeypatch, mode):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            _, call_id, body = await reviewed(owner, plan, "update")
            decision = PlanReviewDecision(request_id=body.request_id, approve=True)
            if mode == "unreadable_receipt":
                original_get = DownloadPlanReceipt.get_or_none
                reads = 0

                async def broken_receipt(**kwargs):
                    nonlocal reads
                    reads += 1
                    if reads <= 2:
                        return await original_get(**kwargs)
                    raise RuntimeError("receipt lookup failed")

                async def offline(**kwargs):
                    raise RuntimeError("write unavailable")

                monkeypatch.setattr(DownloadPlanReceipt, "create", offline)
                monkeypatch.setattr(DownloadPlanReceipt, "get_or_none", broken_receipt)
            elif mode == "post_dispatch_error":
                monkeypatch.setattr(
                    DownloadPlanService,
                    "apply_change",
                    AsyncMock(side_effect=RuntimeError("transport lost")),
                )
            else:
                original_apply = DownloadPlanService.apply_change

                async def commit_then_raise(apply_body, user):
                    await original_apply(apply_body, user)
                    raise RuntimeError("connection closed after commit")

                monkeypatch.setattr(
                    DownloadPlanService, "apply_change", commit_then_raise
                )

            outcome = await plan_review(call_id, owner, decision)
            if mode == "committed_error":
                assert outcome["status"] == "updated"
                assert "retryable" not in outcome
                assert (await DownloadPlan.get(id=plan.id)).interval_num == 12
            else:
                assert outcome["status"] == "uncertain"
                assert "retryable" not in outcome

    asyncio.run(run())


@pytest.mark.parametrize("missing", [False, True])
def test_dependency_change(monkeypatch, missing):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            _, call_id, body = await reviewed(owner, plan, "update")
            issues = [
                {"path": "downloader_id", "message": "Choose an existing downloader."}
            ]
            apply = AsyncMock(
                side_effect=NotFoundException() if missing else None,
                return_value={"valid": False, "issues": issues},
            )
            monkeypatch.setattr(DownloadPlanService, "apply_change", apply)
            outcome = await plan_review(
                call_id,
                owner,
                PlanReviewDecision(request_id=body.request_id, approve=True),
            )
            assert outcome["status"] == ("stale" if missing else "failed")
            if not missing:
                assert outcome["retryable"] and outcome["issues"] == issues
            assert not await DownloadPlanReceipt.exists()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["delete", "update"])
def test_demotion_before_write(monkeypatch, action):
    async def run():
        async with database() as (owner, _):
            _, _, plan = await stored_plan()
            _, call_id, body = await reviewed(owner, plan, action)
            original = DownloadPlanService._review_change

            async def demote(change, user):
                validation = await original(change, user)
                await User.filter(id=user.id).update(role="user")
                return validation

            monkeypatch.setattr(DownloadPlanService, "_review_change", demote)
            with pytest.raises(ForbiddenException):
                await plan_review(
                    call_id,
                    owner,
                    PlanReviewDecision(request_id=body.request_id, approve=True),
                )
            await plan.refresh_from_db()
            assert plan.interval_num != 12
            assert not await DownloadPlanReceipt.exists()

    asyncio.run(run())
