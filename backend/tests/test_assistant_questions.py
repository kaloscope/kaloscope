"""Unit tests for assistant choice cards and their one-time continuation."""

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.models.assistant import AssistantToolCall, ConversationCreate
from app.models.assistant_questions import AnswerGroup, AskUser, QuestionAnswer
from app.models.download import DownloadPlan
from app.models.user import Permissions
from app.services.assistant_questions import (
    answer_card,
    build_card,
    continuation_prompt,
    validate_answer,
)
from app.services.assistant_sessions import AssistantSessionService as Sessions
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate

CHOICE = {
    "title": "Tags?",
    "multiple": True,
    "options": [{"label": "Anime", "detail": "Use the Anime tag."}],
}


@pytest.mark.parametrize(
    ("qformat", "selected", "custom", "issue"),
    [
        ("text", ["one"], "", None),
        ("text", ["one"], "Other", "invalid"),
        ("text", ["unknown"], "", "invalid"),
        ("text", [], "", "empty"),
        ("datetime", [], "2026-12-01T09:00:00", "datetime"),
        ("datetime", [], "2026-12-01T09:00:00+08:00", None),
    ],
)
def test_validation(qformat, selected, custom, issue):
    q = {"format": qformat, "multiple": False, "options": [{"id": "one", "label": "A"}]}
    answer = QuestionAnswer(id="q", selected=selected, custom=custom)
    assert validate_answer(q, answer)[1] == issue


@pytest.mark.parametrize(
    "raw", [{}, {"plan_ids": [1]}, {"plan_ids": [2, 2]}, {"questions": [CHOICE] * 4}]
)
def test_group_limits(raw):
    with pytest.raises(ValidationError):
        AskUser.model_validate(raw)


def test_owned_plan_question():
    async def run():
        async with database() as (owner, other):
            graph, _, template = await fixture_plan()
            plans = [
                await DownloadPlan.create(
                    id=k, **template.plan.model_dump(exclude={"id"})
                )
                for k in (7, 8)
            ]
            conv = (
                await Sessions.create(
                    ConversationCreate.model_validate({"model": MODEL}), owner
                )
            )["id"]
            turn = await Sessions.begin(conv, request(), owner)
            call_id = uuid4().hex
            card = await build_card(
                AskUser.model_validate({"plan_ids": [7, 8], "questions": [CHOICE]}),
                owner,
            )
            await AssistantToolCall.create(
                turn_id=turn.id,
                call_id=call_id,
                name="ask_user",
                arguments="{}",
                status="complete",
                result={"ok": True, "data": card, "id": call_id, "name": "ask_user"},
            )
            await Sessions.record_resources(turn, card["resources"])
            await Sessions.finish(turn, status="complete")
            payload = {
                "version": card["version"],
                "items": [
                    {"id": "plan", "skipped": True},
                    {"id": "q1", "selected": ["o1"], "custom": "Drama"},
                ],
            }

            async def submit():
                return await answer_card(
                    conv, call_id, AnswerGroup.model_validate(payload), owner
                )

            assert card["questions"][0]["options"][0]["detail"] == graph.name
            with pytest.raises(NotFoundException):
                await answer_card(
                    conv, call_id, AnswerGroup.model_validate(payload), other
                )
            assert (await submit())["card"]["state"] == "deferred"
            assert (await Sessions.turns(conv, owner))["items"][0]["tools"][0][
                "result"
            ]["data"]["state"] == "deferred"
            key = f"answer-{call_id}"
            with pytest.raises(KaloscopeException):
                await Sessions.begin(conv, request(key, answer_to=call_id), owner)
            payload["items"][0] = {"id": "plan", "custom": plans[0].keyword}
            assert (await submit())["issue"] == "plan"
            payload["items"][0] = {"id": "plan", "custom": "#8"}
            payload["items"][1]["selected"] = ["invalid"]
            assert await submit() == {"id": "q1", "issue": "invalid"}
            assert (await AssistantToolCall.get(call_id=call_id)).result["data"][
                "state"
            ] == "deferred"
            payload["items"][1]["selected"] = ["o1"]
            await DownloadPlan.filter(id=8).update(keyword="changed")
            assert await submit() == {"id": "plan", "issue": "expired"}
            await DownloadPlan.filter(id=8).update(keyword=plans[0].keyword)
            owner.role = "user"
            owner.perms = Permissions()
            with pytest.raises(ForbiddenException):
                await submit()
            with pytest.raises(ForbiddenException):
                await build_card(AskUser(plan_ids=[7, 8]), owner)
            owner.role = "admin"
            receipts = await asyncio.gather(submit(), submit())
            assert receipts[0] == receipts[1]
            summary = receipts[0]["card"]["summary"]
            assert summary[0]["values"][0].startswith("#8 · ")
            assert summary[1]["values"] == ["Anime", "Drama"]
            resumed = await Sessions.begin(
                conv, request(key, answer_to=call_id, prompt="yes"), owner
            )
            assert "#8" in resumed.prompt and "yes" not in resumed.prompt
            await Sessions.finish(resumed, status="error")
            retry = await Sessions.begin(
                conv,
                request("retry-answer", retry_of=resumed.id, prompt="approve"),
                owner,
            )
            assert retry.prompt == resumed.prompt and retry.retry_of == resumed.id
            await Sessions.finish(retry, status="error")
            latest = await Sessions.begin(
                conv, request("another-retry", retry_of=retry.id), owner
            )
            assert latest.prompt == resumed.prompt and latest.retry_of == resumed.id
            await Sessions.finish(latest, status="complete")
            with pytest.raises(KaloscopeException) as exc:
                await Sessions.begin(conv, request(key, answer_to=call_id), owner)
            assert exc.value.message == "assistant_request_exists"
            assert await DownloadPlan.all().count() == 2
            owner.role = "user"
            owner.perms = Permissions()
            history = (await Sessions.turns(conv, owner))["items"]
            assert [item["is_answer_turn"] for item in history] == [
                False,
                True,
                True,
                True,
            ]
            assert all(not item["prompt"] and not item["tools"] for item in history[1:])

    asyncio.run(run())


@pytest.mark.parametrize(
    ("status", "result"),
    [
        ("running", None),
        ("error", {"ok": False, "error": "invalid_arguments"}),
        ("complete", {"ok": True, "data": None}),
        ("complete", {"ok": True, "data": {"state": "answered"}}),
        ("complete", {"ok": True, "data": {"state": "pending", "summary": []}}),
    ],
)
def test_invalid_continuation(status, result):
    async def run():
        async with database() as (owner, _):
            conv = (
                await Sessions.create(
                    ConversationCreate.model_validate({"model": MODEL}), owner
                )
            )["id"]
            turn = await Sessions.begin(conv, request(), owner)
            await AssistantToolCall.create(
                turn_id=turn.id,
                call_id="card",
                name="ask_user",
                arguments="{}",
                status=status,
                result=result,
            )
            await Sessions.finish(turn, status="complete")

            with pytest.raises(BadRequestException):
                await continuation_prompt(conv, "card", owner)

    asyncio.run(run())
