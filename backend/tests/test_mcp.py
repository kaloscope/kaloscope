"""Unit tests for scoped HTTP MCP and workflow permissions."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from jsonschema import Draft202012Validator
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent, ToolAnnotations
from sanic import HTTPResponse, Request, Sanic
from sanic.compat import Header
from sanic.models.protocol_types import TransportProtocol
from sanic.response.types import BaseHTTPResponse
from tortoise import Tortoise, timezone

from app.core.config import KaloscopeConfig
from app.core.constants import MCP_PATH, SESSION_ID
from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.core.flow.engine import FlowEngine
from app.core.flow.nodes.base import Node
from app.core.flow.reference import get_workflow_reference
from app.core.middleware import on_request, on_response
from app.mcp import close_mcp, create_server, start_mcp
from app.models.ai import AIProvider
from app.models.download import (
    Downloader,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)
from app.models.flow import (
    FlowGraph,
    FlowJob,
    FlowLog,
    FlowRepository,
    FlowTemplate,
    FlowTrigger,
    GraphCategory,
    GraphQuery,
    GraphState,
    GraphWrite,
    JobQuery,
)
from app.models.mcp import (
    MCPAccessToken,
    MCPAccessTokenCreate,
    MCPTokenScope,
    WorkflowLogs,
)
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib, MediaQuery
from app.models.user import PermType, User, UserInfo, UserPermission, UserRole
from app.models.workflow_authoring import TemplateValidation
from app.routes.mcp import dispatch
from app.services import workflow_authoring as authoring
from app.services.ai import AIProviderService
from app.services.flow import FlowGraphService, FlowJobService, FlowLogService
from app.services.mcp import MCPAccessTokenService
from app.services.media import MediaItemService, MediaLibService
from app.utils.json import dumps, loads


@asynccontextmanager
async def application():
    """Open an isolated application with the real MCP transport and database.

    Yields:
        Application containing reader and administrator sessions and MCP tokens.
    """
    original_dumps = BaseHTTPResponse._dumps
    app = Sanic("mcp-test", configure_logging=False, dumps=dumps)
    app.config.update(AUTH_EXCLUDE_PATHS=[], TOKEN_EXPIRATION_HOURS=120)
    app.shared_ctx.sessions = {}
    app.ctx.mcp_tokens = {}
    app.ctx.lib_watcher = SimpleNamespace(is_scanning=lambda _: False)
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        for name, role in (("reader", UserRole.USER), ("admin", UserRole.ADMIN)):
            user = await User.create(username=name, password="unused", role=role)
            now = timezone.now()
            app.shared_ctx.sessions[name] = UserInfo(
                id=user.id,
                username=name,
                role=role,
                login_id=name,
                avatar=None,
                preferences={},
                client_ip="127.0.0.1",
                login_at=now,
                last_activity=now,
                expire_at=now + timedelta(hours=1),
            )
            issued = await MCPAccessTokenService.create(
                user.id, MCPAccessTokenCreate(name=name)
            )
            app.ctx.mcp_tokens[name] = issued["token"]
        await start_mcp(app)
        yield app
    finally:
        await close_mcp(app)
        await Tortoise.close_connections()
        Sanic.unregister_app(app)
        BaseHTTPResponse._dumps = original_dumps


async def post(
    app,
    method,
    params=None,
    *,
    token: str | None = "reader",
    headers=None,
    http_method="POST",
) -> HTTPResponse:
    """Dispatch JSON-RPC through the real token middleware and MCP route.

    Args:
        app: Isolated application.
        method: JSON-RPC method.
        params: JSON-RPC parameters; `None` sends an empty object.
        token: Token fixture name or raw credential; `None` omits authentication.
        headers: Optional header overrides.
        http_method: HTTP method used for the request.

    Returns:
        Sanic response after the existing response middleware.
    """
    request_headers = {
        "host": "localhost:8000",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-11-25",
    }
    if token is not None:
        request_headers["authorization"] = (
            f"Bearer {app.ctx.mcp_tokens.get(token, token)}"
        )
    request_headers.update(headers or {})
    request = Request(
        url_bytes=MCP_PATH.encode(),
        headers=Header(request_headers),
        version="1.1",
        method=http_method,
        transport=cast(TransportProtocol, None),
        app=app,
    )
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not method.startswith("notifications/"):
        message["id"] = 1
    request.body = dumps(message)
    await on_request(request)
    handler = cast(Callable[[Request], Awaitable[HTTPResponse]], dispatch)
    response = await handler(request)
    wrapped = await on_response(request, response)
    assert wrapped is not None
    return wrapped


def response_json(response: HTTPResponse):
    """Decode a response after verifying that it contains a body.

    Args:
        response: Response produced by the MCP test transport.

    Returns:
        Decoded JSON payload.
    """
    assert response.body is not None
    return loads(response.body)


async def call(app, name, arguments=None, *, token="reader"):
    """Call a tool and validate its public schema and both result representations.

    Args:
        app: Isolated application.
        name: Tool name.
        arguments: Tool arguments; `None` supplies an empty object.
        token: Account token fixture to use.

    Returns:
        Tool result, including structured data or a safe error message.
    """
    response = await post(
        app, "tools/call", {"name": name, "arguments": arguments or {}}, token=token
    )
    assert response.status == 200
    payload = response_json(response)
    assert "request_id" not in payload and "data" not in payload
    result = payload["result"]
    if not result.get("isError"):
        assert len(result["content"]) == 1
        assert loads(result["content"][0]["text"]) == result["structuredContent"]
        if not hasattr(app.ctx, "output_schemas"):
            listed = response_json(await post(app, "tools/list", token=token))
            app.ctx.output_schemas = {
                tool["name"]: tool["outputSchema"] for tool in listed["result"]["tools"]
            }
        Draft202012Validator(app.ctx.output_schemas[name]).validate(
            result["structuredContent"]
        )
    return result


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_protocol(mode):
    async def run():
        async with Client(create_server(), mode=mode) as client:
            result = await client.list_tools()
            assert {tool.name for tool in result.tools} == {
                "list_workflows",
                "get_workflow",
                "list_node_types",
                "get_workflow_reference",
                "probe_http_request",
                "validate_workflow_template",
                "get_workflow_logs",
                "list_media_libraries",
                "list_media_items",
                "get_media_item",
                "list_download_tasks",
                "list_workflow_jobs",
                "list_ai_providers",
                "validate_workflow",
                "preview_workflow_draft",
                "create_workflow_draft",
                "get_download_plan_options",
                "validate_download_plan",
                "create_download_plan",
                "get_download_plan",
                "list_download_plans",
                "get_library_ingestion",
                "validate_download_task",
                "create_download_task",
                "search_indexer",
                "save_workflow_draft",
                "list_workflow_drafts",
                "get_workflow_draft",
                "restore_workflow_draft",
                "run_workflow",
                "review_workflow_schedule",
                "apply_workflow_schedule",
                "review_download_plan_change",
                "apply_download_plan_change",
                "review_download_task_action",
                "apply_download_task_action",
            }
            for tool in result.tools:
                assert tool.annotations is not None
                assert tool.output_schema is not None
                writes = tool.name in {
                    "create_workflow_draft",
                    "save_workflow_draft",
                    "restore_workflow_draft",
                    "probe_http_request",
                    "create_download_plan",
                    "create_download_task",
                    "search_indexer",
                    "run_workflow",
                    "apply_workflow_schedule",
                    "apply_download_plan_change",
                    "apply_download_task_action",
                }
                assert tool.annotations.read_only_hint is (not writes)
                assert tool.annotations.destructive_hint is (
                    tool.name
                    in {
                        "save_workflow_draft",
                        "restore_workflow_draft",
                        "probe_http_request",
                        "create_download_plan",
                        "create_download_task",
                        "search_indexer",
                        "run_workflow",
                        "apply_workflow_schedule",
                        "apply_download_plan_change",
                        "apply_download_task_action",
                    }
                )
                # repeating a request ID reads the same plan, task, run or change
                assert tool.annotations.idempotent_hint is (
                    not writes
                    or tool.name
                    in {
                        "create_download_plan",
                        "create_download_task",
                        "run_workflow",
                        "apply_workflow_schedule",
                        "apply_download_plan_change",
                        "apply_download_task_action",
                    }
                )
                if tool.name in {
                    "validate_download_task",
                    "apply_download_task_action",
                }:
                    assert tool.annotations.open_world_hint is True
                assert (
                    not {"ctx", "token", "url"} & tool.input_schema["properties"].keys()
                )
                for parameter in tool.input_schema["properties"].values():
                    assert parameter.get("description")
                for schema in (tool.input_schema, tool.output_schema):
                    for definition in schema.get("$defs", {}).values():
                        if "enum" in definition:
                            assert definition.get("description")
                schema = tool.output_schema
                Draft202012Validator.check_schema(schema)
                assert schema["type"] == "object"
                assert "result" not in schema.get("properties", {})
                if tool.name == "list_node_types":
                    assert schema["additionalProperties"]["type"] == "array"
                    assert "node_type" in schema["$defs"]["NodeResult"]["required"]
                else:
                    assert schema["properties"]
                if tool.name == "list_ai_providers":
                    fields = schema["$defs"]["ProviderResult"]["properties"]
                    assert "api_key" not in fields
                    assert fields["has_api_key"]["type"] == "boolean"
                    assert schema["$defs"]["AIProtocol"]["enum"] == ["chat_completions"]
            result = await client.call_tool("list_workflows", {})
            assert result.is_error
            content = result.content[0]
            assert isinstance(content, TextContent)
            assert "authenticated" in content.text
            assert "valid Kaloscope MCP access token" in content.text

    asyncio.run(run())


def test_http_protocol():
    async def run():
        async with application() as app:
            response = await post(
                app,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0"},
                },
            )
            assert response.status == 200
            data = response_json(response)
            assert data["jsonrpc"] == "2.0"
            assert data["result"]["serverInfo"]["name"] == "Kaloscope"
            instructions = data["result"]["instructions"]
            assert "HTTP 401" in instructions
            assert "correct the reported fields" in instructions
            assert "permissions change" in instructions
            assert "not a failed tool call" in instructions
            assert "mcp-session-id" not in response.headers
            assert "set-cookie" not in response.headers
            response = await post(app, "notifications/initialized")
            assert response.status == 202
            response = await post(app, "tools/list")
            assert len(response_json(response)["result"]["tools"]) == 36
            response = await post(
                app, "ping", headers={"origin": "http://localhost:8000"}
            )
            assert response.status == 200
            for method in ("GET", "DELETE"):
                response = await post(app, "ping", http_method=method)
                assert response.status == 405
                assert response.headers["allow"] == "POST"

    asyncio.run(run())


@pytest.mark.parametrize("token", [None, "missing", "expired"])
def test_authentication(token):
    async def run():
        async with application() as app:
            if token == "expired":
                await MCPAccessToken.filter(name="reader").update(
                    expires_at=timezone.now() - timedelta(seconds=1)
                )
                app.ctx.mcp_tokens[token] = app.ctx.mcp_tokens["reader"]
            with pytest.raises(UnauthorizedException):
                await post(app, "tools/list", token=token)
            with pytest.raises(UnauthorizedException):
                await post(
                    app,
                    "tools/list",
                    token=None,
                    headers={"cookie": f"{SESSION_ID}=reader"},
                )
            with pytest.raises(UnauthorizedException):
                await post(
                    app,
                    "tools/list",
                    token=None,
                    headers={"cookie": f"{SESSION_ID}={app.ctx.mcp_tokens['reader']}"},
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "annotations",
    [None, ToolAnnotations(read_only_hint=False), ToolAnnotations(read_only_hint=True)],
)
def test_write_tool_denied(annotations):
    server = create_server()
    operation = AsyncMock()

    @server.tool(annotations=annotations)
    async def future_write() -> str:
        await operation()
        return "done"

    with pytest.raises(ToolError, match="no registered access grant"):
        asyncio.run(server.call_tool("future_write", {}))
    operation.assert_not_awaited()


@pytest.mark.parametrize(
    "name", ["create_workflow_draft", "save_workflow_draft", "restore_workflow_draft"]
)
def test_readonly_grant(name):
    async def run():
        async with application() as app:
            for token in ("reader", "admin"):
                result = await call(app, name, {}, token=token)
                assert result["isError"]
                assert "only permits read-only" in result["content"][0]["text"]
            assert not await FlowGraph.all().exists()

    asyncio.run(run())


def test_read_write_account():
    async def run():
        async with application() as app:
            identity = app.shared_ctx.sessions["reader"]
            token = await MCPAccessTokenService.create(
                identity.id,
                MCPAccessTokenCreate(name="writer", scope=MCPTokenScope.READ_WRITE),
            )
            result = await call(
                app,
                "create_workflow_draft",
                {
                    "workflow": {
                        "name": "Denied",
                        "category": "schedule",
                        "document": {"nodes": [], "edges": []},
                    }
                },
                token=token["token"],
            )

            assert result["isError"] and "not allowed" in result["content"][0]["text"]
            assert not await FlowGraph.all().exists()

    asyncio.run(run())


def test_draft_repair():
    async def run():
        async with application() as app:
            identity = app.shared_ctx.sessions["admin"]
            token = await MCPAccessTokenService.create(
                identity.id,
                MCPAccessTokenCreate(name="writer", scope=MCPTokenScope.READ_WRITE),
            )
            app.ctx.mcp_tokens["writer"] = token["token"]
            original = {
                "nodes": [
                    {
                        "id": "http",
                        "data": {"$schema": "http", "url": "https://old.test"},
                    }
                ],
                "edges": [],
            }
            created = await call(
                app,
                "create_workflow_draft",
                {
                    "workflow": {
                        "name": "Repair",
                        "category": "schedule",
                        "document": original,
                    }
                },
                token="writer",
            )
            metadata = created["structuredContent"]
            graph_id = metadata["workflow_id"]
            assert metadata["draft_version"] == 1
            assert metadata["editor_path"] == f"/settings/workflows/graphs/{graph_id}"
            assert (await FlowGraph.get(id=graph_id)).definition is None
            published = await FlowGraphService.publish_graph(
                graph_id, GraphWrite(**original, draft_version=1), identity
            )
            await FlowLog.create(
                graph_id=graph_id,
                started_at=timezone.now(),
                node_id="http",
                node_type="http",
                node_data={"$error": "http_request_failed"},
            )
            logs = await call(
                app, "get_workflow_logs", {"workflow_id": graph_id}, token="writer"
            )
            assert (
                logs["structuredContent"]["runs"][0][1]["error"]
                == "http_request_failed"
            )
            details = await call(
                app, "get_workflow", {"workflow_id": graph_id}, token="writer"
            )
            proposed = details["structuredContent"]["draft"]
            proposed["nodes"][0]["data"]["url"] = "https://fixed.test"
            validated = await call(
                app,
                "validate_workflow",
                {"workflow_id": graph_id, "document": proposed},
                token="admin",
            )
            assert validated["structuredContent"]["valid"]
            proposed["draft_version"] = details["structuredContent"]["draft_version"]
            preview = await call(
                app,
                "preview_workflow_draft",
                {"workflow_id": graph_id, "draft": proposed},
                token="writer",
            )
            assert preview["structuredContent"]["validation"]["valid"]
            assert (
                preview["structuredContent"]["changes"][0]["before"]["data"]["url"]
                == "https://old.test"
            )
            assert (await FlowGraph.get(id=graph_id)).draft_version == 2
            saved = await call(
                app,
                "save_workflow_draft",
                {"workflow_id": graph_id, "draft": proposed},
                token="writer",
            )
            assert saved["structuredContent"]["draft_version"] == 3
            graph = await FlowGraph.get(id=graph_id)
            assert graph.definition == published.definition
            assert graph.state == GraphState.MODIFIED
            assert not await FlowJob.all().exists()
            assert not await FlowTrigger.all().exists()
            stale = await call(
                app,
                "save_workflow_draft",
                {"workflow_id": graph_id, "draft": proposed},
                token="writer",
            )
            assert (
                stale["isError"]
                and "flow_draft_conflict" in stale["content"][0]["text"]
            )
            history = await call(
                app, "list_workflow_drafts", {"workflow_id": graph_id}, token="writer"
            )
            assert [
                item["version"] for item in history["structuredContent"]["items"]
            ] == [2, 1, 0]
            previous = await call(
                app,
                "get_workflow_draft",
                {"workflow_id": graph_id, "version": 2},
                token="writer",
            )
            assert (
                previous["structuredContent"]["nodes"][0]["data"]["url"]
                == "https://old.test"
            )
            restored = await call(
                app,
                "restore_workflow_draft",
                {"workflow_id": graph_id, "version": 2, "draft_version": 3},
                token="writer",
            )
            assert restored["structuredContent"]["draft_version"] == 4
            assert (await FlowGraph.get(id=graph_id)).definition == published.definition

            await User.filter(id=identity.id).update(role="user")
            proposed["draft_version"] = 4
            denied = await call(
                app,
                "save_workflow_draft",
                {"workflow_id": graph_id, "draft": proposed},
                token="writer",
            )
            assert denied["isError"] and "not allowed" in denied["content"][0]["text"]
            assert (await FlowGraph.get(id=graph_id)).draft_version == 4

    asyncio.run(run())


@pytest.mark.parametrize(
    "origin",
    [
        "https://foreign.test",
        "null",
        "http://localhost:8000.evil.test",
        "http://[invalid",
        "http://localhost:8000/path",
    ],
)
def test_foreign_origin(origin):
    async def run():
        async with application() as app:
            with pytest.raises(ForbiddenException):
                await post(app, "tools/list", headers={"origin": origin})

    asyncio.run(run())


def test_reads():
    async def run():
        async with application() as app:
            graph = await FlowGraph.create(
                name="Allowed",
                category=GraphCategory.INDEXER,
                state=GraphState.DRAFT,
                draft={
                    "nodes": [
                        {"id": "ai", "data": {"$schema": "ai", "prompt": "Summarize"}}
                    ],
                    "edges": [],
                },
            )
            hidden = await FlowGraph.create(
                name="Hidden", category=GraphCategory.SCHEDULE, state=GraphState.DRAFT
            )
            permission = await UserPermission.create(
                user_id=app.shared_ctx.sessions["reader"].id,
                rel_type=PermType.INDEXER,
                rel_id=graph.id,
            )
            await FlowLog.create(
                graph=graph,
                started_at=timezone.now(),
                node_id="ai",
                node_type="ai",
                node_data={"$error": "ai_timeout"},
            )
            before = await FlowGraph.all().values()

            result = await call(
                app,
                "list_workflows",
                {"name": "allow", "category": "indexer", "state": "draft"},
            )
            assert result["structuredContent"]["total"] == 1
            assert "draft" not in result["structuredContent"]["items"][0]
            details = await call(app, "get_workflow", {"workflow_id": graph.id})
            node = details["structuredContent"]["draft"]["nodes"][0]["data"]
            assert node["prompt"] == "Summarize"
            assert node["$schema"]["node_type"] == "ai"
            logs = await call(
                app, "get_workflow_logs", {"workflow_id": graph.id, "limit": 1}
            )
            assert not logs.get("isError")
            assert logs["structuredContent"]["runs"][0][1]["error"] == "ai_timeout"
            schemas = await call(
                app, "list_node_types", {"node_type": "ai", "category": "schedule"}
            )
            assert len(schemas["structuredContent"]["general"]) == 1
            assert schemas["structuredContent"]["general"][0]["node_type"] == "ai"

            for tool in ("get_workflow", "get_workflow_logs"):
                denied = await call(app, tool, {"workflow_id": hidden.id})
                assert (
                    denied["isError"] and "not allowed" in denied["content"][0]["text"]
                )
                assert "permissions change" in denied["content"][0]["text"]
                for token in ("reader", "admin"):
                    missing = await call(app, tool, {"workflow_id": 9999}, token=token)
                    assert (
                        missing["isError"]
                        and "not found" in missing["content"][0]["text"]
                    )
                    assert "accessible resource list" in missing["content"][0]["text"]
            await permission.delete()
            denied = await call(app, "get_workflow", {"workflow_id": graph.id})
            assert denied["isError"]
            assert (await call(app, "list_workflows"))["structuredContent"][
                "total"
            ] == 0
            assert await FlowGraph.all().values() == before
            assert await FlowLog.all().count() == 1

    asyncio.run(run())


def test_workflow_results():
    async def run():
        async with application() as app:
            user = app.shared_ctx.sessions["admin"]
            repository = await FlowRepository.create(
                repo_name="fixtures", repo_url="https://example.test/workflows"
            )
            definition = {
                "nodes": [
                    {
                        "id": "ai",
                        "data": {
                            "$schema": "ai",
                            "prompt": "Summarize",
                            "extension": {"null": None, "values": [1, True, "text"]},
                        },
                    }
                ],
                "edges": [],
            }
            template = await FlowTemplate.create(
                repo=repository,
                path="example.json",
                name="Template",
                category=GraphCategory.SCHEDULE,
                revision=1,
                definition=definition,
                newest=False,
            )
            await FlowTemplate.create(
                repo=repository,
                path=template.path,
                name=template.name,
                category=template.category,
                revision=2,
                definition=definition,
            )
            graph = await FlowGraph.create(
                tmpl=template,
                name="Workflow",
                category=GraphCategory.SCHEDULE,
                state=GraphState.PUBLISHED,
                draft=definition,
                definition=definition,
            )
            await FlowGraph.create(name="Draft", category="indexer", state="draft")
            started = timezone.now()
            await FlowLog.create(
                graph=graph,
                started_at=started,
                bootparams={"query": [1, False, None]},
                node_type="ai",
                node_data={"$error": "ai_timeout", "nested": {"value": 0.5}},
                exc_info="Connection timed out",
            )
            await FlowLog.create(
                graph=graph,
                started_at=started - timedelta(minutes=1),
                ended_at=started,
                retval=[{"result": True}, None],
            )
            await FlowJob.create(
                graph=graph,
                state="pending",
                trigger="interval",
                interval_num=5,
                interval_unit="minutes",
                bootparams={"values": [None, False, 1.5]},
            )
            cases = [
                (
                    "list_workflows",
                    {},
                    await FlowGraphService.list_graphs(GraphQuery(), user),
                ),
                (
                    "get_workflow",
                    {"workflow_id": graph.id},
                    await FlowGraphService.get_details(graph.id, user),
                ),
                (
                    "get_workflow_logs",
                    {"workflow_id": graph.id},
                    {"runs": await FlowLogService.get_accessible_logs(graph.id, user)},
                ),
                ("list_node_types", {}, FlowGraphService.get_node_schemas()),
                ("list_node_types", {"node_type": "missing"}, {}),
                (
                    "list_workflow_jobs",
                    {},
                    await FlowJobService.list_jobs(JobQuery(), user),
                ),
            ]

            for name, arguments, expected in cases:
                result = await call(app, name, arguments, token="admin")
                assert not result.get("isError"), result
                assert result["structuredContent"] == loads(dumps(expected))

    asyncio.run(run())


def test_optional_results():
    payload = {
        "runs": [
            [
                {
                    "at": "2026-10-18T08:00:00+00:00",
                    "type": "start",
                    "data": None,
                    "document": None,
                },
                {
                    "at": None,
                    "type": "ai",
                    "data": {"nested": [None, 1, False, 0.5]},
                    "document": "error",
                    "error": None,
                    "extension": {"value": "kept"},
                },
            ]
        ],
    }

    result = WorkflowLogs.model_validate(payload).model_dump(mode="json")

    assert result == payload
    assert "error" not in result["runs"][0][0]
    assert result["runs"][0][1]["error"] is None


def test_invalid_result(monkeypatch):
    monkeypatch.setattr(
        FlowGraphService,
        "list_graphs",
        AsyncMock(
            return_value={"total": 1, "items": [{"name": "private-result-value"}]}
        ),
    )

    async def run():
        async with application() as app:
            result = await call(app, "list_workflows")

            assert result["isError"]
            assert "private-result-value" not in str(result)
            assert "structuredContent" not in result

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", list(LibType))
def test_media_results(tmp_path, lib_type):
    path = tmp_path / "work"
    path.mkdir()
    nfo = path / "movie.nfo"
    nfo.write_text("<movie><title>Current title</title><rating>8.25</rating></movie>")

    async def run():
        async with application() as app:
            user = app.shared_ctx.sessions["admin"]
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            graph = await FlowGraph.create(
                name="Ingest", category="ingest", state="published"
            )
            await FlowTrigger.create(
                category="ingest", rel_id=lib.id, graph=graph, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                name="Stored title",
                path=str(path),
                dir=str(tmp_path),
                nfo_path=str(nfo),
                rating="7.50",
                format="dir" if lib_type in (LibType.NOVEL, LibType.COMIC) else None,
                original={"nested": [None, True, 42]},
            )
            await MediaItem.create(
                lib=lib,
                parent=item,
                name="Child",
                dir=str(path),
                path=str(path / "child"),
            )
            cases = [
                (
                    "list_media_libraries",
                    {},
                    {
                        "items": await MediaLibService.list_libraries(
                            user, app.ctx.lib_watcher
                        )
                    },
                ),
                (
                    "list_media_items",
                    {},
                    await MediaItemService.list_items(MediaQuery(), user),
                ),
                (
                    "get_media_item",
                    {"item_id": item.id},
                    await MediaItemService.get_details(item.id, user),
                ),
            ]

            for name, arguments, expected in cases:
                result = await call(app, name, arguments, token="admin")
                assert not result.get("isError"), result
                assert result["structuredContent"] == loads(dumps(expected))
            listed = await call(app, "list_media_items", token="admin")
            assert isinstance(listed["structuredContent"]["items"][0]["rating"], str)

    asyncio.run(run())


def test_concurrent_accounts():
    async def run():
        async with application() as app:
            await FlowGraph.create(
                name="Private", category=GraphCategory.SCHEDULE, state=GraphState.DRAFT
            )
            results = await asyncio.gather(
                *(
                    call(app, "list_workflows", token=token)
                    for token in ["reader", "admin"] * 4
                )
            )
            assert [r["structuredContent"]["total"] for r in results] == [0, 1] * 4

    asyncio.run(run())


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_workflows", {"page_num": 0}),
        ("list_workflows", {"page_num": True}),
        ("list_workflows", {"page_size": 101}),
        ("list_workflows", {"page_size": True}),
        ("list_workflows", {"category": "unknown"}),
        ("get_workflow", {"workflow_id": 0}),
        ("get_workflow", {"workflow_id": True}),
        ("get_workflow", {"workflow_id": "../admin"}),
        ("get_workflow_logs", {"workflow_id": 1, "limit": 21}),
        ("list_media_items", {"library_id": True}),
        ("list_media_items", {"page_size": 101}),
        ("get_media_item", {"item_id": 0}),
        ("list_download_tasks", {"state": "unknown"}),
        ("list_download_tasks", {"page_num": 0}),
        ("list_workflow_jobs", {"trigger": "unknown"}),
        ("list_workflow_jobs", {"page_size": 101}),
        ("scan_library", {"library_id": 1}),
        ("add_download", {"url": "https://example.test/file"}),
        ("execute_workflow", {"workflow_id": 1}),
        ("publish_workflow", {"workflow_id": 1}),
        ("delete_workflow", {"workflow_id": 1}),
    ],
)
def test_invalid_calls(name, arguments):
    async def run():
        async with application() as app:
            result = await call(app, name, arguments)
            assert result["isError"]
            assert await FlowGraph.all().count() == 0

    asyncio.run(run())


def test_internal_error(monkeypatch):
    monkeypatch.setattr(
        FlowGraphService,
        "list_graphs",
        AsyncMock(side_effect=RuntimeError("private database details")),
    )

    async def run():
        async with application() as app:
            result = await call(app, "list_workflows")
            assert result["isError"]
            assert "private database details" not in str(result)

    asyncio.run(run())


def test_media_reads(tmp_path):
    nfo = tmp_path / "movie.nfo"
    nfo.write_text("<movie><title>MCP Movie</title><year>2026</year></movie>")

    async def run():
        async with application() as app:
            library = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            private = await MediaLib.create(
                name="Private",
                dir=str(tmp_path / "private"),
                lib_type=LibType.MOVIE,
                priority=2,
            )
            item = await MediaItem.create(
                lib=library,
                name="movie.mp4",
                title="MCP Movie",
                dir=str(tmp_path),
                path=str(tmp_path / "movie.mp4"),
                nfo_path=str(nfo),
            )
            hidden = await MediaItem.create(
                lib=private,
                name="Private.mp4",
                dir=private.dir,
                path=f"{private.dir}/Private.mp4",
            )
            permission = await UserPermission.create(
                user_id=app.shared_ctx.sessions["reader"].id,
                rel_type=PermType.MEDIA_LIB,
                rel_id=library.id,
            )
            await MediaEvent.create(
                lib=library,
                src_path=library.dir,
                event_type="reconcile",
                payload={"state": "pending"},
            )
            before = await MediaItem.all().values()
            events_before = await MediaEvent.all().values()

            libraries = (await call(app, "list_media_libraries"))["structuredContent"][
                "items"
            ]
            assert [lib["id"] for lib in libraries] == [library.id]
            assert libraries[0]["scanning"] is True
            listed = await call(
                app,
                "list_media_items",
                {"keyword": "mcp", "library_id": library.id, "page_size": 1},
            )
            assert [row["id"] for row in listed["structuredContent"]["items"]] == [
                item.id
            ]
            empty_page = await call(
                app, "list_media_items", {"page_num": 2, "page_size": 1}
            )
            assert empty_page["structuredContent"] == {"total": 1, "items": []}
            excluded = await call(app, "list_media_items", {"library_id": private.id})
            assert excluded["structuredContent"]["total"] == 0
            details = (await call(app, "get_media_item", {"item_id": item.id}))[
                "structuredContent"
            ]
            assert details["metadata"]["title"] == "MCP Movie"
            assert details["metadata_state"] == "ready"
            assert (await call(app, "get_media_item", {"item_id": hidden.id}))[
                "isError"
            ]
            assert (await call(app, "get_media_item", {"item_id": 9999}))["isError"]
            admin = await call(app, "list_media_libraries", token="admin")
            assert len(admin["structuredContent"]["items"]) == 2
            assert await MediaItem.all().values() == before
            assert await MediaEvent.all().values() == events_before

            await permission.delete()
            assert (await call(app, "get_media_item", {"item_id": item.id}))["isError"]
            assert (await call(app, "list_media_items"))["structuredContent"][
                "total"
            ] == 0

    asyncio.run(run())
    assert nfo.read_text() == "<movie><title>MCP Movie</title><year>2026</year></movie>"


def test_application_scope(tmp_path):
    async def run():
        async with application() as app:
            other = Sanic("mcp-other-app", configure_logging=False, dumps=dumps)
            try:
                app.ctx.lib_watcher = SimpleNamespace(is_scanning=lambda _: True)
                await MediaLib.create(
                    name="Movies", dir=str(tmp_path), lib_type="movie", priority=1
                )
                result = await call(app, "list_media_libraries", token="admin")
                assert result["structuredContent"]["items"][0]["scanning"] is True
            finally:
                Sanic.unregister_app(other)

    asyncio.run(run())


@pytest.mark.parametrize("parent_state", ["hidden", "foreign"])
def test_inaccessible_media(tmp_path, parent_state):
    async def run():
        async with application() as app:
            lib = await MediaLib.create(
                name="Series", dir=str(tmp_path), lib_type="tv_show", priority=1
            )
            parent_lib = lib
            if parent_state == "foreign":
                parent_lib = await MediaLib.create(
                    name="Other",
                    dir=str(tmp_path / "other"),
                    lib_type="tv_show",
                    priority=2,
                )
            parent = await MediaItem.create(
                lib=parent_lib,
                dir=parent_lib.dir,
                path=f"{parent_lib.dir}/parent",
                name="Parent",
                visible=parent_state != "hidden",
            )
            child = await MediaItem.create(
                lib=lib,
                parent=parent,
                dir=parent.path,
                path=f"{parent.path}/child.mp4",
                name="Child",
            )
            await UserPermission.create(
                user_id=app.shared_ctx.sessions["reader"].id,
                rel_type=PermType.MEDIA_LIB,
                rel_id=lib.id,
            )
            for item in (parent, child):
                assert (await call(app, "get_media_item", {"item_id": item.id}))[
                    "isError"
                ]
                page = await call(app, "list_media_items", {"path": item.path})
                assert page["structuredContent"]["items"] == []

    asyncio.run(run())


def test_job_reads():
    async def run():
        async with application() as app:
            allowed = await FlowGraph.create(
                name="Allowed", category="indexer", state="published"
            )
            hidden = await FlowGraph.create(
                name="Hidden", category="schedule", state="published"
            )
            for graph in (allowed, hidden):
                await FlowJob.create(
                    graph=graph, state="pending", trigger="cron", cron_expr="0 8 * * *"
                )
            permission = await UserPermission.create(
                user_id=app.shared_ctx.sessions["reader"].id,
                rel_type=PermType.INDEXER,
                rel_id=allowed.id,
            )
            before = await FlowJob.all().values()

            page = await call(
                app,
                "list_workflow_jobs",
                {"name": "allow", "trigger": "cron", "state": "pending"},
            )
            assert page["structuredContent"]["total"] == 1
            assert page["structuredContent"]["items"][0]["graph_name"] == "Allowed"
            assert page["structuredContent"]["items"][0]["cron_expr"] == "0 8 * * *"
            admin = await call(app, "list_workflow_jobs", token="admin")
            assert admin["structuredContent"]["total"] == 2
            assert await FlowJob.all().values() == before
            await permission.delete()
            assert (await call(app, "list_workflow_jobs"))["structuredContent"][
                "total"
            ] == 0

    asyncio.run(run())


def test_provider_reads(monkeypatch):
    network = AsyncMock(
        side_effect=AssertionError("Read-only access must not call providers")
    )
    monkeypatch.setattr(httpx.AsyncClient, "send", network)

    async def run():
        async with application() as app:
            provider = await AIProvider.create(
                name="Offline",
                base_url="https://example.test/v1",
                api_key="encrypted-private-key",
                enabled=False,
                model_ids=["fixture-model"],
            )
            before = await AIProvider.all().values()
            result = await call(app, "list_ai_providers", token="admin")
            assert result["structuredContent"] == {
                "items": loads(
                    dumps(
                        await AIProviderService.list_providers(
                            app.shared_ctx.sessions["admin"]
                        )
                    )
                )
            }
            data = result["structuredContent"]["items"][0]
            assert data["id"] == provider.id and data["enabled"] is False
            assert (
                data["model_ids"] == ["fixture-model"] and data["has_api_key"] is True
            )
            assert "api_key" not in data and "encrypted-private-key" not in str(result)
            assert (await call(app, "list_ai_providers"))["isError"]
            assert await AIProvider.all().values() == before
            network.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("error_kind", [None, OfflineDownloadErrorKind.REMOTE_FAILED])
def test_download_reads(monkeypatch, error_kind):
    monkeypatch.setattr(
        KaloscopeConfig, "_config", SimpleNamespace(secret_key_enabled=False)
    )
    network = AsyncMock(
        side_effect=AssertionError("Read-only access must not poll downloaders")
    )
    monkeypatch.setattr(httpx.AsyncClient, "send", network)
    config = """driver: rpc
name: Test downloader
protocol: http
host: localhost
port: 9000
auth:
  password: private-downloader-password
methods:
  list:
    get: /tasks
"""

    async def run():
        async with application() as app:
            downloader = await Downloader.create(name="Test", config=config, priority=1)
            task = await DownloadTask.create(
                downloader=downloader,
                name="MCP Movie",
                dir="/private/downloads",
                state="error",
                error_msg="Connection unavailable",
                percentage=25,
                total_size=100,
                completed_size=25,
                magnet_link="https://example.test/?token=private-source-token",
                files=["private-file"],
            )
            if error_kind is not None:
                await OfflineDownloadJob.create(
                    download=task,
                    job_uuid="fixture",
                    source_fingerprint="fixture",
                    remote_dir="/remote/fixture",
                    last_error_kind=error_kind,
                )
            before = await DownloadTask.all().values()
            result = await call(
                app,
                "list_download_tasks",
                {"name": "mcp", "state": "error", "downloader_id": downloader.id},
            )
            data = result["structuredContent"]["items"][0]
            assert data["id"] == task.id and data["percentage"] == 25
            assert data["error_msg"] == "Connection unavailable"
            assert data["error_kind"] == error_kind
            assert not {"config", "magnet_link", "files", "dir"} & data.keys()
            assert "private-downloader-password" not in str(result)
            assert "private-source-token" not in str(result)
            empty_page = await call(
                app, "list_download_tasks", {"page_num": 2, "page_size": 1}
            )
            assert empty_page["structuredContent"] == {"total": 1, "items": []}
            assert await DownloadTask.all().values() == before
            network.assert_not_awaited()

    asyncio.run(run())


def test_node_knowledge(monkeypatch):
    execute = AsyncMock(side_effect=AssertionError("Read tools cannot execute"))
    complete = AsyncMock(side_effect=AssertionError("Read tools cannot call AI"))
    monkeypatch.setattr(FlowEngine, "execute", execute)
    monkeypatch.setattr(AIProviderService, "complete", complete)

    async def run():
        async with application() as app:
            counts = [
                await model.all().count() for model in (FlowGraph, FlowJob, FlowLog)
            ]
            for topic in (
                "context",
                "templates",
                "json_indexer",
                "html_indexer",
                "indexer_auth",
                "movie_scraper",
                "tv_scraper",
            ):
                result = await call(app, "get_workflow_reference", {"topic": topic})
                assert (
                    result["structuredContent"]
                    == get_workflow_reference(topic).model_dump()
                )
            invalid = await call(app, "get_workflow_reference", {"topic": "../config"})
            assert invalid["isError"]
            for schema in Node.schemas:
                node_type = schema.node_type
                result = await call(app, "list_node_types", {"node_type": node_type})
                expected = loads(
                    dumps(FlowGraphService.get_node_schemas(node_type=node_type))
                )
                assert result["structuredContent"] == expected
                node = next(iter(result["structuredContent"].values()))[0]
                assert node["description"]
                for example in node["fields"]:
                    if example.get("template"):
                        assert "// PURPOSE:" in example["default"]
                        assert example["readonly"]
            assert counts == [
                await model.all().count() for model in (FlowGraph, FlowJob, FlowLog)
            ]
            execute.assert_not_awaited()
            complete.assert_not_awaited()

    asyncio.run(run())


def test_authoring_tools(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [{"id": "one"}], "total": 3})

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    execute = AsyncMock(
        side_effect=AssertionError("Authoring tools cannot execute workflows")
    )
    complete = AsyncMock(side_effect=AssertionError("Authoring tools cannot call AI"))
    monkeypatch.setattr(FlowEngine, "execute", execute)
    monkeypatch.setattr(AIProviderService, "complete", complete)

    async def run():
        async with application() as app:
            counts = [
                await model.all().count()
                for model in (FlowGraph, FlowJob, FlowLog, DownloadTask)
            ]
            fixture = {
                "template": "{{ items|length }}",
                "context": {"items": [1, 2]},
                "mode": "expression",
            }
            result = await call(
                app, "validate_workflow_template", {"validation": fixture}
            )
            assert result["structuredContent"] == authoring.validate_workflow_template(
                TemplateValidation(**fixture)
            ).model_dump(mode="json")

            probe = {
                "probe": {
                    "request": {"url": "https://example.test/search"},
                    "formatter": '{"items":'
                    '{{ response.text|jsonpath_all("$.items[*]")|tojson }}}',
                }
            }
            for name in ("reader", "admin"):
                result = await call(app, "probe_http_request", probe, token=name)
                assert result["isError"]
                assert "read-only" in result["content"][0]["text"]
                identity = app.shared_ctx.sessions[name]
                issued = await MCPAccessTokenService.create(
                    identity.id,
                    MCPAccessTokenCreate(name="probe", scope=MCPTokenScope.READ_WRITE),
                )
                result = await call(
                    app, "probe_http_request", probe, token=issued["token"]
                )
                if name == "reader":
                    assert result["isError"]
                else:
                    assert result["structuredContent"]["formatted"]["value"] == {
                        "items": [{"id": "one"}]
                    }
                    assert result["structuredContent"]["body_complete"]
            assert len(requests) == 1
            assert counts == [
                await model.all().count()
                for model in (FlowGraph, FlowJob, FlowLog, DownloadTask)
            ]
            execute.assert_not_awaited()
            complete.assert_not_awaited()

    asyncio.run(run())
