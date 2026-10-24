"""Unit tests for assistant tool grants and shared workflow services."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.ai import tools
from app.core.exceptions import ErrorCode
from app.models.assistant import (
    AssistantConversation,
    AssistantProposal,
    AssistantSnapshot,
    AssistantToolCall,
    AssistantTurn,
)
from app.models.flow import (
    FlowDraftRevision,
    FlowGraph,
    FlowJob,
    FlowLog,
    GraphCategory,
    GraphState,
)
from app.models.user import Permissions, User, UserInfo, UserRole

ADMIN = cast(UserInfo, SimpleNamespace(id=1, role=UserRole.ADMIN))
CREATE = {"name": "Nightly scan", "category": "schedule", "description": "Scan daily"}


@asynccontextmanager
async def database():
    """Create an isolated database with an owned tool conversation.

    Yields:
        Control while the account and conversation fixtures are available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        await User.create(
            id=ADMIN.id, username="owner", password="unused", role="admin"
        )
        await AssistantConversation.create(id=1, user_id=ADMIN.id, title="Tool fixture")
        yield
    finally:
        await Tortoise.close_connections()


async def record_call(call_id: str, retry_of: AssistantTurn | None = None):
    """Persist a running tool call in a new turn, as the runtime does before execution.

    Args:
        call_id: Server-generated tool call identifier.
        retry_of: Earlier incomplete turn repeated by the new turn.

    Returns:
        The turn that owns the running call.
    """
    turn = await AssistantTurn.create(
        conversation_id=1,
        request_id=call_id,
        prompt="Create a workflow",
        retry_of=retry_of.id if retry_of else None,
    )
    await AssistantToolCall.create(
        turn_id=turn.id, call_id=call_id, name="create_workflow_draft", arguments="{}"
    )
    return turn


async def create(arguments=None, call_id="create", user=ADMIN):
    """Run the creation tool for an already recorded call.

    Args:
        arguments: Tool arguments; the default describes a valid new workflow.
        call_id: Recorded tool call identifier.
        user: Account executing the tool.

    Returns:
        The sanitized tool result.
    """
    return await tools.execute_tool(
        "create_workflow_draft",
        json.dumps(arguments or CREATE),
        user,
        conversation_id=1,
        call_id=call_id,
    )


def test_grants():
    reader = cast(UserInfo, SimpleNamespace(role=UserRole.USER))
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
        "list_media_libraries",
        "list_media_items",
        "get_media_item",
        "list_download_tasks",
        "get_download_task",
        "navigate",
    }
    assert admin_names - read_names == {
        "validate_workflow",
        "preview_workflow_draft",
        "create_workflow_draft",
    }
    assert all("save" not in name and "publish" not in name for name in admin_names)
    # creation accepts metadata only; generated content goes through canvas edits
    assert set(tools.WorkflowCreate.model_fields) == {"name", "category", "description"}


def test_compact_schemas():
    encoded = json.dumps(tools.tool_schemas(ADMIN))
    schemas = {
        tool["function"]["name"]: tool["function"]["parameters"]
        for tool in json.loads(encoded)
    }

    assert '"title": "' not in encoded
    assert '"discriminator"' not in encoded
    assert str(2**53 - 1) not in encoded
    assert '"default": null' not in encoded
    assert all("description" not in schema for schema in schemas.values())
    # categories are documented once; other tools list their values only
    assert "description" in schemas["create_workflow_draft"]["$defs"]["GraphCategory"]
    assert all(
        "description" not in definition
        for name in ("list_workflows", "list_node_types", "list_download_tasks")
        for definition in schemas[name].get("$defs", {}).values()
    )
    state = schemas["list_workflows"]["properties"]["state"]["anyOf"][0]
    assert state["enum"] == ["draft", "modified", "published"]
    assert "Not execution status" in state["description"]
    workflow_id = schemas["get_workflow"]["properties"]["workflow_id"]
    assert schemas["get_workflow"]["required"] == ["workflow_id"]
    assert schemas["get_workflow"]["$defs"]["ResourceID"]["exclusiveMinimum"] == 0
    assert workflow_id == {"$ref": "#/$defs/ResourceID"}
    edits = schemas["preview_workflow_draft"]["$defs"]
    operations = {
        edit["properties"]["op"]["const"]
        for edit in edits.values()
        if "op" in edit.get("properties", {})
    }
    assert operations == {
        "set_field",
        "remove_field",
        "add_node",
        "remove_node",
        "move_node",
        "add_edge",
        "remove_edge",
    }
    assert edits["SetField"]["properties"]["field_id"]["pattern"] == "^[^$].*$"
    assert schemas["list_media_items"]["properties"]["page_size"]["maximum"] == 50


@pytest.mark.parametrize(
    ("name", "arguments", "role", "code"),
    [
        ("save_workflow_draft", "{}", UserRole.ADMIN, ErrorCode.FORBIDDEN),
        ("preview_workflow_draft", "{}", UserRole.USER, ErrorCode.FORBIDDEN),
        (
            "create_workflow_draft",
            '{"name":"New","category":"schedule","description":"Purpose"}',
            UserRole.USER,
            ErrorCode.FORBIDDEN,
        ),
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
        tools.execute_tool(
            name,
            arguments,
            cast(UserInfo, SimpleNamespace(role=role)),
            conversation_id=1,
            call_id="test",
        )
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
            reader = cast(
                UserInfo,
                SimpleNamespace(
                    id=ADMIN.id,
                    role=UserRole.USER,
                    perms=Permissions(indexer_ids=[visible.id]),
                ),
            )

            listing = await tools.execute_tool(
                "list_workflows", "{}", reader, conversation_id=1, call_id="test"
            )
            details = await tools.execute_tool(
                "get_workflow",
                json.dumps({"workflow_id": visible.id}),
                reader,
                conversation_id=1,
                call_id="test",
            )
            denied = await tools.execute_tool(
                "get_workflow",
                json.dumps({"workflow_id": private.id}),
                reader,
                conversation_id=1,
                call_id="test",
            )
            logs = await tools.execute_tool(
                "get_workflow_logs",
                json.dumps({"workflow_id": private.id}),
                reader,
                conversation_id=1,
                call_id="test",
            )

            assert listing["ok"] is True
            assert [item["id"] for item in listing["data"]["items"]] == [visible.id]
            assert details["ok"] is True
            assert details["data"]["id"] == visible.id
            assert details["data"]["graph_source"] == "saved_draft"
            assert isinstance(details["data"]["created_at"], str)
            snapshot = await AssistantSnapshot.get(
                id=details["data"]["base"]["snapshot_id"]
            )
            assert snapshot.conversation_id == 1
            assert snapshot.workflow_id == visible.id
            assert snapshot.document == {"nodes": [], "edges": []}
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
                    conversation_id=1,
                    call_id="test",
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
        {"page": "media_library"},
        {"page": "media_item", "resource_id": "1"},
    ],
)
def test_invalid_navigation(arguments):
    result = asyncio.run(
        tools.execute_tool(
            "navigate", json.dumps(arguments), ADMIN, conversation_id=1, call_id="test"
        )
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "bad_request"


def test_navigation_scope():
    async def run():
        async with database():
            graph = await FlowGraph.create(
                name="Visible", category=GraphCategory.INDEXER, state=GraphState.DRAFT
            )
            reader = cast(
                UserInfo,
                SimpleNamespace(
                    role=UserRole.USER, perms=Permissions(indexer_ids=[graph.id])
                ),
            )
            denied = cast(
                UserInfo, SimpleNamespace(role=UserRole.USER, perms=Permissions())
            )
            args = json.dumps({"page": "workflow_designer", "resource_id": graph.id})

            edit = await tools.execute_tool(
                "navigate", args, ADMIN, conversation_id=1, call_id="test"
            )
            view = await tools.execute_tool(
                "navigate", args, reader, conversation_id=1, call_id="test"
            )
            failure = await tools.execute_tool(
                "navigate", args, denied, conversation_id=1, call_id="test"
            )
            listing = await tools.execute_tool(
                "navigate",
                '{"page":"workflows"}',
                reader,
                conversation_id=1,
                call_id="test",
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
                conversation_id=1,
                call_id="test",
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
                conversation_id=1,
                call_id="test",
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
                conversation_id=1,
                call_id="test",
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
            assert preview["data"]["pre_existing_change_count"] == 0
            assert preview["data"]["change_count"] == 1
            assert "proposal_id" not in preview["data"]
            snapshot = await AssistantSnapshot.get(
                id=preview["data"]["proposal_snapshot_id"]
            )
            assert snapshot.document["nodes"][0]["id"] == "http"
            assert await AssistantProposal.all().count() == 0
            assert conflict["error"]["code"] == ErrorCode.FLOW_DRAFT_CONFLICT
            await graph.refresh_from_db()
            assert graph.draft == {"nodes": [], "edges": []}
            assert graph.draft_version == 0
            assert await FlowDraftRevision.all().count() == 0

    asyncio.run(run())


def test_node_schema():
    original = tools.dumps(tools.FlowGraphService.get_node_schemas(None, "ai"))
    result = asyncio.run(
        tools.execute_tool(
            "list_node_types",
            '{"node_type":"ai"}',
            ADMIN,
            conversation_id=1,
            call_id="test",
        )
    )

    assert result["ok"] is True
    assert "ai" in json.dumps(result["data"])
    assert tools.dumps(tools.FlowGraphService.get_node_schemas(None, "ai")) == original


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
                conversation_id=1,
                call_id="test",
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
    result = asyncio.run(
        tools.execute_tool(
            "list_node_types", "{}", ADMIN, conversation_id=1, call_id="test"
        )
    )

    assert result["error"]["code"] == ErrorCode.ASSISTANT_RESULT_LIMIT


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_result_byte_boundary(monkeypatch, extra_bytes):
    payload = {"label": "工作流", "values": (1, 2)}
    monkeypatch.setattr(tools, "workflow_list", AsyncMock(return_value=payload))
    monkeypatch.setattr(
        tools, "MAX_TOOL_RESULT_BYTES", len(tools.dumps(payload)) - extra_bytes
    )

    result = asyncio.run(
        tools.execute_tool(
            "list_workflows", "{}", ADMIN, conversation_id=1, call_id="test"
        )
    )

    if extra_bytes:
        assert result["error"]["code"] == ErrorCode.ASSISTANT_RESULT_LIMIT
    else:
        assert result == {"ok": True, "data": {"label": "工作流", "values": [1, 2]}}


def test_timeout(monkeypatch):
    monkeypatch.setattr(
        tools.FlowGraphService, "get_accessible", AsyncMock(side_effect=TimeoutError)
    )
    result = asyncio.run(
        tools.execute_tool(
            "get_workflow",
            '{"workflow_id":1}',
            ADMIN,
            conversation_id=1,
            call_id="test",
        )
    )

    assert result["error"]["code"] == ErrorCode.AI_TIMEOUT


def test_error_redaction(monkeypatch):
    monkeypatch.setattr(
        tools.FlowGraphService,
        "get_accessible",
        AsyncMock(side_effect=RuntimeError("private-password")),
    )
    result = asyncio.run(
        tools.execute_tool(
            "get_workflow",
            '{"workflow_id":1}',
            ADMIN,
            conversation_id=1,
            call_id="test",
        )
    )

    assert result["error"]["code"] == ErrorCode.INTERNAL_SERVER_ERROR
    assert "private-password" not in json.dumps(result)


def test_create_draft():
    async def run():
        async with database():
            await record_call("create")

            result = await create()

            assert result["ok"] is True
            data = result["data"]
            graph = await FlowGraph.get(id=data["workflow_id"])
            assert (graph.name, graph.category, graph.description) == (
                "Nightly scan",
                GraphCategory.SCHEDULE,
                "Scan daily",
            )
            assert graph.state == GraphState.DRAFT
            assert graph.draft == {"nodes": [], "edges": []}
            assert graph.definition is None
            assert graph.draft_version == data["draft_version"] == 0
            assert data["editor_path"] == f"/settings/workflows/graphs/{graph.id}"
            assert data["published"] is data["executed"] is data["replayed"] is False
            assert await FlowJob.all().count() == await FlowLog.all().count() == 0
            # the workflow and its successful result are recorded together
            call = await AssistantToolCall.get(call_id="create")
            assert call.status == "complete"
            assert call.result == {
                "id": "create",
                "name": "create_workflow_draft",
                "ok": True,
                "data": data,
            }

    asyncio.run(run())


def test_create_replay():
    async def run():
        async with database():
            first = await record_call("first")
            created = await create(call_id="first")
            await AssistantToolCall.create(
                turn_id=first.id,
                call_id="repeat",
                name="create_workflow_draft",
                arguments="{}",
            )
            retry = await record_call("retry", retry_of=first)
            await record_call("chained", retry_of=retry)

            repeated = await create({**CREATE, "name": "Other"}, call_id="repeat")
            retried = await create(call_id="retry")
            chained = await create({**CREATE, "name": "Third"}, call_id="chained")

            for result in (repeated, retried, chained):
                assert result["ok"] is True
                assert result["data"] == {**created["data"], "replayed": True}
            assert await FlowGraph.all().count() == 1

    asyncio.run(run())


def test_create_independent():
    async def run():
        async with database():
            await record_call("first")
            await record_call("second")

            first = await create(call_id="first")
            second = await create({**CREATE, "name": "Second scan"}, call_id="second")

            assert first["data"]["workflow_id"] != second["data"]["workflow_id"]
            assert second["data"]["replayed"] is False
            assert await FlowGraph.all().count() == 2

    asyncio.run(run())


def test_create_existing_name():
    async def run():
        async with database():
            existing = await FlowGraph.create(
                name="Nightly scan",
                category=GraphCategory.INDEXER,
                state=GraphState.PUBLISHED,
                draft={"nodes": [], "edges": []},
            )
            await record_call("create")

            result = await create()

            assert result["error"]["code"] == ErrorCode.NAME_ALREADY_EXISTS
            assert "Nothing was created" in result["error"]["guidance"]
            assert "workflow_id" not in json.dumps(result)
            assert await FlowGraph.all().count() == 1
            await existing.refresh_from_db()
            assert existing.category == GraphCategory.INDEXER
            call = await AssistantToolCall.get(call_id="create")
            assert (call.status, call.result) == ("running", None)

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments",
    [
        {"category": "schedule", "description": "Scan daily"},
        {"name": "Scan", "description": "Scan daily"},
        {"name": "Scan", "category": "schedule"},
        {**CREATE, "name": "   "},
        {**CREATE, "description": ""},
        {**CREATE, "name": "x" * 65},
        {**CREATE, "category": "download"},
        {**CREATE, "document": {"nodes": [], "edges": []}},
    ],
)
def test_create_invalid(arguments):
    async def run():
        async with database():
            await record_call("create")

            result = await create(arguments)

            assert result["error"]["code"] == ErrorCode.BAD_REQUEST
            assert await FlowGraph.all().count() == 0

    asyncio.run(run())


def test_create_unrecorded():
    async def run():
        async with database():
            result = await create(call_id="missing")

            assert result["error"]["code"] == ErrorCode.NOT_FOUND
            assert await FlowGraph.all().count() == 0

    asyncio.run(run())


def test_create_rollback(monkeypatch):
    original = tools.FlowGraphService.create_draft

    async def interrupted(*args):
        await original(*args)
        raise RuntimeError("interrupted after insert")

    monkeypatch.setattr(tools.FlowGraphService, "create_draft", interrupted)

    async def run():
        async with database():
            await record_call("create")

            result = await create()

            assert result["error"]["code"] == ErrorCode.INTERNAL_SERVER_ERROR
            assert await FlowGraph.all().count() == 0
            assert (await AssistantToolCall.get(call_id="create")).result is None

    asyncio.run(run())
