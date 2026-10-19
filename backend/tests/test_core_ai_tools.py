"""Unit tests for assistant tool grants and shared workflow services."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.ai import tools
from app.core.exceptions import ErrorCode
from app.models.flow import FlowDraftRevision, FlowGraph, GraphCategory, GraphState
from app.models.user import Permissions, UserRole

ADMIN = SimpleNamespace(role=UserRole.ADMIN)


@asynccontextmanager
async def database():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        yield
    finally:
        await Tortoise.close_connections()


def test_grants():
    reader = SimpleNamespace(role=UserRole.USER)
    read_names = {tool["function"]["name"] for tool in tools.tool_schemas(reader)}
    admin_names = {tool["function"]["name"] for tool in tools.tool_schemas(ADMIN)}

    assert read_names == {
        "list_workflows",
        "get_workflow",
        "get_workflow_logs",
        "list_node_types",
    }
    assert admin_names - read_names == {"validate_workflow", "preview_workflow_draft"}
    assert all("save" not in name and "create" not in name for name in admin_names)


@pytest.mark.parametrize(
    ("name", "arguments", "role", "code"),
    [
        ("save_workflow_draft", "{}", UserRole.ADMIN, ErrorCode.FORBIDDEN),
        ("preview_workflow_draft", "{}", UserRole.USER, ErrorCode.FORBIDDEN),
        ("list_workflows", '{"page_size":101}', UserRole.ADMIN, ErrorCode.BAD_REQUEST),
        ("get_workflow", '{"workflow_id":true}', UserRole.ADMIN, ErrorCode.BAD_REQUEST),
        ("get_workflow", '{"workflow_id":"1"}', UserRole.ADMIN, ErrorCode.BAD_REQUEST),
        (
            "get_workflow",
            '{"workflow_id":1,"user_id":2}',
            UserRole.ADMIN,
            ErrorCode.BAD_REQUEST,
        ),
        ("list_workflows", "broken JSON", UserRole.ADMIN, ErrorCode.BAD_REQUEST),
    ],
)
def test_rejected(monkeypatch, name, arguments, role, code):
    read = AsyncMock()
    monkeypatch.setattr(tools.FlowGraphService, "get_details", read)
    result = asyncio.run(
        tools.execute_tool(name, arguments, SimpleNamespace(role=role))
    )

    assert result["ok"] is False
    assert result["error"]["code"] == code
    read.assert_not_awaited()


def test_account_scope():
    async def run():
        async with database():
            visible = await FlowGraph.create(
                name="Visible", category=GraphCategory.INDEXER, state=GraphState.DRAFT
            )
            private = await FlowGraph.create(
                name="Private", category=GraphCategory.SCHEDULE, state=GraphState.DRAFT
            )
            reader = SimpleNamespace(
                role=UserRole.USER, perms=Permissions(indexer_ids=[visible.id])
            )

            listing = await tools.execute_tool("list_workflows", "{}", reader)
            details = await tools.execute_tool(
                "get_workflow", json.dumps({"workflow_id": visible.id}), reader
            )
            denied = await tools.execute_tool(
                "get_workflow", json.dumps({"workflow_id": private.id}), reader
            )
            logs = await tools.execute_tool(
                "get_workflow_logs", json.dumps({"workflow_id": private.id}), reader
            )

            assert listing["ok"] is True
            assert [item["id"] for item in listing["data"]["items"]] == [visible.id]
            assert details["ok"] is True
            assert details["data"]["id"] == visible.id
            assert isinstance(details["data"]["created_at"], str)
            assert denied["error"]["code"] == ErrorCode.PERMISSION_DENIED
            assert logs["error"]["code"] == ErrorCode.PERMISSION_DENIED
            assert "Private" not in json.dumps(denied)

    asyncio.run(run())


def test_preview_does_not_write():
    async def run():
        async with database():
            graph = await FlowGraph.create(
                name="Example",
                category=GraphCategory.SCHEDULE,
                state=GraphState.DRAFT,
                draft={"nodes": [], "edges": []},
            )
            document = {
                "nodes": [{"id": "http", "data": {"$schema": "http", "url": ""}}],
                "edges": [],
            }
            validation = await tools.execute_tool(
                "validate_workflow",
                json.dumps({"workflow_id": graph.id, "document": document}),
                ADMIN,
            )
            preview = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps(
                    {
                        "workflow_id": graph.id,
                        "document": {**document, "draft_version": 0},
                    }
                ),
                ADMIN,
            )
            conflict = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps(
                    {
                        "workflow_id": graph.id,
                        "document": {**document, "draft_version": 1},
                    }
                ),
                ADMIN,
            )

            assert validation["ok"] is True
            assert validation["data"]["valid"] is False
            assert preview["ok"] is True
            assert preview["data"]["changes"][0]["kind"] == "node"
            assert conflict["error"]["code"] == ErrorCode.FLOW_DRAFT_CONFLICT
            await graph.refresh_from_db()
            assert graph.draft == {"nodes": [], "edges": []}
            assert graph.draft_version == 0
            assert await FlowDraftRevision.all().count() == 0

    asyncio.run(run())


def test_node_schema():
    result = asyncio.run(
        tools.execute_tool("list_node_types", '{"node_type":"ai"}', ADMIN)
    )

    assert result["ok"] is True
    assert "ai" in json.dumps(result["data"])


def test_result_limit(monkeypatch):
    monkeypatch.setattr(tools, "MAX_TOOL_RESULT_BYTES", 1)
    result = asyncio.run(tools.execute_tool("list_node_types", "{}", ADMIN))

    assert result["error"]["code"] == ErrorCode.AI_ASSISTANT_LIMIT


def test_timeout(monkeypatch):
    monkeypatch.setattr(
        tools.FlowGraphService, "get_details", AsyncMock(side_effect=TimeoutError)
    )
    result = asyncio.run(tools.execute_tool("get_workflow", '{"workflow_id":1}', ADMIN))

    assert result["error"]["code"] == ErrorCode.AI_TIMEOUT


def test_error_redaction(monkeypatch):
    monkeypatch.setattr(
        tools.FlowGraphService,
        "get_details",
        AsyncMock(side_effect=RuntimeError("private-password")),
    )
    result = asyncio.run(tools.execute_tool("get_workflow", '{"workflow_id":1}', ADMIN))

    assert result["error"]["code"] == ErrorCode.INTERNAL_SERVER_ERROR
    assert "private-password" not in json.dumps(result)
