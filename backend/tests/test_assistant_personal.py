"""Exercise personal settings, page context and device action receipts."""

import asyncio

import pytest
from pydantic import ValidationError

from app.core.ai.tools import execute_tool, media_resources
from app.core.exceptions import (
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.middleware import SessionHolder
from app.models.assistant import AssistantToolCall, ConversationCreate, WebSearchContext
from app.models.flow import FlowGraph
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import MediaItem, MediaLib
from app.models.user import DisplayPreference, Permissions
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_mcp import application, call


@pytest.mark.parametrize(
    "change",
    [
        {"key": "read_records", "value": False},
        {"key": "homepage", "value": "//elsewhere"},
        {"key": "vibration", "value": "yes"},
        {"key": "vibration", "value": True, "user_id": 2},
    ],
)
def test_rejected_setting(change):
    with pytest.raises(ValidationError):
        DisplayPreference.model_validate(change)


def test_personal_settings(monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(
                SessionHolder, "get_sessions", lambda: app.shared_ctx.sessions
            )
            admin = app.shared_ctx.sessions["admin"]
            token = (
                await MCPAccessTokenService.create(
                    admin.id,
                    MCPAccessTokenCreate(
                        name="preferences", scope=MCPTokenScope.READ_WRITE
                    ),
                )
            )["token"]
            read = await call(app, "get_personal_preferences", {})
            assert read["structuredContent"]["vibration"] is False
            change = {"key": "vibration", "value": True}
            assert (await call(app, "set_personal_preference", {"change": change}))[
                "isError"
            ]
            saved = await call(
                app, "set_personal_preference", {"change": change}, token=token
            )
            assert saved["structuredContent"]["vibration"] is True
            updated = await execute_tool(
                "get_personal_preferences",
                "{}",
                admin,
                conversation_id=1,
                call_id="pref",
            )
            assert updated["data"] == saved["structuredContent"]
            denied = await execute_tool(
                "set_personal_preference",
                '{"key":"homepage","value":"/downloads"}',
                app.shared_ctx.sessions["reader"],
                conversation_id=1,
                call_id="denied",
            )
            assert not denied["ok"]
            assert (await call(app, "get_personal_preferences", {}))[
                "structuredContent"
            ]["homepage"] == "/dashboard"

    asyncio.run(run())


def test_device_receipt():
    async def run():
        async with database() as (owner, other):
            lib = await MediaLib.create(
                name="Books", lib_type="novel", dir="/books", priority=1
            )
            item = await MediaItem.create(
                lib=lib, name="Book", dir="/books", path="/books/work.txt"
            )
            context = {
                "page_type": "dashboard",
                "route": "/dashboard",
                "read_ids": [item.id],
                "watch_ids": [],
            }
            conv = (
                await Sessions.create(
                    ConversationCreate.model_validate({"model": MODEL}), owner
                )
            )["id"]
            turn = await Sessions.begin(conv, request(context=context), owner)
            assert turn.resource_ids == [f"media_lib:{lib.id}"]
            target = {"page": "resume_reading", "resource_id": item.id}
            result = await execute_tool(
                "navigate",
                dumps(target).decode(),
                owner,
                conversation_id=conv,
                call_id="device",
            )
            assert result["data"]["path"] == f"/medialibs/{lib.id}/{item.id}"
            assert "device_outcome" not in result["data"]
            assert (
                media_resources("navigate", target, result["data"]) == turn.resource_ids
            )
            mismatch = await execute_tool(
                "navigate",
                dumps({**target, "page": "play_video"}).decode(),
                owner,
                conversation_id=conv,
                call_id="wrong",
            )
            assert not mismatch["ok"]
            await AssistantToolCall.create(
                turn_id=turn.id,
                name="navigate",
                call_id="device",
                arguments="{}",
                status="complete",
                result=result,
            )
            await Sessions.finish(turn, status="complete")
            with pytest.raises(NotFoundException):
                await Sessions.record_device_result(conv, "device", True, other)
            assert (
                await Sessions.record_device_result(conv, "device", False, owner)
                == "unconfirmed"
            )
            assert (
                await Sessions.record_device_result(conv, "device", True, owner)
                == "opened"
            )
            assert (
                await Sessions.record_device_result(conv, "device", False, owner)
                == "opened"
            )
            owner.role = "user"
            owner.perms = Permissions()
            with pytest.raises(ForbiddenException):
                await Sessions.record_device_result(conv, "device", True, owner)
            with pytest.raises(ForbiddenException):
                await Sessions.begin(conv, request("revoked", context=context), owner)

    asyncio.run(run())


def test_search_context():
    async def run():
        async with database() as (owner, _):
            graph = await FlowGraph.create(
                name="Search", category="indexer", state="published"
            )
            owner.role = "user"
            owner.perms = Permissions(indexer_ids=[graph.id])
            conv = (
                await Sessions.create(
                    ConversationCreate.model_validate({"model": MODEL}), owner
                )
            )["id"]
            context = {
                "page_type": "web_search",
                "route": f"/websearch/{graph.id}",
                "workflow_id": graph.id,
                "keyword": "fixture",
            }
            turn = await Sessions.begin(conv, request(context=context), owner)
            assert turn.resource_ids == [graph.id]
            assert turn.context == context
            await Sessions.finish(turn, status="complete")
            with pytest.raises(ValidationError):
                WebSearchContext.model_validate(
                    {**context, "route": f"/websearch/{graph.id + 1}"}
                )
            owner.perms = Permissions()
            with pytest.raises(KaloscopeException, match="permission_denied"):
                await Sessions.begin(conv, request("denied", context=context), owner)

    asyncio.run(run())
