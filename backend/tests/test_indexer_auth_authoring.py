"""Tests for authenticated JSON indexer authoring and its runtime parity."""

import asyncio
import multiprocessing
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from app.core.ai import history, tools
from app.core.cookiejar import SQLiteCookieJar
from app.core.flow.engine import FlowEngine, TransientTask
from app.core.flow.nodes.end import auth as auth_end
from app.core.flow.nodes.end.auth import AuthEndNode
from app.core.flow.nodes.general import http as http_node
from app.core.flow.nodes.general.http import HTTPNode
from app.core.flow.nodes.start.auth import AUTH_CONFIG
from app.core.flow.reference import get_workflow_reference
from app.models.assistant import ConversationCreate
from app.models.flow import FlowGraph, FlowLog, FlowVariable, GraphDocument
from app.models.general import GlobalCookie
from app.models.workflow_authoring import HTTPProbe, TemplateValidation
from app.services import workflow_authoring as authoring
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowGraphService
from app.utils.json import dumps, loads, loads_jsonc
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_workflow_outputs import (
    DETAILS_TEMPLATE,
    JSON_FORMATTER,
    SEARCH_TEMPLATE,
    search_page,
)

USERNAME = "fixture-user"
PASSWORD = 'fixture "pass" & key'
TOKEN = "fixture-access-token"
BORROWED = "borrowed-saved-token"
SESSION = "fixture-session-value"
TOKEN_CONFIG = "login:\n  mode: password\n  required: true\n"
COOKIE_CONFIG = (
    f"{TOKEN_CONFIG}cookie:\n  domain: .example.test\n  path: /\n  name: sid\n"
)
LOGIN_FORMATTER = (
    '{"login": {{ response.text|tojson }}, "status": {{ response.status_code }}}'
)
TOKEN_AUTH = """{% set b = login|jsonpath_first('$') %}
{% if status == 200 and b.code == 0 %}
{"name": {{ b.data.profile.nickname|tojson }},
 "token": {{ b.data.access_token|tojson }},
 "expires_in": {{ b.data.expires_in|tojson }}}
{% else %}{"name": ""}{% endif %}"""
COOKIE_AUTH = """{% set b = login|jsonpath_first('$') %}
{% if status == 200 and b.ok %}{"name": {{ b.user.name|tojson }}}
{% else %}{"name": ""}{% endif %}"""
TOKEN_LOGIN = {
    "code": 0,
    "data": {
        "profile": {"nickname": "Demo Reader"},
        "access_token": TOKEN,
        "expires_in": 7200,
    },
}
SOURCES = {
    "token": {
        "config": TOKEN_CONFIG,
        "login": "https://api.example.test/v1/login",
        "content_type": "application/json",
        "body": '{"account": {{ username|tojson }}, "secret": {{ password|tojson }}}',
        "auth": TOKEN_AUTH,
        "headers": [
            {"key": "Authorization", "value": "Bearer {{ var('$auth').token }}"}
        ],
        "base": "https://api.example.test/v1",
    },
    "cookie": {
        "config": COOKIE_CONFIG,
        "login": "https://example.test/account/login",
        "content_type": "application/x-www-form-urlencoded",
        "body": '{"user": {{ username|tojson }}, "pass": {{ password|tojson }}}',
        "auth": COOKIE_AUTH,
        "headers": [],
        "base": "https://example.test/api",
    },
}


def indexer_changes(kind, auth=None):
    """Build the canvas edits for a complete authenticated JSON indexer.

    Args:
        kind: Synthetic source using token or cookie authentication.
        auth: Optional `auth_end` template replacing the source's tested mapping.

    Returns:
        Add-node and add-edge operations for login, search and details paths.
    """
    source = SOURCES[kind]
    paths = [
        (
            ("auth_start", {"config": source["config"]}),
            {
                "method": "POST",
                "url": source["login"],
                "headers": [{"key": "Content-Type", "value": source["content_type"]}],
                "body": source["body"],
                "formatter": LOGIN_FORMATTER,
            },
            ("auth_end", {"response": auth or source["auth"]}),
        ),
        (
            ("search_start", {}),
            {
                "url": source["base"] + "/search?q={{ keyword|quote('') }}"
                "&page={{ page_num }}&size={{ page_size }}",
                "headers": source["headers"],
                "formatter": JSON_FORMATTER,
            },
            ("search_end", {"response": SEARCH_TEMPLATE}),
        ),
        (
            ("details_start", {}),
            {
                "url": source["base"] + "/items/{{ id|quote('') }}",
                "headers": source["headers"],
                "formatter": JSON_FORMATTER,
            },
            ("details_end", {"response": DETAILS_TEMPLATE}),
        ),
    ]
    changes = []
    for row, (start, request_fields, end) in enumerate(paths):
        for column, (name, fields) in enumerate((start, ("http", request_fields), end)):
            changes.append(
                {
                    "op": "add_node",
                    "node": {
                        "id": f"{row}-{column}",
                        "type": "node",
                        "position": {"x": column * 800, "y": row * 600},
                        "data": {"$schema": name, **fields},
                    },
                }
            )
        for column in (0, 1):
            changes.append(
                {
                    "op": "add_edge",
                    "edge": {
                        "id": f"e{row}-{column}",
                        "source": f"{row}-{column}",
                        "target": f"{row}-{column + 1}",
                        "sourceHandle": "output",
                        "targetHandle": "input",
                    },
                }
            )
    return changes


def definition(changes):
    """Convert canvas edits into the saved graph used after publication.

    Args:
        changes: Add-node and add-edge operations.

    Returns:
        Graph definition with copied node data.
    """
    return loads(
        dumps(
            {
                "nodes": [c["node"] for c in changes if c["op"] == "add_node"],
                "edges": [c["edge"] for c in changes if c["op"] == "add_edge"],
            }
        )
    )


def node_data(changes, schema, row):
    """Select one HTTP node's fields from a generated path.

    Args:
        changes: Add-node and add-edge operations.
        schema: Node type to select.
        row: Path index: login, search or details.

    Returns:
        Node fields without the schema marker.
    """
    node = next(
        c["node"]
        for c in changes
        if c["op"] == "add_node"
        and c["node"]["data"]["$schema"] == schema
        and c["node"]["id"].startswith(f"{row}-")
    )
    return {k: v for k, v in node["data"].items() if k != "$schema"}


class Remote:
    """Serve synthetic APIs that only accept their own login state."""

    def __init__(self, *, max_age=True, tokens=(TOKEN,)):
        """Configure cookie persistence and accepted bearer tokens.

        Args:
            max_age: Whether the session cookie has an explicit lifetime.
            tokens: Bearer tokens accepted by the token API.
        """
        self.requests = []
        self.max_age = max_age
        self.tokens = tokens

    def __call__(self, request):
        """Answer one login, search or details request.

        Args:
            request: Request sent by a workflow node or probe.

        Returns:
            Synthetic JSON response; HTTP 200 also covers rejected logins.
        """
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/login":
            accepted = loads(request.content) == {
                "account": USERNAME,
                "secret": PASSWORD,
            }
            return httpx.Response(
                200,
                json=TOKEN_LOGIN
                if accepted
                else {"code": 1001, "message": "Invalid credentials"},
            )
        if path == "/account/login":
            accepted = parse_qs(request.content.decode()) == {
                "user": [USERNAME],
                "pass": [PASSWORD],
            }
            cookie = f"sid={SESSION}; Domain=example.test; Path=/; Secure"
            return httpx.Response(
                200,
                json={"ok": accepted, "user": {"name": "Demo Reader"}}
                if accepted
                else {"ok": False},
                headers={"set-cookie": cookie + ("; Max-Age=3600" * self.max_age)}
                if accepted
                else {},
            )
        if request.url.host == "api.example.test":
            authorized = request.headers.get("authorization") in {
                f"Bearer {token}" for token in self.tokens
            }
        else:
            authorized = request.headers.get("cookie") == f"sid={SESSION}"
        if not authorized:
            return httpx.Response(401, json={"error": "login required"})
        if path.endswith("/search"):
            return httpx.Response(
                200, json=search_page(int(request.url.params["page"]))
            )
        found = path.endswith("/items/1")
        return httpx.Response(
            200,
            json={
                "resource": {
                    "id": 1,
                    "name": "Lantern 1",
                    "playback": "https://example.test/1.mp4",
                }
                if found
                else None
            },
        )


@pytest.fixture
def runtime(monkeypatch):
    app = SimpleNamespace(
        # cookie saves use the production lock's non-blocking `acquire(block=...)`
        shared_ctx=SimpleNamespace(cookies={}, cookies_lock=multiprocessing.RLock()),
        ctx=SimpleNamespace(),
    )
    app.ctx.cookies = SQLiteCookieJar(app)
    sanic = SimpleNamespace(get_app=lambda: app)
    for module in (http_node, auth_end, authoring):
        monkeypatch.setattr(module, "Sanic", sanic)
    return app


async def execute(graph_id, **params):
    """Run one published entry through the real transient flow engine.

    Args:
        graph_id: Published workflow.
        **params: Boot parameters, including `$start`.

    Returns:
        Workflow result stored in `$retval`.
    """
    async with TransientTask(graph_id, params, True) as task:
        await task.run()
        return await task.retval()


def validate(template, mode, context=None, *, config=None, strict=False):
    """Check a template through the shared authoring service.

    Args:
        template: Exact node template.
        mode: Output contract.
        context: Explicit fixture variables.
        config: `auth_start` configuration for `auth` mode.
        strict: Whether undefined values are rejected.

    Returns:
        Shared template result.
    """
    return authoring.validate_workflow_template(
        TemplateValidation(
            template=template,
            mode=mode,
            context=context or {},
            config=config,
            strict=strict,
        )
    )


def login_fixture(body, status=200):
    """Describe the login HTTP formatter output for a remote response.

    Args:
        body: Remote JSON response.
        status: Remote HTTP status.

    Returns:
        Context keys read by the `auth_end` template.
    """
    return {"login": dumps(body).decode(), "status": status}


@pytest.mark.parametrize(
    ("template", "fixture", "config", "outcome", "warning"),
    [
        (
            TOKEN_AUTH,
            login_fixture(TOKEN_LOGIN),
            TOKEN_CONFIG,
            "authenticated",
            "expires after",
        ),
        (
            COOKIE_AUTH,
            login_fixture({"ok": True, "user": {"name": "Demo Reader"}}),
            COOKIE_CONFIG,
            "authenticated",
            "configured cookie's saved expiry",
        ),
        (
            TOKEN_AUTH,
            login_fixture({"code": 1001, "message": "Invalid credentials"}),
            TOKEN_CONFIG,
            "unauthenticated",
            "earlier login stays",
        ),
        (
            COOKIE_AUTH,
            login_fixture({"ok": False}),
            COOKIE_CONFIG,
            "unauthenticated",
            "still reports a login",
        ),
        (
            TOKEN_AUTH,
            login_fixture("<html>Bad gateway</html>", 502),
            TOKEN_CONFIG,
            "unauthenticated",
            "earlier login stays",
        ),
        (
            '{"name": "A", "token": "t"}',
            {},
            TOKEN_CONFIG,
            "authenticated",
            "until logout",
        ),
        ('{"name": null}', {}, TOKEN_CONFIG, "unauthenticated", "unchanged"),
        ('{"name": "A", "token": "t"}', {}, AUTH_CONFIG, "authenticated", "logout"),
        (
            COOKIE_AUTH,
            login_fixture({"ok": True, "user": {"name": "Demo Reader"}}),
            TOKEN_CONFIG + "cookie: {domain: nas, path: /api, name: sid}",
            "authenticated",
            "nas.local",
        ),
    ],
    ids=[
        "token",
        "cookie",
        "token-failure",
        "cookie-failure",
        "error-page",
        "no-expiry",
        "null-name",
        "default-config",
        "dotless-host",
    ],
)
def test_outcomes(template, fixture, config, outcome, warning):
    result = validate(template, "auth", fixture, config=config)

    assert result.valid, result
    assert result.outcome == outcome
    assert any(warning in item for item in result.warnings), result.warnings
    assert result.rendered is None
    if template == TOKEN_AUTH and outcome == "authenticated":
        assert result.value == {
            "name": "Demo Reader",
            "token": TOKEN,
            "expires_in": 7200,
        }


@pytest.mark.parametrize(
    ("value", "config", "path"),
    [
        ([], TOKEN_CONFIG, "$"),
        ({"name": True, "token": "t"}, TOKEN_CONFIG, "name"),
        ({"name": "  ", "token": "t"}, TOKEN_CONFIG, "name"),
        ({"name": "", "token": "t"}, TOKEN_CONFIG, "name"),
        ({"name": None, "expires_in": 60}, TOKEN_CONFIG, "name"),
        ({"name": "A", "token": 1}, TOKEN_CONFIG, "token"),
        ({"name": "A", "token": ""}, TOKEN_CONFIG, "token"),
        ({"name": "A", "token": "t", "expires_in": "60"}, TOKEN_CONFIG, "expires_in"),
        ({"name": "A", "token": "t", "expires_in": 60.0}, TOKEN_CONFIG, "expires_in"),
        ({"name": "A", "token": "t", "expires_in": True}, TOKEN_CONFIG, "expires_in"),
        ({"name": "A", "token": "t", "expires_in": 0}, TOKEN_CONFIG, "expires_in"),
        ({"name": "A"}, TOKEN_CONFIG, "token"),
        ({"name": "A", "token": "t"}, "login: true", "config.login"),
        ({"name": "A", "token": "t"}, "login: [", "config"),
        ({"name": "A", "token": "t"}, "login: {mode: qrcode}", "config.login.mode"),
        (
            {"name": "A", "token": "t"},
            "login: {required: 'yes'}",
            "config.login.required",
        ),
        ({"name": "A", "token": "t"}, TOKEN_CONFIG + "cookie: sid", "config.cookie"),
        (
            {"name": "A"},
            TOKEN_CONFIG + "cookie: {domain: .example.test, path: /}",
            "config.cookie.name",
        ),
        (
            {"name": "A", "token": "t"},
            TOKEN_CONFIG + "cookie: {path: /, name: sid}",
            "config.cookie.domain",
        ),
        (
            {"name": "A"},
            TOKEN_CONFIG + "cookie: {domain: https://example.test, path: /, name: s}",
            "config.cookie.domain",
        ),
        (
            {"name": "A"},
            TOKEN_CONFIG + "cookie: {domain: Example.test, path: /, name: s}",
            "config.cookie.domain",
        ),
        (
            {"name": "A"},
            TOKEN_CONFIG + "cookie: {domain: example.test:8080, path: /, name: s}",
            "config.cookie.domain",
        ),
        (
            {"name": "A"},
            TOKEN_CONFIG + "cookie: {domain: example.test, path: api, name: s}",
            "config.cookie.path",
        ),
        (
            {"name": "A"},
            TOKEN_CONFIG + "cookie: {domain: example.test, path: /, name: a=b}",
            "config.cookie.name",
        ),
        (
            {"name": "A", "token": "t"},
            "login: {mode: password}\ncookie:\n  domain: .example.test\n path: /\n",
            "config",
        ),
        # nested aliases would expand exponentially if the configuration were loaded
        (
            {"name": "A", "token": "t"},
            'a: &a ["x", "x"]\nb: &b [*a, *a]\nlogin: {mode: password}\n',
            "config",
        ),
    ],
)
def test_auth_errors(value, config, path):
    original = dumps(value)
    result = validate("{{ result|tojson }}", "auth", {"result": value}, config=config)

    assert not result.valid, result
    assert path in [issue.path for issue in result.issues]
    assert result.outcome is None
    assert "validate again" in result.error
    assert dumps(result.value) == original


def test_shared_example():
    example = loads_jsonc(
        (Path(__file__).parents[1] / "static/flow/resp/auth.jsonc").read_text()
    )
    result = validate(
        "{{ result|tojson }}", "auth", {"result": example}, config=TOKEN_CONFIG
    )

    assert result.valid and result.outcome == "authenticated"
    guide = get_workflow_reference("indexer_auth").content
    assert "mode=auth" in guide and "var('$auth').token" in guide
    assert (
        "json_indexer" in guide
        and "indexer_auth" in get_workflow_reference("json_indexer").content
    )


@pytest.mark.parametrize(
    ("mode", "config"), [("auth", None), ("search", TOKEN_CONFIG), ("text", "")]
)
def test_config_pairing(mode, config):
    with pytest.raises(ValidationError, match="mode=auth"):
        TemplateValidation(template="{}", mode=mode, config=config)

    result = asyncio.run(
        tools.execute_tool(
            "validate_workflow_template",
            dumps({"template": "{}", "mode": mode, "config": config}).decode(),
            SimpleNamespace(id=1, role="user"),
            conversation_id=1,
            call_id="pairing",
        )
    )
    assert not result["ok"]
    assert "config is required for mode=auth" in result["error"]["guidance"]


@pytest.mark.parametrize("kind", ["json", "form"])
def test_login_encoding(kind):
    source = SOURCES["token" if kind == "json" else "cookie"]
    data = node_data(
        indexer_changes("token" if kind == "json" else "cookie"), "http", 0
    )
    placeholders = {"username": "placeholder-user", "password": "placeholder-pass"}

    body = validate(source["body"], "object", placeholders, strict=True)
    options = HTTPNode.prepare_request(
        data, {"username": USERNAME, "password": PASSWORD}
    )

    assert body.valid and set(body.value.values()) == set(placeholders.values())
    assert options["method"] == "POST" and options["content"] is None
    if kind == "json":
        assert options["json"] == {"account": USERNAME, "secret": PASSWORD}
        assert options["data"] is None
    else:
        assert options["data"] == {"user": USERNAME, "pass": PASSWORD}
        assert options["json"] is None
    assert all(secret not in dumps(data).decode() for secret in ("fixture", TOKEN))


@pytest.mark.parametrize("auth", [None, {"name": "Demo", "token": "placeholder"}])
def test_optional_login_header(auth):
    template = "{{ var('$auth', {}).token|prefix('Bearer ') }}"
    context = {} if auth is None else {"$auth": auth}
    options = HTTPNode.prepare_request(
        {
            "url": "https://api.example.test/v1/search",
            "headers": [{"key": "Authorization", "value": template}],
        },
        context,
    )

    if auth is None:
        assert "authorization" not in options["headers"]
    else:
        assert options["headers"]["authorization"] == "Bearer placeholder"
        strict = validate(
            "Bearer {{ var('$auth').token }}", "text", context, strict=True
        )
        assert strict.valid and strict.value == "Bearer placeholder"


@pytest.mark.parametrize(
    ("kind", "max_age"),
    [("token", True), ("cookie", True), ("cookie", False)],
    ids=["token", "cookie", "session-cookie"],
)
def test_runtime_parity(monkeypatch, runtime, kind, max_age):
    remote = Remote(max_age=max_age)
    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(remote)
    )
    changes = indexer_changes(kind)
    login = node_data(changes, "http", 0)

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(
                cookies=runtime.ctx.cookies,
                transport=httpx.MockTransport(remote),
                follow_redirects=True,
            ) as client,
        ):
            runtime.ctx.httpx = client
            graph = await FlowGraph.create(
                name=f"{kind} indexer",
                category="indexer",
                state="published",
                definition=definition(changes),
            )
            await FlowVariable.create(
                graph_id=graph.id,
                key="$auth",
                value={"name": "Previous", "token": "stale"},
            )
            assert all(
                secret not in dumps(graph.definition).decode()
                for secret in (USERNAME, PASSWORD, TOKEN, SESSION)
            )

            # a probe with explicit fixtures observes the login without saving it
            observed = {}
            for password in ("wrong", PASSWORD):
                probe = await authoring.probe_http_request(
                    HTTPProbe(
                        request={
                            "url": login["url"],
                            "method": "POST",
                            "headers": {"Content-Type": SOURCES[kind]["content_type"]},
                            "body": login["body"],
                        },
                        context={"username": USERNAME, "password": password},
                        formatter=LOGIN_FORMATTER,
                    ),
                    owner,
                )
                observed[password] = validate(
                    SOURCES[kind]["auth"],
                    "auth",
                    probe.formatted.value,
                    config=SOURCES[kind]["config"],
                )
            probe_body = remote.requests[-1].content
            assert len(runtime.ctx.cookies) == await GlobalCookie.all().count() == 0
            assert (await FlowVariable.get(graph_id=graph.id)).value[
                "name"
            ] == "Previous"
            assert observed["wrong"].outcome == "unauthenticated"
            assert observed[PASSWORD].outcome == "authenticated"

            # an intentional failure keeps the earlier login
            failed = await execute(
                graph.id,
                **{"$start": "auth_start"},
                username=USERNAME,
                password="wrong",
            )
            assert failed == observed["wrong"].value == {"name": ""}
            saved = await FlowVariable.get(graph_id=graph.id)
            assert saved.value == {"name": "Previous", "token": "stale"}

            started = time.time()
            result = await execute(
                graph.id,
                **{"$start": "auth_start"},
                username=USERNAME,
                password=PASSWORD,
            )
            assert remote.requests[-1].content == probe_body
            expected = {
                k: v for k, v in observed[PASSWORD].value.items() if k != "expires_in"
            }
            saved = await FlowVariable.get(graph_id=graph.id)
            assert result == saved.value == expected
            assert (
                await FlowGraphService.indexer_auth(graph.id, runtime.ctx.cookies)
                == expected
            )
            cookies = await GlobalCookie.all()
            if kind == "token":
                assert started + 7200 - 1 <= saved.expires <= time.time() + 7200
                assert not cookies
            elif max_age:
                assert [(c.domain, c.path, c.name) for c in cookies] == [
                    (".example.test", "/", "sid")
                ]
                assert saved.expires == cookies[0].expires
            else:
                assert saved.expires is None and not cookies
                assert runtime.ctx.cookies.get_cookie(".example.test", "/", "sid")

            sent = len(remote.requests)
            for page in (1, 2, 3):
                output = await execute(
                    graph.id,
                    **{"$start": "search_start"},
                    keyword="Lantern",
                    page_num=page,
                    page_size=1,
                )
                check = validate(
                    SEARCH_TEMPLATE,
                    "search",
                    {
                        "payload": dumps(search_page(page)).decode(),
                        "page_num": page,
                        "page_size": 1,
                    },
                )
                assert check.valid and output == check.value
            for item_id, found in (("1", True), ("missing", False)):
                output = await execute(
                    graph.id, **{"$start": "details_start"}, id=item_id
                )
                assert (output is not None) is found
            for sent_request in remote.requests[sent:]:
                if kind == "token":
                    assert sent_request.headers["authorization"] == f"Bearer {TOKEN}"
                    assert "cookie" not in sent_request.headers
                else:
                    assert sent_request.headers["cookie"] == f"sid={SESSION}"
                    assert "authorization" not in sent_request.headers

            # saved-login probes reuse this indexer's state without changing it
            before = (
                await FlowVariable.all().values(),
                await GlobalCookie.all().values(),
            )
            url = SOURCES[kind]["base"] + "/search?page=1"
            reused = await authoring.probe_http_request(
                HTTPProbe(
                    request={
                        "url": url,
                        "headers": {
                            h["key"]: h["value"] for h in SOURCES[kind]["headers"]
                        },
                    },
                    workflow_id=graph.id,
                ),
                owner,
            )
            assert reused.status_code == 200
            other = await authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://other.test/search?page=1"},
                    workflow_id=graph.id,
                ),
                owner,
            )
            assert (
                other.status_code == 401 and "cookie" not in remote.requests[-1].headers
            )
            assert (
                await FlowVariable.all().values(),
                await GlobalCookie.all().values(),
            ) == before

    asyncio.run(run())


def test_validation_isolated(monkeypatch):
    for target in (AuthEndNode, FlowEngine):
        monkeypatch.setattr(
            target, "execute", AsyncMock(side_effect=AssertionError("No execution"))
        )

    async def run():
        async with database():
            graph = await FlowGraph.create(
                name="Saved", category="indexer", state="published"
            )
            await FlowVariable.create(
                graph_id=graph.id, key="$auth", value={"name": "Saved"}
            )
            await GlobalCookie.create(
                name="sid",
                value="saved",
                domain=".example.test",
                path="/",
                expires=int(time.time()) + 60,
            )
            before = (
                await FlowVariable.all().values(),
                await GlobalCookie.all().values(),
            )
            for body in (TOKEN_LOGIN, {"code": 1001}):
                assert validate(
                    TOKEN_AUTH, "auth", login_fixture(body), config=COOKIE_CONFIG
                ).valid
            assert (
                await FlowVariable.all().values(),
                await GlobalCookie.all().values(),
            ) == before
            assert await FlowLog.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_assistant_build(monkeypatch, runtime, verbose):
    streams = []
    base = {}
    changes = indexer_changes("token")
    remote = Remote(tokens=(TOKEN, BORROWED))
    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(remote)
    )
    detail = {
        "resource": {
            "id": 1,
            "name": "Lantern 1",
            "playback": "https://example.test/1.mp4",
        }
    }

    def validation(template, mode, context, **extra):
        """Describe a template check with explicit sample data.

        Args:
            template: Exact node template.
            mode: Output contract.
            context: Isolated fixture values.
            **extra: Additional validation arguments, such as `config`.

        Returns:
            Named assistant tool call.
        """
        return "validate_workflow_template", {
            "template": template,
            "mode": mode,
            "context": context,
            **extra,
        }

    def handler(upstream):
        body = loads(upstream.content)
        assert (
            history.context_size(body["messages"], body.get("tools", []))
            <= 32000 - 4096
        )
        if not body.get("stream"):
            summary = {
                "goals": ["Build the requested authenticated indexer."],
                "constraints": ["Do not publish, execute or log in."],
                "source_turn_ids": loads(body["messages"][-1]["content"])["turn_ids"],
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": dumps(summary).decode(),
                            },
                        }
                    ]
                },
            )
        streams.append(body)
        step = len(streams)
        text = dumps(body["messages"]).decode()
        if step == 1:
            calls = [
                (
                    "create_workflow_draft",
                    {
                        "name": "Authenticated indexer",
                        "category": "indexer",
                        "description": "Log in, search and read details.",
                    },
                )
            ]
        elif step == 3:
            calls = [
                ("get_workflow_reference", {"topic": topic})
                for topic in ("json_indexer", "indexer_auth")
            ] + [
                ("list_node_types", {"node_type": name, "include_examples": False})
                for name in ("auth_start", "auth_end", "http")
            ]
        elif step == 4:
            names = {tool["function"]["name"] for tool in body["tools"]}
            assert "preview_workflow_draft" in names
            assert "list_media_items" not in names
            search = node_data(changes, "http", 1)
            calls = [
                (
                    "probe_http_request",
                    {
                        "request": {
                            "url": search["url"],
                            "headers": {
                                h["key"]: h["value"] for h in search["headers"]
                            },
                        },
                        "context": {
                            "keyword": "Lantern",
                            "page_num": 1,
                            "page_size": 1,
                        },
                        "formatter": JSON_FORMATTER,
                        "workflow_id": base["existing"],
                    },
                )
            ]
        elif step == 5:
            calls = [
                validation(
                    TOKEN_AUTH.replace("profile.nickname", "nickname"),
                    "auth",
                    login_fixture(TOKEN_LOGIN),
                    config=TOKEN_CONFIG,
                )
            ]
        elif step == 6:
            assert "Correct the reported output fields" in text
            calls = (
                [
                    validation(
                        TOKEN_AUTH, "auth", login_fixture(item), config=TOKEN_CONFIG
                    )
                    for item in (TOKEN_LOGIN, {"code": 1001, "message": "Invalid"})
                ]
                + [
                    validation(
                        SEARCH_TEMPLATE,
                        "search",
                        {
                            "payload": dumps(search_page(page)).decode(),
                            "page_num": page,
                            "page_size": 1,
                        },
                    )
                    for page in (1, 3)
                ]
                + [
                    validation(
                        DETAILS_TEMPLATE, "details", {"payload": dumps(item).decode()}
                    )
                    for item in (detail, {"resource": None})
                ]
            )
        elif step == 7:
            assert "preview_workflow_draft" in {
                tool["function"]["name"] for tool in body["tools"]
            }
            # required recipes and contracts survive active-turn compaction
            assert "Indexer password login" in text and "Paginated JSON indexer" in text
            assert "Start an indexer password login" in text
            calls = [
                (
                    "preview_workflow_draft",
                    {
                        "workflow_id": base["workflow_id"],
                        "draft_version": 0,
                        "snapshot_id": base["snapshot_id"],
                        "changes": changes,
                    },
                )
            ]
        else:
            return response(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "content": "Ready to inspect; publish, then log in.",
                        },
                        "stop",
                    )
                ]
            )
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "content": "Inspecting samples. " * 200
                        if verbose and step >= 3
                        else "",
                        "tool_calls": [
                            call_delta(
                                name, dumps(args).decode(), f"{step}-{index}", index
                            )
                            for index, (name, args) in enumerate(calls)
                        ],
                    },
                    "tool_calls",
                )
            ]
        )

    configure(monkeypatch, handler)

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(
                cookies=runtime.ctx.cookies,
                transport=httpx.MockTransport(remote),
            ) as client,
        ):
            runtime.ctx.httpx = client
            existing = await FlowGraph.create(
                name="Existing indexer",
                category="indexer",
                state="published",
                definition=definition(indexer_changes("token")),
            )
            await FlowVariable.create(
                graph_id=existing.id,
                key="$auth",
                value={"name": "Owner", "token": BORROWED},
            )
            base["existing"] = existing.id
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            created = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request("create", prompt="Create an authenticated indexer."),
                    owner,
                )
            ]
            assert created[-1].event == "done", created
            graph = await FlowGraph.get(name="Authenticated indexer")
            snapshot = await Sessions.snapshot(
                conversation["id"],
                graph.id,
                0,
                GraphDocument(nodes=[], edges=[]),
                "canvas",
                owner,
            )
            base.update(workflow_id=graph.id, snapshot_id=snapshot.id)
            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request(
                        "build",
                        prompt="Build login, search and details from the supplied "
                        "API samples; the existing indexer may be probed for an "
                        "authenticated search sample. Do not publish or execute.",
                        context={
                            "page_type": "workflow_designer",
                            "route": f"/settings/workflows/graphs/{graph.id}",
                            "resource_id": graph.id,
                            "snapshot_id": snapshot.id,
                            "draft_version": 0,
                            "unsaved": False,
                            "editable": True,
                            "selected_node_ids": [],
                        },
                    ),
                    owner,
                )
            ]
            assert events[-1].event == "done", events[-1]
            assert len(streams) == 8
            results = [event.data for event in events if event.event == "tool_result"]
            assert all(result["ok"] for result in results), results
            probe = next(r for r in results if r["name"] == "probe_http_request")
            assert (
                probe["data"]["status_code"] == 200 and "redacted" not in probe["data"]
            )
            checks = [
                result["data"]
                for result in results
                if result["name"] == "validate_workflow_template"
            ]
            assert not checks[0]["valid"]
            assert [issue["path"] for issue in checks[0]["issues"]] == ["name"]
            assert all(check["valid"] for check in checks[1:]), checks
            assert [check.get("outcome") for check in checks[1:3]] == [
                "authenticated",
                "unauthenticated",
            ]
            preview = results[-1]["data"]
            assert preview["validation"]["valid"], preview
            assert preview["proposal_id"] and preview["canvas_change_count"] > 0
            proposed = await Sessions.read_snapshot(
                conversation["id"], preview["proposal_snapshot_id"], owner
            )
            nodes = {node["id"]: node for node in proposed.document["nodes"]}
            for item in changes:
                if item["op"] == "add_node":
                    assert nodes[item["node"]["id"]]["data"] == item["node"]["data"]
            assert nodes["0-2"]["data"]["response"] == TOKEN_AUTH
            serialized = dumps(proposed.document).decode()
            assert all(
                secret not in serialized
                for secret in (USERNAME, "fixture ", TOKEN, BORROWED)
            )
            assert BORROWED not in dumps(streams).decode()
            assert len(remote.requests) == 1
            assert remote.requests[0].headers["authorization"] == f"Bearer {BORROWED}"
            await graph.refresh_from_db()
            assert graph.draft in ({"nodes": [], "edges": []}, None)
            assert graph.definition is None and graph.draft_version == 0
            assert await FlowLog.all().count() == await GlobalCookie.all().count() == 0
            assert await FlowVariable.filter(graph_id=graph.id).count() == 0

            # after the user keeps and publishes, the draft logs in by itself
            graph.definition = loads(dumps(proposed.document))
            graph.state = "published"
            await graph.save()
            assert (
                await FlowGraphService.indexer_auth(graph.id, runtime.ctx.cookies)
                is None
            )
            await execute(
                graph.id,
                **{"$start": "auth_start"},
                username=USERNAME,
                password=PASSWORD,
            )
            output = await execute(
                graph.id,
                **{"$start": "search_start"},
                keyword="Lantern",
                page_num=1,
                page_size=1,
            )
            assert output == checks[3]["value"]
            assert remote.requests[-1].headers["authorization"] == f"Bearer {TOKEN}"
            assert (await FlowVariable.get(graph_id=graph.id)).value == {
                "name": "Demo Reader",
                "token": TOKEN,
            }
            assert (await FlowVariable.get(graph_id=existing.id)).value[
                "token"
            ] == BORROWED

    asyncio.run(run())


def test_mcp_parity(monkeypatch):
    execute_flow = AsyncMock(side_effect=AssertionError("No workflow execution"))
    monkeypatch.setattr(FlowEngine, "execute", execute_flow)
    network = AsyncMock(side_effect=AssertionError("No probe request"))
    monkeypatch.setattr(authoring, "_probe_http_request", network)

    async def run():
        async with application() as app:
            reference = await call(
                app, "get_workflow_reference", {"topic": "indexer_auth"}
            )
            assert (
                reference["structuredContent"]
                == get_workflow_reference("indexer_auth").model_dump()
            )
            bad = TOKEN_AUTH.replace("profile.nickname", "nickname")
            outcomes = []
            for template, body in (
                (bad, TOKEN_LOGIN),
                (TOKEN_AUTH, TOKEN_LOGIN),
                (TOKEN_AUTH, {"code": 1001}),
            ):
                fixture = {
                    "template": template,
                    "mode": "auth",
                    "context": login_fixture(body),
                    "config": TOKEN_CONFIG,
                }
                for account in ("reader", "admin"):
                    result = await call(
                        app,
                        "validate_workflow_template",
                        {"validation": fixture},
                        token=account,
                    )
                    assistant = await tools.execute_tool(
                        "validate_workflow_template",
                        dumps(fixture).decode(),
                        app.shared_ctx.sessions[account],
                        conversation_id=1,
                        call_id="auth",
                    )
                    assert result["structuredContent"] == assistant["data"]
                outcomes.append(result["structuredContent"].get("outcome"))
            assert outcomes == [None, "authenticated", "unauthenticated"]

            missing = {"template": TOKEN_AUTH, "mode": "auth"}
            invalid = await call(
                app, "validate_workflow_template", {"validation": missing}
            )
            assert invalid["isError"]
            assert not (
                await tools.execute_tool(
                    "validate_workflow_template",
                    dumps(missing).decode(),
                    app.shared_ctx.sessions["reader"],
                    conversation_id=1,
                    call_id="missing",
                )
            )["ok"]

            # login probes keep their administrator and read_write requirements
            probe = {"request": {"url": SOURCES["token"]["login"], "method": "POST"}}
            for account in ("reader", "admin"):
                denied = await call(
                    app, "probe_http_request", {"probe": probe}, token=account
                )
                assert denied["isError"] and "read-only" in str(denied)
            reader = await tools.execute_tool(
                "probe_http_request",
                dumps(probe).decode(),
                app.shared_ctx.sessions["reader"],
                conversation_id=1,
                call_id="reader-probe",
            )
            assert not reader["ok"]
            assert await FlowVariable.all().count() == 0
            assert await GlobalCookie.all().count() == await FlowLog.all().count() == 0
            network.assert_not_awaited()
            execute_flow.assert_not_awaited()

    asyncio.run(run())
