"""Unit tests for workflow authorization, visibility, and versioned writes."""

import asyncio
import io
import json
import multiprocessing
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sanic import HTTPResponse, Request, Sanic
from sanic.compat import Header
from sanic.models.protocol_types import TransportProtocol
from sanic.response.types import BaseHTTPResponse
from sanic_ext.exceptions import ValidationError
from tortoise import Tortoise

from app.core.cookiejar import SQLiteCookieJar
from app.core.exceptions import (
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
    UnauthorizedException,
)
from app.core.flow.context import AUTH_KEY
from app.models.flow import (
    FlowGraph,
    FlowJob,
    FlowLog,
    FlowRepository,
    FlowTemplate,
    FlowVariable,
    GraphCategory,
    GraphState,
)
from app.models.general import GlobalCookie
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


async def call_route(
    handler: object, request: Request, **arguments: object
) -> HTTPResponse:
    """Call a decorated route using its runtime asynchronous handler contract.

    Args:
        handler: Route callable whose signature is obscured by Sanic decorators.
        request: Request passed through authorization and validation.
        **arguments: URL parameters supplied by the router.

    Returns:
        Response produced by the decorated route.
    """
    callback = cast(Callable[..., Awaitable[HTTPResponse]], handler)
    return await callback(request, **arguments)


def response_json(response: HTTPResponse):
    """Decode a route response after verifying that it contains a body.

    Args:
        response: Response returned by the decorated route.

    Returns:
        Decoded JSON payload.
    """
    assert response.body is not None
    return json.loads(response.body)


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
        headers=Header(
            {"content-type": "application/json"} if body is not None else {}
        ),
        version="1.1",
        method="POST" if body is not None else "GET",
        transport=cast(TransportProtocol, None),
        app=Sanic.get_app("flow-route-test"),
    )
    if body is not None:
        result.body = json.dumps(body).encode()
    if user is not None:
        result.ctx.user = SimpleNamespace(id=user.id, role=user.role, perms=None)
    return result


def upload(user, path, archive):
    """Create a multipart request carrying a workflow archive as `zip`.

    Args:
        user: Authenticated account.
        path: API path.
        archive: Zip archive contents.

    Returns:
        Request registered as current, so form models can read its files.
    """
    boundary = "flow-route-test"
    result = Request(
        url_bytes=path.encode(),
        headers=Header({"content-type": f"multipart/form-data; boundary={boundary}"}),
        version="1.1",
        method="POST",
        transport=cast(TransportProtocol, None),
        app=Sanic.get_app("flow-route-test"),
    )
    result.body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="zip"; filename="flows.zip"\r\n'
            "Content-Type: application/zip\r\n\r\n"
        ).encode()
        + archive
        + f"\r\n--{boundary}--\r\n".encode()
    )
    result.ctx.user = SimpleNamespace(id=user.id, role=user.role, perms=None)
    Request._current.set(result)
    return result


@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        (routes.list_graphs, {}),
        (routes.get_graph_details, {"id": 1}),
        (routes.get_graph_logs, {"id": 1}),
        (routes.get_node_schemas, {}),
        (routes.list_jobs, {}),
        (routes.execute_graph, {"id": 1}),
        (routes.get_indexer_config, {"id": 1}),
        (routes.get_indexer_auth, {"id": 1}),
        (routes.indexer_logout, {"id": 1}),
        (routes.pause_job, {"id": 1}),
        (routes.resume_job, {"id": 1}),
        (routes.reference_template, {"id": 1}),
        (routes.copy_template, {"id": 1}),
        (routes.export_graphs, {}),
        (routes.import_graphs, {}),
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

            listed = response_json(await call_route(routes.list_graphs, request(user)))
            details = await call_route(
                routes.get_graph_details, request(user), id=allowed.id
            )
            logs = await call_route(routes.get_graph_logs, request(user), id=allowed.id)
            schemas = await call_route(routes.get_node_schemas, request(user))
            jobs = response_json(await call_route(routes.list_jobs, request(user)))

            assert listed["total"] == 1
            assert [item["id"] for item in listed["items"]] == [allowed.id]
            assert response_json(details)["draft"] == {"nodes": [], "edges": []}
            assert response_json(logs) == []
            assert jobs["total"] == 1
            assert jobs["items"][0]["graph_id"] == allowed.id
            assert any(
                n["node_type"] == "ai" for n in response_json(schemas)["general"]
            )
            for handler in (routes.get_graph_details, routes.get_graph_logs):
                with pytest.raises(ForbiddenException) as exc_info:
                    await call_route(handler, request(user), id=hidden.id)
                assert exc_info.value.status_code == 403
                assert exc_info.value.message == ErrorCode.PERMISSION_DENIED

            await permission.delete()
            assert (
                response_json(await call_route(routes.list_jobs, request(user)))[
                    "total"
                ]
                == 0
            )
            for handler in (routes.get_graph_details, routes.get_graph_logs):
                with pytest.raises(ForbiddenException) as exc_info:
                    await call_route(handler, request(user), id=allowed.id)
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
            saved = response_json(
                await call_route(
                    routes.save_graph, request(user, body=body), id=graph.id
                )
            )
            assert saved["draft_version"] == 1

            for handler in (routes.save_graph, routes.publish_graph):
                with pytest.raises(KaloscopeException) as error:
                    await call_route(handler, request(user, body=body), id=graph.id)
                assert error.value.status_code == 409
                assert error.value.message == ErrorCode.FLOW_DRAFT_CONFLICT

            validated = await call_route(
                routes.validate_graph,
                request(user, body={"nodes": body["nodes"], "edges": []}),
                id=graph.id,
            )
            assert not response_json(validated)["valid"]
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

            response = await call_route(
                routes.get_graph_details, request(user), id=graph.id
            )
            assert response_json(response)["id"] == graph.id

            for handler in (routes.get_graph_details, routes.get_graph_logs):
                with pytest.raises(NotFoundException) as exc_info:
                    await call_route(handler, request(user), id=9999)
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
                    await call_route(handler, request(user), id=9999)
                assert exc_info.value.status_code == 404
                assert exc_info.value.message == ErrorCode.NOT_FOUND

    asyncio.run(run())


def test_indexer_login_access(application):
    application.ctx.cookies = SQLiteCookieJar(
        cast(
            Sanic,
            SimpleNamespace(
                shared_ctx=SimpleNamespace(
                    cookies={}, cookies_lock=multiprocessing.RLock()
                )
            ),
        )
    )
    application.ctx.flow_engine = SimpleNamespace(
        execute=AsyncMock(return_value={"name": "Intruder"})
    )
    login = {"$start": "auth_start", "username": "intruder", "password": "fixture"}

    async def run():
        async with database():
            reader = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            indexer = await FlowGraph.create(
                name="Login",
                category=GraphCategory.INDEXER,
                state=GraphState.PUBLISHED,
                definition={
                    "nodes": [
                        {
                            "id": "auth",
                            "data": {
                                "$schema": "auth_start",
                                "config": "login: {required: true}\n"
                                "cookie: {domain: .example.test, path: /, name: sid}",
                            },
                        }
                    ],
                    "edges": [],
                },
            )
            await FlowVariable.create(
                graph_id=indexer.id,
                key=AUTH_KEY,
                value={"name": "Owner", "token": "saved-token"},
            )
            await GlobalCookie.create(
                name="sid", value="saved", domain=".example.test", path="/"
            )
            path = f"/_api/flow/graph/{indexer.id}/execute"

            for handler, call in (
                (routes.get_indexer_config, request(reader)),
                (routes.get_indexer_auth, request(reader)),
                (routes.indexer_logout, request(reader, body={})),
                (routes.execute_graph, request(reader, path, login)),
            ):
                with pytest.raises(ForbiddenException) as exc_info:
                    await call_route(handler, call, id=indexer.id)
                assert exc_info.value.message == ErrorCode.PERMISSION_DENIED
            application.ctx.flow_engine.execute.assert_not_awaited()
            assert (await FlowVariable.get(graph_id=indexer.id)).value == {
                "name": "Owner",
                "token": "saved-token",
            }
            assert await GlobalCookie.all().count() == 1

            await UserPermission.create(
                user=reader, rel_type=PermType.INDEXER, rel_id=indexer.id
            )
            status = await call_route(
                routes.get_indexer_auth, request(reader), id=indexer.id
            )
            assert response_json(status) == {"name": "Owner"}
            config = await call_route(
                routes.get_indexer_config, request(reader), id=indexer.id
            )
            assert response_json(config)["auth"]["login"] == {"required": True}
            executed = await call_route(
                routes.execute_graph, request(reader, path, login), id=indexer.id
            )
            assert response_json(executed) == {"name": "Intruder"}
            logout = await call_route(
                routes.indexer_logout, request(reader, body={}), id=indexer.id
            )
            assert logout.status == 204
            assert not await FlowVariable.filter(graph_id=indexer.id).exists()
            assert await GlobalCookie.all().count() == 0

    asyncio.run(run())


def test_job_control(application):
    engine = SimpleNamespace(pause_job=AsyncMock(), resume_job=AsyncMock())
    application.ctx.flow_engine = engine

    async def run():
        async with database():
            reader = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            admin = await User.create(
                username="admin", password="unused", role=UserRole.ADMIN
            )
            indexer = await FlowGraph.create(
                name="Indexer",
                category=GraphCategory.INDEXER,
                state=GraphState.PUBLISHED,
            )
            await UserPermission.create(
                user=reader, rel_type=PermType.INDEXER, rel_id=indexer.id
            )
            job = await FlowJob.create(
                graph=indexer, state="running", trigger="cron", cron_expr="0 8 * * *"
            )
            listed = response_json(await call_route(routes.list_jobs, request(reader)))
            assert [item["id"] for item in listed["items"]] == [job.id]

            for handler in (routes.pause_job, routes.resume_job):
                with pytest.raises(ForbiddenException) as exc_info:
                    await call_route(handler, request(reader, body={}), id=job.id)
                assert exc_info.value.status_code == 403
            engine.pause_job.assert_not_awaited()
            engine.resume_job.assert_not_awaited()

            for handler in (routes.pause_job, routes.resume_job):
                response = await call_route(handler, request(admin, body={}), id=job.id)
                assert response.status == 204
            engine.pause_job.assert_awaited_once_with(job.id)
            engine.resume_job.assert_awaited_once_with(job.id)

    asyncio.run(run())


def test_execution_access(application):
    engine = SimpleNamespace(execute=AsyncMock(return_value=[]))
    application.ctx.flow_engine = engine

    async def run():
        async with database():
            reader = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            admin = await User.create(
                username="admin", password="unused", role=UserRole.ADMIN
            )
            graphs = [
                await FlowGraph.create(
                    name=category, category=category, state=GraphState.PUBLISHED
                )
                for category in (
                    GraphCategory.INDEXER,
                    GraphCategory.INGEST,
                    GraphCategory.SCHEDULE,
                )
            ]
            indexer, ingest, general = graphs
            # indexer grants never cover other categories, even with matching IDs
            for graph in graphs:
                await UserPermission.create(
                    user=reader, rel_type=PermType.INDEXER, rel_id=graph.id
                )

            for graph_id, body in (
                (ingest.id, {}),
                (general.id, {}),
                (9999, {}),
                (indexer.id, {"recoverable": True, "recovery_id": 1}),
            ):
                path = f"/_api/flow/graph/{graph_id}/execute"
                with pytest.raises(ForbiddenException) as exc_info:
                    await call_route(
                        routes.execute_graph, request(reader, path, body), id=graph_id
                    )
                assert exc_info.value.message == ErrorCode.FORBIDDEN
            engine.execute.assert_not_awaited()

            for user, graph_id, body in (
                (reader, indexer.id, {}),
                (admin, general.id, {"recovery_id": 1}),
            ):
                path = f"/_api/flow/graph/{graph_id}/execute"
                response = await call_route(
                    routes.execute_graph, request(user, path, body), id=graph_id
                )
                assert response_json(response) == []
            assert [
                (call.kwargs["graph_id"], call.kwargs["recovery_id"])
                for call in engine.execute.await_args_list
            ] == [(indexer.id, None), (general.id, 1)]

    asyncio.run(run())


def test_template_access():
    async def run():
        async with database():
            reader = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            admin = await User.create(
                username="admin", password="unused", role=UserRole.ADMIN
            )
            repo = await FlowRepository.create(
                repo_name="example/flows", repo_url="https://example.test/flows.git"
            )
            tmpl = await FlowTemplate.create(
                repo=repo,
                path="search.json",
                name="Search",
                category=GraphCategory.INDEXER,
                revision=1,
                definition={"nodes": [], "edges": []},
            )
            path = f"/_api/flow/tmpl/{tmpl.id}"

            for handler in (routes.reference_template, routes.copy_template):
                with pytest.raises(ForbiddenException) as exc_info:
                    await call_route(
                        handler, request(reader, path, {"name": "Copy"}), id=tmpl.id
                    )
                assert exc_info.value.status_code == 403
            assert not await FlowGraph.exists()

            for handler, name in (
                (routes.reference_template, "Referenced"),
                (routes.copy_template, "Copied"),
            ):
                await call_route(
                    handler, request(admin, path, {"name": name}), id=tmpl.id
                )
            assert sorted(
                await FlowGraph.all().values_list("name", "tmpl_id", "editable")
            ) == [("Copied", None, True), ("Referenced", tmpl.id, False)]

    asyncio.run(run())


def test_graph_transfer():
    async def run():
        async with database():
            reader = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            admin = await User.create(
                username="admin", password="unused", role=UserRole.ADMIN
            )
            graph = await FlowGraph.create(
                name="Shared",
                category=GraphCategory.INDEXER,
                state=GraphState.PUBLISHED,
                revision=1,
                definition={"nodes": [], "edges": []},
            )
            # an indexer grant does not extend to workflow transfer
            await UserPermission.create(
                user=reader, rel_type=PermType.INDEXER, rel_id=graph.id
            )
            path = "/_api/flow/graph/export"

            with pytest.raises(ForbiddenException) as exc_info:
                await call_route(
                    routes.export_graphs, request(reader, path, {"ids": [graph.id]})
                )
            assert exc_info.value.status_code == 403
            exported = await call_route(
                routes.export_graphs, request(admin, path, {"ids": [graph.id]})
            )
            assert exported.body is not None
            with zipfile.ZipFile(io.BytesIO(exported.body)) as archive:
                assert archive.namelist() == ["Shared.json"]
            await graph.delete()

            path = "/_api/flow/graph/import"
            with pytest.raises(ForbiddenException) as exc_info:
                await call_route(
                    routes.import_graphs, upload(reader, path, exported.body)
                )
            assert exc_info.value.status_code == 403
            assert not await FlowGraph.exists()
            imported = await call_route(
                routes.import_graphs, upload(admin, path, exported.body)
            )
            assert response_json(imported) == 1
            restored = await FlowGraph.get(name="Shared")
            assert restored.state == GraphState.DRAFT
            assert restored.draft == {"nodes": [], "edges": []}

    asyncio.run(run())
