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
        "read_workflow_log",
        "read_workflow_snapshot",
        "read_conversation_history",
        "list_node_types",
        "navigate",
    }
    assert admin_names - read_names == {"validate_workflow", "preview_workflow_draft"}
    assert all("save" not in name and "create" not in name for name in admin_names)


@pytest.mark.parametrize(
    ("name", "arguments", "role", "code"),
    [
        ("save_workflow_draft", "{}", UserRole.ADMIN, ErrorCode.FORBIDDEN),
        ("preview_workflow_draft", "{}", UserRole.USER, ErrorCode.FORBIDDEN),
        ("list_workflows", '{"page_size":101}', UserRole.ADMIN, ErrorCode.BAD_REQUEST),
        (
            "list_workflows",
            '{"ordering":"draft"}',
            UserRole.ADMIN,
            ErrorCode.BAD_REQUEST,
        ),
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
            assert details["data"]["graph_source"] == "saved_draft"
            assert isinstance(details["data"]["created_at"], str)
            assert denied["error"]["code"] == ErrorCode.PERMISSION_DENIED
            assert logs["error"]["code"] == ErrorCode.PERMISSION_DENIED
            assert "Private" not in json.dumps(denied)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("ordering", "expected"),
    [("name", ["Alpha", "Zulu"]), ("-name", ["Zulu", "Alpha"])],
)
def test_list_ordering(ordering, expected):
    async def run():
        async with database():
            for name in ["Zulu", "Alpha"]:
                await FlowGraph.create(
                    name=name, category=GraphCategory.SCHEDULE, state=GraphState.DRAFT
                )

            names = []
            for page in [1, 2]:
                result = await tools.execute_tool(
                    "list_workflows",
                    json.dumps(
                        {
                            "ordering": ordering,
                            "page_num": page,
                            "page_size": 1,
                        }
                    ),
                    ADMIN,
                )
                assert result["ok"] is True
                names.extend(item["name"] for item in result["data"]["items"])

            assert names == expected

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments",
    [
        {"page": "https://example.test"},
        {"page": "workflows", "url": "//example.test"},
        {"page": "workflow_designer"},
        {"page": "workflows", "resource_id": 1},
        {"page": "workflow_designer", "resource_id": True},
        {"page": "workflow_designer", "resource_id": 2**53},
        {"page": "workflow_logs", "resource_id": 1},
    ],
)
def test_invalid_navigation(arguments):
    result = asyncio.run(tools.execute_tool("navigate", json.dumps(arguments), ADMIN))

    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"


def test_navigation_scope():
    async def run():
        async with database():
            graph = await FlowGraph.create(
                name="Visible", category=GraphCategory.INDEXER, state=GraphState.DRAFT
            )
            reader = SimpleNamespace(
                role=UserRole.USER, perms=Permissions(indexer_ids=[graph.id])
            )
            denied = SimpleNamespace(role=UserRole.USER, perms=Permissions())
            args = json.dumps({"page": "workflow_designer", "resource_id": graph.id})

            edit = await tools.execute_tool("navigate", args, ADMIN)
            view = await tools.execute_tool("navigate", args, reader)
            failure = await tools.execute_tool("navigate", args, denied)
            listing = await tools.execute_tool(
                "navigate", '{"page":"workflows"}', reader
            )

            assert edit["data"]["path"] == f"/settings/workflows/graphs/{graph.id}"
            assert view["data"]["path"] == f"/settings/workflows/graphs/r/{graph.id}"
            assert listing["data"]["path"] == "/settings/workflows/graphs"
            assert failure["error"]["code"] == "permission_denied"

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
                json.dumps({"workflow_id": graph.id, "draft_version": 0}),
                ADMIN,
            )
            preview = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps(
                    {
                        "workflow_id": graph.id,
                        "draft_version": 0,
                        "changes": [{"op": "add_node", "node": document["nodes"][0]}],
                    }
                ),
                ADMIN,
            )
            conflict = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps(
                    {
                        "workflow_id": graph.id,
                        "draft_version": 1,
                        "changes": [],
                    }
                ),
                ADMIN,
            )

            assert validation["ok"] is True
            assert validation["data"]["valid"] is False
            assert validation["data"]["readiness"]["status"] == "empty"
            assert preview["ok"] is True
            assert preview["data"]["changes"][0]["kind"] == "node"
            assert preview["data"]["changes"][0]["changed_paths"] == [""]
            assert preview["data"]["changes"][0]["changed_path_count"] == 1
            assert preview["data"]["validation"]["executed"] is False
            assert preview["data"]["validation"]["readiness"]["status"] == "invalid"
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


def test_preview_budget():
    async def run():
        async with database():
            graph = await FlowGraph.create(
                name="Many changes",
                category=GraphCategory.SCHEDULE,
                state=GraphState.DRAFT,
                draft={
                    "nodes": [
                        {
                            "id": str(index),
                            "data": {
                                "$schema": "http",
                                "url": "https://old.test/" + "x" * 700,
                            },
                        }
                        for index in range(40)
                    ],
                    "edges": [],
                },
            )
            result = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps(
                    {
                        "workflow_id": graph.id,
                        "draft_version": 0,
                        "changes": [
                            {
                                "op": "set_field",
                                "node_id": str(index),
                                "field_id": "url",
                                "value": "https://new.test/" + "x" * 700,
                            }
                            for index in range(40)
                        ],
                    }
                ),
                ADMIN,
            )

            assert result["ok"]
            data = result["data"]
            assert data["change_count"] == 40
            assert 0 < len(data["changes"]) < 30
            assert data["omitted_changes"] == 40 - len(data["changes"])
            assert all(
                change["changed_paths"] == ["/data/url"] for change in data["changes"]
            )
            assert len(tools.dumps(data)) <= tools.MAX_TOOL_RESULT_BYTES
            await graph.refresh_from_db()
            assert graph.draft_version == 0

    asyncio.run(run())


def test_result_limit(monkeypatch):
    monkeypatch.setattr(tools, "MAX_TOOL_RESULT_BYTES", 1)
    result = asyncio.run(tools.execute_tool("list_node_types", "{}", ADMIN))

    assert result["error"]["code"] == ErrorCode.ASSISTANT_RESULT_LIMIT


def test_timeout(monkeypatch):
    monkeypatch.setattr(
        tools.FlowGraphService, "get_accessible", AsyncMock(side_effect=TimeoutError)
    )
    result = asyncio.run(tools.execute_tool("get_workflow", '{"workflow_id":1}', ADMIN))

    assert result["error"]["code"] == ErrorCode.AI_TIMEOUT


def test_error_redaction(monkeypatch):
    monkeypatch.setattr(
        tools.FlowGraphService,
        "get_accessible",
        AsyncMock(side_effect=RuntimeError("private-password")),
    )
    result = asyncio.run(tools.execute_tool("get_workflow", '{"workflow_id":1}', ADMIN))

    assert result["error"]["code"] == ErrorCode.INTERNAL_SERVER_ERROR
    assert "private-password" not in json.dumps(result)
