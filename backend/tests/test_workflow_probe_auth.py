"""Tests for HTTP probes using isolated snapshots of saved indexer login state."""

import asyncio
import itertools
import json
import random
import time
from http.cookiejar import Cookie
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest

from app.core.ai import history, tools
from app.core.cookiejar import SQLiteCookieJar
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.flow.context import Context
from app.core.flow.engine import FlowEngine
from app.core.flow.nodes.general import http as http_node
from app.models.assistant import (
    AssistantConversation,
    AssistantToolCall,
    AssistantTurn,
    ConversationCreate,
)
from app.models.flow import FlowGraph, FlowLog, FlowVariable
from app.models.general import GlobalCookie
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.user import Permissions, UserRole
from app.models.workflow_authoring import HTTPProbe, HTTPProbeResult, TemplateResult
from app.services import workflow_authoring as authoring
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.mcp import MCPAccessTokenService
from app.utils.dict import TrackableDict
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import (
    MODEL,
    completion_client,
    database,
    request,
)
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response

TOKEN = 'saved/token+"secret'
COOKIE = "saved-cookie-secret"
READS_AUTH = {"Authorization": "Bearer {{ var('$auth').token }}"}


def cookie(value=COOKIE, *, domain="example.test", path="/", expires=None, secure=True):
    """Build a synthetic cookie with explicit browser scope.

    Args:
        value: Synthetic cookie value.
        domain: Cookie domain.
        path: Allowed request path.
        expires: Optional Unix expiry time.
        secure: Whether the cookie requires HTTPS.

    Returns:
        Cookie carrying the supplied scope and expiry.
    """
    return Cookie(
        version=0,
        name="session",
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=domain.startswith("."),
        path=path,
        path_specified=True,
        secure=secure,
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={},
    )


@pytest.fixture
def saved_session(monkeypatch):
    app = SimpleNamespace(
        shared_ctx=SimpleNamespace(cookies={}, cookies_lock=RLock()),
        ctx=SimpleNamespace(),
    )
    jar = SQLiteCookieJar(app)
    jar.set_cookie(cookie())
    app.ctx.cookies = jar
    monkeypatch.setattr(authoring, "Sanic", SimpleNamespace(get_app=lambda: app))
    monkeypatch.setattr(
        FlowEngine, "execute", AsyncMock(side_effect=AssertionError("No workflow run"))
    )
    return app


async def indexer(*, token=True, expires=None):
    """Create a synthetic indexer and optional authentication variable.

    Args:
        token: Whether to store a token login as well as cookie configuration.
        expires: Optional authentication-variable expiry time.

    Returns:
        Saved indexer available to an administrator in the temporary database. Its
        HTTP node targets `example.test`, the only host that receives its `$auth`.
    """
    graph = await FlowGraph.create(
        name="Authenticated fixture",
        category="indexer",
        state="published",
        definition={
            "nodes": [
                {
                    "id": "auth",
                    "data": {
                        "$schema": "auth_start",
                        "config": "login: {required: true}\n"
                        "cookie: {domain: example.test, path: /, name: session}",
                    },
                },
                {
                    "id": "search",
                    "data": {
                        "$schema": "http",
                        "url": "https://example.test/search?page={{ page_num }}",
                    },
                },
            ],
            "edges": [],
        },
    )
    if token:
        await FlowVariable.create(
            graph_id=graph.id,
            key="$auth",
            value={"name": "Fixture", "token": TOKEN},
            expires=expires,
        )
    return graph


def test_node_parity(monkeypatch, saved_session):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(
            200,
            json={"items": [{"id": "one"}]},
            headers={"set-cookie": "session=rotated-cookie; Path=/; Secure"},
        )

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    data = {
        "url": "https://example.test/search?page={{ page_num }}",
        "method": "POST",
        "headers": {
            "Authorization": "Bearer {{ var('$auth').token }}",
            "Content-Type": "application/json",
        },
        "body": '{"keyword":{{ keyword|tojson }}}',
    }
    formatter = '{"items":{{ response.text|jsonpath_all("$.items[*]")|tojson }}}'
    fixtures = {"page_num": 2, "keyword": "Lantern"}

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            variables = await FlowVariable.all().values()
            result = await authoring.probe_http_request(
                HTTPProbe(
                    request=data,
                    context=fixtures,
                    formatter=formatter,
                    workflow_id=graph.id,
                ),
                owner,
            )

            assert result.formatted.valid and not result.redacted
            assert result.formatted.value == {"items": [{"id": "one"}]}
            assert saved_session.ctx.cookies.get_cookie("example.test").value == COOKIE
            assert await FlowVariable.all().values() == variables
            assert await GlobalCookie.all().count() == await FlowLog.all().count() == 0
            context = Context.__new__(Context)
            context.globalvars = {}
            context.localvars = {"$auth": variables[0]["value"]}
            context.bootparams = fixtures
            context.storage = TrackableDict()
            context.union()
            async with httpx.AsyncClient(
                cookies=saved_session.ctx.cookies,
                transport=httpx.MockTransport(handler),
            ) as client:
                saved_session.ctx.httpx = client
                monkeypatch.setattr(http_node, "Sanic", authoring.Sanic)
                await http_node.HTTPNode.execute(
                    node_data={
                        **data,
                        "headers": [
                            {"key": key, "value": value}
                            for key, value in data["headers"].items()
                        ],
                        "formatter": formatter,
                    },
                    context=context,
                )
            assert context.storage["items"] == result.formatted.value["items"]

    asyncio.run(run())
    assert len(sent) == 2
    for sent_request in sent:
        assert sent_request.headers["authorization"] == f"Bearer {TOKEN}"
        assert sent_request.headers["cookie"] == f"session={COOKIE}"
        assert sent_request.content == b'{"keyword":"Lantern"}'
        assert str(sent_request.url) == "https://example.test/search?page=2"


@pytest.mark.parametrize(
    ("url", "path", "expected"),
    [
        ("https://example.test/search", "/", True),
        ("https://other.test/search", "/", False),
        ("https://example.test/search", "/account", False),
        ("http://example.test/search", "/", False),
    ],
)
def test_cookie_scope(monkeypatch, saved_session, url, path, expected):
    saved_session.ctx.cookies.set_cookie(cookie(path=path))
    if path != "/":
        saved_session.ctx.cookies.clear("example.test", "/", "session")
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, text="fixture")

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            result = await authoring.probe_http_request(
                HTTPProbe(request={"url": url}, workflow_id=graph.id), owner
            )
            assert result.status_code == 200

    asyncio.run(run())
    assert ("cookie" in sent[0].headers) is expected


@pytest.mark.parametrize(
    "stored", ["cookie", "expired_token", "expired_cookie", "missing"]
)
def test_login_state(monkeypatch, saved_session, stored):
    network = AsyncMock(
        return_value=HTTPProbeResult(status_code=200, body_complete=True)
    )
    monkeypatch.setattr(authoring, "_probe_http_request", network)
    if stored != "cookie":
        saved_session.ctx.cookies.clear()
    if stored == "expired_cookie":
        saved_session.ctx.cookies.set_cookie(cookie(expires=int(time.time()) - 60))

    async def run():
        async with database() as (owner, _):
            graph = await indexer(token=stored == "expired_token", expires=1)
            before = await FlowVariable.all().values()
            result = await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://example.test"}, workflow_id=graph.id
                ),
                owner,
            )
            if stored == "cookie":
                assert result.status_code == 200
                assert "$auth" not in network.call_args.args[1]
                assert len(network.call_args.args[2]) == 1
            else:
                assert "missing or expired" in result.error
                network.assert_not_awaited()
            assert await FlowVariable.all().values() == before
            assert await GlobalCookie.all().count() == 0

    asyncio.run(run())


def test_isolation_and_override(monkeypatch, saved_session):
    network = AsyncMock(return_value=HTTPProbeResult(status_code=200))
    monkeypatch.setattr(authoring, "_probe_http_request", network)

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            args = HTTPProbe(request={"url": "https://example.test"})
            await authoring.probe_http_request(args, owner)
            assert network.call_args.args[1] == {}
            assert len(network.call_args.args[2]) == 0

            args.workflow_id = graph.id
            args.context = {"$auth": {"token": "explicit-fixture"}}
            await authoring.probe_http_request(args, owner)
            assert network.call_args.args[1] == args.context
            assert (await FlowVariable.get(graph_id=graph.id)).value["token"] == TOKEN

    asyncio.run(run())


def test_optional_login(monkeypatch, saved_session):
    network = AsyncMock(return_value=HTTPProbeResult(status_code=200))
    monkeypatch.setattr(authoring, "_probe_http_request", network)

    async def run():
        async with database() as (owner, _):
            graph = await indexer(token=False)
            graph.definition["nodes"][0]["data"]["config"] = "login: {required: false}"
            await graph.save()
            result = await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://example.test"}, workflow_id=graph.id
                ),
                owner,
            )
            assert result.status_code == 200
            assert network.call_args.args[1] == {}
            assert len(network.call_args.args[2]) == 1

    asyncio.run(run())


def test_redaction(monkeypatch, saved_session):
    rotated = "rotated-cookie-secret"
    payload = {"token": TOKEN, "cookie": COOKIE, "new_cookie": rotated}

    def handler(request):
        return httpx.Response(
            200,
            json=payload,
            headers={"set-cookie": f"session={rotated}; Path=/; Secure"},
        )

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            result = await authoring.probe_http_request(
                HTTPProbe(
                    request={
                        "url": "https://example.test"
                        "?q={{ var('$auth').token|quote('') }}"
                    },
                    workflow_id=graph.id,
                    formatter='{"auth":{{ var("$auth").token|tojson }},'
                    '"data":{{ response.text }}}',
                ),
                owner,
            )
            assert result.redacted
            assert result.formatted.valid
            assert result.formatted.value["auth"] == "[redacted]"
            assert set(result.formatted.value["data"].values()) == {"[redacted]"}
            serialized = dumps(result.model_dump()).decode()
            for secret in (TOKEN, COOKIE, rotated, quote(TOKEN, safe="")):
                assert secret not in serialized
            assert saved_session.ctx.cookies.get_cookie("example.test").value == COOKIE

    asyncio.run(run())


def test_redaction_limits():
    result = authoring._redact_probe_result(
        HTTPProbeResult(
            text="x" * 2000,
            formatted=TemplateResult(valid=True, value={"text": "x" * 2000}),
        ),
        {"x", "a"},
    )
    assert result.redacted and result.text_truncated
    assert len(result.text) == 2000
    assert "[redacted]" in result.text
    assert not result.formatted.valid and "8000" in result.formatted.error


def test_encoded_credentials(monkeypatch, saved_session):
    token = "privé-token<&'"
    encoded = json.dumps(token)[1:-1]
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda _: httpx.Response(200, text=json.dumps({"token": token}))
        ),
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            await FlowVariable.filter(graph_id=graph.id).update(
                value={"name": "Fixture", "token": token}
            )
            result = await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://example.test"},
                    workflow_id=graph.id,
                    formatter='{"token":{{ var("$auth").token|tojson }}}',
                ),
                owner,
            )
            assert result.redacted and result.formatted.valid
            assert result.formatted.value == {"token": "[redacted]"}
            assert encoded not in result.text
            assert "priv" not in result.formatted.rendered

    asyncio.run(run())


def test_formatter_error_redaction(monkeypatch, saved_session):
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(lambda _: httpx.Response(200, text="fixture")),
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            result = await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://example.test"},
                    workflow_id=graph.id,
                    formatter="{{ [1]|map('x' * 270 ~ var('$auth').token)|list }}",
                ),
                owner,
            )
            assert result.status_code == 200 and not result.formatted.valid
            assert "saved" not in result.formatted.error
            assert len(result.formatted.error) <= 350

    asyncio.run(run())


@pytest.mark.parametrize("field,limit", [("text", 2000), ("last-modified", 512)])
def test_redaction_before_clipping(monkeypatch, saved_session, field, limit):
    value = "x" * (limit - 10) + TOKEN

    def handler(request):
        return httpx.Response(
            200,
            text=value if field == "text" else "fixture",
            headers={field: value} if field != "text" else {},
        )

    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(handler),
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            result = await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://example.test"}, workflow_id=graph.id
                ),
                owner,
            )
            assert result.redacted
            value = result.text if field == "text" else result.headers[field]
            assert TOKEN[:10] not in value
            assert len(value) <= limit
            if field == "text":
                assert result.text_truncated

    asyncio.run(run())


def test_permissions(monkeypatch, saved_session):
    network = AsyncMock(side_effect=AssertionError("No request expected"))
    monkeypatch.setattr(authoring, "_probe_http_request", network)

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            args = HTTPProbe(
                request={"url": "https://example.test"}, workflow_id=graph.id
            )
            reader = SimpleNamespace(
                role=UserRole.USER, perms=Permissions(indexer_ids=[graph.id])
            )
            with pytest.raises(ForbiddenException):
                await authoring.probe_http_request(args, reader)
            graph.category = "ingest"
            await graph.save()
            assert (
                "Select an indexer"
                in (await authoring.probe_http_request(args, owner)).error
            )
            await graph.delete()
            with pytest.raises(NotFoundException):
                await authoring.probe_http_request(args, owner)
            network.assert_not_awaited()

    asyncio.run(run())


def test_curl_snapshot(monkeypatch, saved_session):
    sent = []

    class Response:
        status_code = 200
        url = "https://example.test/search"
        headers = httpx.Headers({"Content-Type": "application/json"})
        quit_now = None
        astream_task = None

        async def aiter_content(self):
            yield b'{"items":[]}'

        async def aclose(self):
            pass

    class Client(authoring.AsyncSession):
        def __init__(self, **kwargs):
            # a borrowed login must not follow redirects to other hosts
            assert kwargs["allow_redirects"] is False
            super().__init__(**kwargs)

        async def request(self, method, url, **kwargs):
            sent.append(kwargs)
            assert self.cookies.jar is not saved_session.ctx.cookies
            assert (
                self.cookies.get("session", domain="example.test", path="/") == COOKIE
            )
            self.cookies.set(
                "session", "rotated-cookie", domain="example.test", path="/"
            )
            return Response()

    monkeypatch.setattr(authoring, "AsyncSession", Client)
    monkeypatch.setattr(http_node, "resolve_proxy", AsyncMock(return_value=None))

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            result = await authoring.probe_http_request(
                HTTPProbe(
                    request={
                        "client": "curl_cffi",
                        "url": "https://example.test/search",
                        "headers": {"Authorization": "Bearer {{ var('$auth').token }}"},
                    },
                    workflow_id=graph.id,
                ),
                owner,
            )
            assert result.body_complete and result.status_code == 200
            assert saved_session.ctx.cookies.get_cookie("example.test").value == COOKIE
            assert sent[0]["headers"] == [("authorization", f"Bearer {TOKEN}")]
            assert sent[0]["impersonate"] == "chrome"
            assert await GlobalCookie.all().count() == 0

    asyncio.run(run())


def test_tool_parity(monkeypatch, saved_session):
    network = []

    def handler(request):
        network.append(request)
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["cookie"] == f"session={COOKIE}"
        return httpx.Response(200, json={"title": "Private fixture"})

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with application() as app:
            graph = await indexer()
            args = {
                "request": {
                    "url": "https://example.test/search",
                    "headers": {"Authorization": "Bearer {{ var('$auth').token }}"},
                },
                "workflow_id": graph.id,
            }
            for name in ("reader", "admin"):
                result = await call(
                    app, "probe_http_request", {"probe": args}, token=name
                )
                assert result["isError"] and "read-only" in str(result)
                user = app.shared_ctx.sessions[name]
                token = await MCPAccessTokenService.create(
                    user.id,
                    MCPAccessTokenCreate(
                        name="auth probe", scope=MCPTokenScope.READ_WRITE
                    ),
                )
                result = await call(
                    app, "probe_http_request", {"probe": args}, token=token["token"]
                )
                if name == "reader":
                    assert result["isError"]
                else:
                    invalid = {**args, "workflow_id": str(graph.id)}
                    denied = await call(
                        app,
                        "probe_http_request",
                        {"probe": invalid},
                        token=token["token"],
                    )
                    assert denied["isError"]
                    denied = await tools.execute_tool(
                        "probe_http_request",
                        dumps(invalid).decode(),
                        user,
                        conversation_id=1,
                        call_id="invalid-probe",
                    )
                    assert not denied["ok"]
                    assistant = await tools.execute_tool(
                        "probe_http_request",
                        dumps(args).decode(),
                        user,
                        conversation_id=1,
                        call_id="probe",
                    )
                    assert assistant["ok"]
                    assert result["structuredContent"] == assistant["data"]
            assert len(network) == 2
            assert await GlobalCookie.all().count() == await FlowLog.all().count() == 0

    asyncio.run(run())


def test_history_access(monkeypatch, saved_session):
    fixture, streams = {}, []

    def model_handler(upstream):
        body = loads(upstream.content)
        assert history.context_size(body["messages"], body.get("tools", [])) <= 27904
        assert (
            TOKEN not in upstream.content.decode()
            and COOKIE not in upstream.content.decode()
        )
        streams.append(body)
        if len(streams) == 1:
            return response(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                call_delta(
                                    "probe_http_request", dumps(fixture).decode()
                                )
                            ],
                        },
                        "tool_calls",
                    )
                ]
            )
        return response(
            [chunk({"role": "assistant", "content": "Private result read."}, "stop")]
        )

    configure(monkeypatch, model_handler)
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda _: httpx.Response(
                200, json={"title": "Private fixture", "token": TOKEN}
            )
        ),
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            fixture.update(
                workflow_id=graph.id,
                request={"url": "https://example.test/search"},
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request("probe", prompt="Probe the selected indexer."),
                    owner,
                )
            ]
            assert events[-1].event == "done"
            turn = await AssistantTurn.get(request_id="probe")
            assert turn.resource_ids == [graph.id]
            stored = await AssistantToolCall.get(turn_id=turn.id)
            assert stored.result["ok"] and stored.result["data"]["redacted"]
            assert TOKEN not in dumps(stored.result).decode()
            owner.role, owner.perms = UserRole.USER, Permissions(indexer_ids=[])
            restored = await Sessions.turns(conversation["id"], owner)
            assert (
                not restored["items"][0]["content"]
                and not restored["items"][0]["tools"]
            )
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": turn.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="history",
            )
            assert denied["error"]["code"] == "permission_denied"
            await AssistantConversation.filter(id=conversation["id"]).update(
                summary={"goals": ["Private fixture"]},
                summary_resources=[graph.id],
                summary_through=turn.id,
            )
            current = await Sessions.begin(conversation["id"], request("next"), owner)
            create = AsyncMock(side_effect=AssertionError("No model call expected"))
            messages, _, _ = await history.prepare_context(
                completion_client(create), current, owner, "system", [], None
            )
            assert "Private" not in dumps(messages).decode()
            assert len(streams) == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    ("url", "headers", "source", "sent_auth"),
    [
        ("https://EXAMPLE.test/search", READS_AUTH, None, f"Bearer {TOKEN}"),
        ("https://other.test/search", READS_AUTH, None, None),
        ("https://other.test/search", {}, None, ""),
        ("https://example.test/search", READS_AUTH, "{{ base }}/search", None),
        ("https://example.test/search", READS_AUTH, "https://{{ host }}/", None),
    ],
    ids=["source-host", "other-host", "other-host-unused", "templated", "host-var"],
)
def test_auth_hosts(monkeypatch, saved_session, url, headers, source, sent_auth):
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, text="fixture")

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            if source is not None:
                definition = graph.definition
                definition["nodes"][1]["data"]["url"] = source
                await FlowGraph.filter(id=graph.id).update(definition=definition)
            return await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": url, "headers": headers}, workflow_id=graph.id
                ),
                owner,
            )

    result = asyncio.run(run())

    if sent_auth is None:
        assert not sent and result.status_code is None
        assert "only sent to hosts" in result.error and TOKEN not in result.error
        assert ("example.test" in result.error) is (source is None)
    else:
        assert [r.headers.get("authorization", "") for r in sent] == [sent_auth]


@pytest.mark.parametrize("selected", [False, True], ids=["anonymous", "indexer"])
def test_received_cookies(monkeypatch, saved_session, selected):
    first, second, rejected = "first-session-value", "second-session-value", "x" * 12

    def handler(request):
        if request.url.path == "/login":
            return httpx.Response(
                302,
                headers={
                    "location": "/home",
                    "set-cookie": f"session={first}; Path=/; Secure",
                },
            )
        return httpx.Response(
            200,
            json={"echo": [first, second, rejected], "lang": "en"},
            headers=[
                ("set-cookie", f"session={second}; Path=/; Secure"),
                ("set-cookie", "lang=en; Path=/"),
                ("set-cookie", f"tracker={rejected}; Domain=elsewhere.test"),
            ],
        )

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with database() as (owner, _):
            # a saved `$auth` would stop redirects; copied cookies alone follow them
            graph = await indexer(token=False)
            return await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://example.test/login", "method": "POST"},
                    formatter='{"data": {{ response.text }}, '
                    '"cookies": {{ response.headers.get_list("set-cookie")|tojson }}}',
                    workflow_id=graph.id if selected else None,
                ),
                owner,
            )

    result = asyncio.run(run())

    assert result.status_code == 200 and result.redacted
    serialized = dumps(result.model_dump()).decode()
    for secret in (first, second, rejected):
        assert secret not in serialized
    assert result.formatted.value["data"] == {
        "echo": ["[redacted]"] * 3,
        "lang": "en",
    }
    assert "lang=en; Path=/" in result.formatted.value["cookies"]
    assert saved_session.ctx.cookies.get_cookie("example.test").value == COOKIE


@pytest.mark.parametrize("client", ["httpx", "curl_cffi"])
def test_encoded_size(monkeypatch, client):
    network = AsyncMock(side_effect=AssertionError("Oversized request was sent"))
    monkeypatch.setattr(authoring.HTTPNode, "send_request", network)

    async def run():
        async with database() as (owner, _):
            return await authoring.probe_http_request(
                HTTPProbe(
                    request={
                        "url": "https://example.test/login",
                        "method": "POST",
                        "client": client,
                        "headers": {
                            "Content-Type": "application/x-www-form-urlencoded"
                        },
                        "body": '{"q": {{ big|tojson }}}',
                    },
                    # 24,000 rendered characters encode to 72,000 form bytes
                    context={"big": "%" * 24000},
                ),
                owner,
            )

    result = asyncio.run(run())

    assert result.error == "Rendered request exceeds 64 KiB."
    network.assert_not_awaited()


@pytest.mark.parametrize(
    ("options", "client", "size"),
    [
        ({"json": {"q": "é"}}, "httpx", len('{"q":"é"}'.encode())),
        ({"json": {"q": "é"}}, "curl_cffi", len('{"q":"\\u00e9"}')),
        ({"data": {"q": "%", "n": 1}}, "httpx", len("q=%25&n=1")),
        ({"data": {"q": "%", "n": 1}}, "curl_cffi", len("q=%25&n=1")),
        ({"content": b"raw"}, "httpx", 3),
    ],
)
def test_body_size(options, client, size):
    prepared = {"content": None, "data": None, "json": None, **options}

    assert authoring._body_size(prepared, client) == size


@pytest.mark.parametrize(
    ("hosts", "sent_host"),
    [(["example.test", "outside.test"], "example.test"), (["outside.test"], None)],
    ids=["allowed-first", "outside-first"],
)
def test_single_render(monkeypatch, saved_session, hosts, sent_host):
    sent = []
    # alternate hosts on each render, as a nondeterministic template could
    choices = itertools.cycle(hosts + ["example.test"])
    monkeypatch.setattr(random, "choice", lambda _: next(choices))

    def handler(request):
        sent.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(200)

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            return await authoring.probe_http_request(
                HTTPProbe(
                    request={
                        "url": "https://{{ ['example.test', 'outside.test']|random }}/",
                        "headers": READS_AUTH,
                    },
                    workflow_id=graph.id,
                ),
                owner,
            )

    result = asyncio.run(run())

    if sent_host is None:
        assert not sent and "only sent to hosts" in result.error
    else:
        assert sent == [(sent_host, f"Bearer {TOKEN}")]


def test_borrowed_redirect(monkeypatch, saved_session):
    sent = []

    def handler(request):
        sent.append((request.url.host, request.headers.get("x-token")))
        return httpx.Response(307, headers={"location": "https://outside.test/in"})

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            return await authoring.probe_http_request(
                HTTPProbe(
                    request={
                        "url": "https://example.test/search",
                        "method": "POST",
                        "headers": {"X-Token": "{{ var('$auth').token }}"},
                        "body": "token={{ var('$auth').token|quote('') }}",
                    },
                    workflow_id=graph.id,
                ),
                owner,
            )

    result = asyncio.run(run())

    assert sent == [("example.test", TOKEN)]
    assert result.status_code == 307
    assert result.headers["location"] == "https://outside.test/in"


@pytest.mark.parametrize("selected", [True, False], ids=["configured", "http-only"])
def test_short_session_cookies(monkeypatch, saved_session, selected):
    saved_session.ctx.cookies.set_cookie(cookie("abc123"))

    def handler(request):
        return httpx.Response(
            200,
            text=f"sent {request.headers.get('cookie')}; set xyz789 and q1w2",
            headers=[
                ("set-cookie", "session=xyz789; Path=/"),
                ("set-cookie", "sid=q1w2; Path=/; httponly"),
                ("set-cookie", "lang=en; Path=/"),
            ],
        )

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with database() as (owner, _):
            graph = await indexer()
            return await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://example.test/search"},
                    formatter='{"cookies": '
                    '{{ response.headers.get_list("set-cookie")|tojson }}}',
                    workflow_id=graph.id if selected else None,
                ),
                owner,
            )

    result = asyncio.run(run())
    serialized = dumps(result.model_dump()).decode()

    assert result.redacted and "q1w2" not in serialized
    assert "lang=en; Path=/" in result.formatted.value["cookies"]
    if selected:
        # the indexer configures `session` as its login cookie
        assert "abc123" not in serialized and "xyz789" not in serialized
