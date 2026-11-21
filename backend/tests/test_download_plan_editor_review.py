"""Verify the native subscription editor's assistant review provenance."""

import asyncio
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from sanic import Request
from sanic.compat import Header

from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
)
from app.models.assistant import AssistantToolCall
from app.models.download import DownloadPlan, PlanReviewDecision
from app.models.review_decision import AssistantReviewDecision
from app.routes.download import create_plan
from app.services.assistant_reviews import assistant_review
from app.services.download import DownloadPlanService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import database
from tests.test_assistant_reviews import proposal
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_routes_assistant import application as application

pytestmark = pytest.mark.usefixtures("application", "isolate")


async def submit(app, owner, call_id, body):
    """Save the native editor body through its decorated browser endpoint.

    Args:
        app: Isolated test server.
        owner: Browser identity.
        call_id: Review query value, or `None` for the standalone editor.
        body: Final settings and confirmation.

    Returns:
        Parsed domain result.
    """
    path = "/_api/download/plan/create"
    if call_id is not None:
        path += "?" + urlencode({"review": call_id})
    request = Request(
        url_bytes=path.encode(),
        headers=Header({"content-type": "application/json"}),
        version="1.1",
        method="POST",
        transport=None,
        app=app,
    )
    request.body = dumps(body)
    request.ctx.user = owner
    return loads((await create_plan(request)).body)


async def replace_review(turn, call):
    await AssistantToolCall.create(
        turn=turn,
        name=call.name,
        call_id=uuid4().hex,
        arguments=call.arguments,
        result=call.result,
        status="complete",
    )


def test_saved_review(application):
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan()
            turn, call, data = await proposal(owner, settings.plan)
            body = settings.model_dump(mode="json")
            body["request_id"] = data["request_id"]
            body["plan"]["total_limit"] = 22
            first = await submit(application, owner, call.call_id, body)
            await replace_review(turn, call)
            replay = await submit(application, owner, call.call_id, body)

            assert first["created"] and first["plan"]["total_limit"] == 22
            assert replay["replayed"] and replay["plan_id"] == first["plan_id"]
            assert await DownloadPlan.all().count() == 1
            body["plan"]["total_limit"] = 23
            with pytest.raises(BadRequestException):
                await submit(application, owner, call.call_id, body)

    asyncio.run(run())


@pytest.mark.parametrize(
    "state",
    [
        "superseded",
        "cancelled",
        "owner",
        "request",
        "tool",
        "missing",
        "token",
        "user",
        "anonymous",
    ],
)
def test_rejected_origin(application, state):
    async def run():
        async with database() as (owner, other):
            _, _, settings = await fixture_plan()
            turn, call, data = await proposal(owner, settings.plan)
            body = {
                **settings.model_dump(mode="json"),
                "request_id": data["request_id"],
            }
            caller, call_id = owner, call.call_id
            if state == "superseded":
                await replace_review(turn, call)
            elif state == "cancelled":
                decision = PlanReviewDecision(
                    request_id=UUID(data["request_id"]), approve=False
                )
                await assistant_review(call_id, owner, decision)
            elif state == "owner":
                caller = other
            elif state == "request":
                body["request_id"] = str(uuid4())
            elif state == "tool":
                await AssistantToolCall.filter(id=call.id).update(
                    name="validate_download_task"
                )
            elif state == "missing":
                call_id = ""
            elif state == "token":
                caller = SimpleNamespace(
                    id=owner.id, role="admin", mcp_scope="read_write"
                )
            elif state == "user":
                caller = SimpleNamespace(id=owner.id, role="user")
            elif state == "anonymous":
                caller = None
            error = {
                "owner": NotFoundException,
                "missing": NotFoundException,
                "user": ForbiddenException,
                "anonymous": UnauthorizedException,
                "token": ForbiddenException,
            }.get(state, BadRequestException)
            with pytest.raises(error) as denied:
                await submit(application, caller, call_id, body)
            if error is BadRequestException:
                assert denied.value.message == ErrorCode.ASSISTANT_REVIEW_UNAVAILABLE
            assert not await DownloadPlan.all().exists()
            assert await AssistantReviewDecision.all().count() == int(
                state == "cancelled"
            )

    asyncio.run(run())


@pytest.mark.parametrize("channel", ["editor", "button"])
def test_new_review_during_validation(application, monkeypatch, channel):
    async def run():
        async with database() as (owner, _):
            _, _, settings = await fixture_plan()
            turn, call, data = await proposal(owner, settings.plan)
            body = {
                **settings.model_dump(mode="json"),
                "request_id": data["request_id"],
            }
            validate = DownloadPlanService.validate_creation

            async def validate_then_replace(check, account, **kwargs):
                result = await validate(check, account, **kwargs)
                await replace_review(turn, call)
                return result

            monkeypatch.setattr(
                DownloadPlanService, "validate_creation", validate_then_replace
            )
            if channel == "editor":
                with pytest.raises(BadRequestException):
                    await submit(application, owner, call.call_id, body)
            else:
                decision = PlanReviewDecision(
                    request_id=data["request_id"], approve=True
                )
                result = await assistant_review(call.call_id, owner, decision)
                assert result["status"] == "stale"
            assert not await AssistantReviewDecision.all().exists()
            assert not await DownloadPlan.all().exists()

            standalone = await submit(application, owner, None, body)
            assert standalone["created"]

    asyncio.run(run())
