"""Unit tests for workflow authorization, visibility, and versioned writes."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sanic import Request, Sanic
from sanic.response.types import BaseHTTPResponse
from sanic_ext.exceptions import ValidationError
from tortoise import Tortoise

from app.core.exceptions import (
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
    UnauthorizedException,
)
from app.models.flow import FlowGraph, FlowJob, FlowLog, GraphCategory, GraphState
from app.models.user import PermType, User, UserPermission, UserRole
from app.routes import flow as routes
from app.utils.json import dumps


@pytest.fixture(autouse=True)
def application():
    original_dumps = BaseHTTPResponse._dumps
    app = Sanic("flow-route-test", configure_logging=False, dumps=dumps)
    try:
        yield app
    finally:
        Sanic.unregister_app(app)
        BaseHTTPResponse._dumps = original_dumps


@asynccontextmanager
async def database():
    """Open an isolated database for workflow permission checks.

    Yields:
        Control while the temporary database is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        yield
    finally:
        await Tortoise.close_connections()


def request(user=None, path="/_api/flow/graph/list", body=None):
    """Create a request carrying a session without starting background services.

    Args:
        user: Authenticated account; `None` simulates a missing session.
        path: API path and optional query string.
        body: Optional JSON payload; when present, send a POST request.

    Returns:
        Request suitable for the existing authorization and validation decorators.
    """
    result = Request(
        url_bytes=path.encode(),
        headers={"content-type": "application/json"} if body is not None else {},
        version="1.1",
        method="POST" if body is not None else "GET",
        transport=None,
        app=Sanic.get_app("flow-route-test"),
    )
    if body is not None:
        result.body = json.dumps(body).encode()
    if user is not None:
        result.ctx.user = SimpleNamespace(id=user.id, role=user.role, perms=None)
    return result


@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (routes.list_graphs, {}),
        (routes.get_graph_details, {"id": 1}),
        (routes.get_graph_logs, {"id": 1}),
        (routes.get_node_schemas, {}),
        (routes.list_jobs, {}),
    ],
)
def test_missing_session(handler, arguments):
    with pytest.raises(UnauthorizedException):
        asyncio.run(handler(request(), **arguments))


def test_visibility():
    async def run():
        async with database():
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            allowed = await FlowGraph.create(
                name="Allowed",
                category=GraphCategory.INDEXER,
                state=GraphState.DRAFT,
                draft={"nodes": [], "edges": []},
            )
            hidden = await FlowGraph.create(
                name="Hidden",
                category=GraphCategory.SCHEDULE,
                state=GraphState.DRAFT,
            )
            permission = await UserPermission.create(
                user=user,
                rel_type=PermType.INDEXER,
                rel_id=allowed.id,
            )
            for graph in (allowed, hidden):
                await FlowJob.create(
                    graph=graph, state="pending", trigger="cron", cron_expr="0 8 * * *"
                )

            listed = json.loads((await routes.list_graphs(request(user))).body)
            details = await routes.get_graph_details(request(user), id=allowed.id)
            logs = await routes.get_graph_logs(request(user), id=allowed.id)
            schemas = await routes.get_node_schemas(request(user))
            jobs = json.loads((await routes.list_jobs(request(user))).body)

            assert listed["total"] == 1
            assert [item["id"] for item in listed["items"]] == [allowed.id]
            assert json.loads(details.body)["draft"] == {"nodes": [], "edges": []}
            assert json.loads(logs.body) == []
            assert jobs["total"] == 1
            assert jobs["items"][0]["graph_id"] == allowed.id
            assert any(
                n["node_type"] == "ai" for n in json.loads(schemas.body)["general"]
            )
            for handler in (routes.get_graph_details, routes.get_graph_logs):
                with pytest.raises(ForbiddenException) as exc_info:
                    await handler(request(user), id=hidden.id)
                assert exc_info.value.status_code == 403
                assert exc_info.value.message == ErrorCode.PERMISSION_DENIED

            await permission.delete()
            assert (
                json.loads((await routes.list_jobs(request(user))).body)["total"] == 0
            )
            for handler in (routes.get_graph_details, routes.get_graph_logs):
                with pytest.raises(ForbiddenException) as exc_info:
                    await handler(request(user), id=allowed.id)
                assert exc_info.value.status_code == 403
                assert exc_info.value.message == ErrorCode.PERMISSION_DENIED

            assert await FlowGraph.all().count() == 2
            assert await FlowLog.all().count() == 0
            assert (await FlowGraph.get(id=allowed.id)).draft == {
                "nodes": [],
                "edges": [],
            }

    asyncio.run(run())


@pytest.mark.parametrize("handler", [routes.save_graph, routes.publish_graph])
def test_version_required(handler):
    user = SimpleNamespace(id=1, role=UserRole.ADMIN)
    with pytest.raises(ValidationError):
        asyncio.run(handler(request(user, body={"nodes": [], "edges": []}), id=1))


def test_versioned_write():
    async def run():
        async with database():
            user = await User.create(
                username="admin", password="unused", role=UserRole.ADMIN
            )
            graph = await FlowGraph.create(
                name="Versioned",
                category=GraphCategory.SCHEDULE,
                state=GraphState.DRAFT,
            )
            body = {
                "nodes": [{"id": "http", "data": {"$schema": "http"}}],
                "edges": [],
                "draft_version": 0,
            }
            saved = json.loads(
                (await routes.save_graph(request(user, body=body), id=graph.id)).body
            )
            assert saved["draft_version"] == 1

            for handler in (routes.save_graph, routes.publish_graph):
                with pytest.raises(KaloscopeException) as error:
                    await handler(request(user, body=body), id=graph.id)
                assert error.value.status_code == 409
                assert error.value.message == ErrorCode.FLOW_DRAFT_CONFLICT

            validated = await routes.validate_graph(
                request(user, body={"nodes": body["nodes"], "edges": []}), id=graph.id
            )
            assert not json.loads(validated.body)["valid"]
            latest = await FlowGraph.get(id=graph.id)
            assert latest.draft_version == 1
            assert latest.definition is None

    asyncio.run(run())


def test_admin():
    async def run():
        async with database():
            user = await User.create(
                username="admin", password="unused", role=UserRole.ADMIN
            )
            graph = await FlowGraph.create(
                name="Scheduled",
                category=GraphCategory.SCHEDULE,
                state=GraphState.DRAFT,
            )

            response = await routes.get_graph_details(request(user), id=graph.id)
            assert json.loads(response.body)["id"] == graph.id

            for handler in (routes.get_graph_details, routes.get_graph_logs):
                with pytest.raises(NotFoundException) as exc_info:
                    await handler(request(user), id=9999)
                assert exc_info.value.status_code == 404
                assert exc_info.value.message == ErrorCode.NOT_FOUND

    asyncio.run(run())


@pytest.mark.parametrize("granted", [False, True])
def test_missing(granted):
    async def run():
        async with database():
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            if granted:
                await UserPermission.create(
                    user=user, rel_type=PermType.INDEXER, rel_id=9999
                )

            for handler in (routes.get_graph_details, routes.get_graph_logs):
                with pytest.raises(NotFoundException) as exc_info:
                    await handler(request(user), id=9999)
                assert exc_info.value.status_code == 404
                assert exc_info.value.message == ErrorCode.NOT_FOUND

    asyncio.run(run())
