"""Tests for shared HTTP probes and fixture-based workflow template checks."""

import asyncio
import gzip
from http.cookiejar import CookieJar
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from app.core.ai import tools
from app.core.exceptions import ForbiddenException
from app.core.flow.context import Context
from app.core.flow.nodes.general import http as http_node
from app.core.renderer import render
from app.models.user import UserRole
from app.models.workflow_authoring import HTTPProbe, TemplateValidation
from app.services import workflow_authoring as authoring
from app.utils.dict import TrackableDict
from app.utils.json import dumps, loads_jsonc

ADMIN = SimpleNamespace(role=UserRole.ADMIN)
READER = SimpleNamespace(role=UserRole.USER)


def evaluate(template, **kwargs):
    """Evaluate a template using the public shared input contract.

    Args:
        template: Template or literal text.
        **kwargs: Fixture context and output options.

    Returns:
        Shared validation result.
    """
    return authoring.validate_workflow_template(
        TemplateValidation(template=template, **kwargs)
    )


@pytest.mark.parametrize(
    ("template", "mode", "context", "expected"),
    [
        ("Hello {{ name }}", "text", {"name": "Reader"}, "Hello Reader"),
        ("{{ missing }}", "text", {}, ""),
        ("{{", "text", {}, "{{"),
        ("{{ count > 1 }}", "expression", {"count": 0}, False),
        ("{{ items }}", "expression", {"items": []}, []),
        ("{{ $auth.token }}", "expression", {"$auth": {"token": "fixture"}}, "fixture"),
        (
            "Bearer {{ var('$auth').token }}",
            "text",
            {"$auth": {"token": "fixture"}},
            "Bearer fixture",
        ),
        ("{{ var('missing', {}) }}", "expression", {}, {}),
        ("// value\nnull", "json", {}, None),
        ("false", "json", {}, False),
        ("0", "json", {}, 0),
        ("{}", "object", {}, {}),
        ("[1,]", "array", {}, [1]),
        (
            '{"title":{{ title|tojson }}}',
            "object",
            {"title": 'A "quoted" 中文 title'},
            {"title": 'A "quoted" 中文 title'},
        ),
        (
            "{{ html|xpath_all('//a/@href')|tojson }}",
            "array",
            {"html": '<a href="/one">One</a><a href="/two">Two</a>'},
            ["/one", "/two"],
        ),
        (
            "{{ response.text|jsonpath_all('$.items[*].id')|tojson }}",
            "array",
            {"response": {"text": '{"items":[{"id":"a"},{"id":"b"}]}'}},
            ["a", "b"],
        ),
    ],
)
def test_template_values(template, mode, context, expected):
    result = evaluate(template, mode=mode, context=context)

    assert result.valid, result
    assert result.value == expected
    assert result.value_type == type(expected).__name__
    assert result.rendered == render(template, context, raw=mode == "expression")


@pytest.mark.parametrize(
    ("template", "kwargs", "error"),
    [
        ("{{ missing }}", {"strict": True}, "UndefinedError"),
        ("{{ }}", {}, "TemplateSyntaxError"),
        ('{"bad":}', {"mode": "json"}, "JSONDecodeError"),
        ("[]", {"mode": "object"}, "Expected a JSON object"),
        ("{}", {"mode": "array"}, "Expected a JSON array"),
        ("{{ 'x' * 9000 }}", {}, "exceeds 8000"),
        ("{{ cycler.__init__.__globals__ }}", {}, "SecurityError"),
        ("{{ items.append(2) }}", {"context": {"items": [1]}}, "SecurityError"),
        ("{{ 'text'|regex_first('(') }}", {}, "PatternError"),
        ("{{ '<p/>'|xpath_all('[') }}", {}, "XPathSyntaxError"),
        ("{{ '{}'|jsonpath_all('$[') }}", {}, "JsonPathParserError"),
        (
            "{{ '<p/>'|xpath_first('//p') }}",
            {"mode": "expression"},
            "not JSON-serializable",
        ),
    ],
)
def test_template_errors(template, kwargs, error):
    original = dumps(kwargs)
    result = evaluate(template, **kwargs)

    assert not result.valid
    assert error in result.error
    assert result.value is None
    assert dumps(kwargs) == original


@pytest.mark.parametrize(
    "data",
    [
        {"request": {"url": "https://example.test", "method": "DELETE"}},
        {"request": {"url": "https://example.test", "timeout": 0}},
        {"request": {"url": "https://example.test", "timeout": 11}},
        {"request": {"url": "https://example.test", "timeout": True}},
        {"request": {"url": "https://example.test"}, "context": {"x": "x" * 65536}},
        {"request": {"url": "https://example.test"}, "workflow_id": 0},
        {"request": {"url": "https://example.test"}, "workflow_id": "1"},
        {"request": {"url": "https://example.test"}, "workflow_id": 1.0},
        {"request": {"url": "https://example.test"}, "workflow_id": True},
    ],
)
def test_probe_arguments(data):
    with pytest.raises(ValidationError):
        HTTPProbe.model_validate(data)


@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("application/json", '{"page": {{ page }},}', b'{"page":2}'),
        ("application/x-www-form-urlencoded", '{"page": {{ page }}}', b"page=2"),
        ("text/plain", '{"page": {{ page }}}', b'{"page": 2}'),
        ("application/json", "invalid", b"invalid"),
    ],
)
def test_request_matches_node(monkeypatch, content_type, body, expected):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [{"id": "one"}], "total": 3})

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    formatter = (
        '{"total":{{ response.text|jsonpath_first("$.total") }},'
        '"items":{{ response.text|jsonpath_all("$.items[*]")|tojson }}}'
    )
    data = {
        "url": "https://example.test/search?q={{ keyword|quote('') }}",
        "method": "POST",
        "headers": {"Content-Type": content_type, "X-Hishel-Ttl": "60"},
        "body": body,
    }
    fixture = {"page": 2, "keyword": "a/b"}

    async def run():
        result = await authoring.probe_http_request(
            HTTPProbe(request=data, context=fixture, formatter=formatter), ADMIN
        )
        assert result.body_complete
        assert result.formatted.valid
        assert result.formatted.value == {"total": 3, "items": [{"id": "one"}]}

        context = Context.__new__(Context)
        context.globalvars = context.localvars = {}
        context.bootparams = fixture
        context.storage = TrackableDict()
        context.union()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(
                http_node,
                "Sanic",
                SimpleNamespace(
                    get_app=lambda: SimpleNamespace(ctx=SimpleNamespace(httpx=client))
                ),
            )
            await http_node.HTTPNode.execute(
                node_data={
                    **data,
                    "headers": [
                        {"key": k, "value": v} for k, v in data["headers"].items()
                    ],
                    "formatter": formatter,
                },
                context=context,
            )
        assert context.storage["items"] == result.formatted.value["items"]
        assert context.storage["total"] == result.formatted.value["total"]

    asyncio.run(run())
    assert len(requests) == 2
    for request in requests:
        assert str(request.url) == "https://example.test/search?q=a%2Fb"
        assert request.content == expected
        assert "x-hishel-ttl" not in request.headers
        assert request.extensions["hishel_ttl"] == 60


def test_redirect_cookies(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/start":
            assert "cookie" not in request.headers
            return httpx.Response(
                302, headers={"location": "/end", "set-cookie": "probe=one; Path=/"}
            )
        assert request.headers["cookie"] == "probe=one"
        return httpx.Response(
            200,
            text="done",
            headers={"set-cookie": "secret=fixture", "Content-Type": "text/plain"},
        )

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        for _ in range(2):
            result = await authoring.probe_http_request(
                HTTPProbe(request={"url": "https://example.test/start"}), ADMIN
            )
            assert result.status_code == 200
            assert result.url == "https://example.test/end"
            assert "set-cookie" not in result.headers
            assert result.text == "done"
            assert result.body_complete and not result.text_truncated

    asyncio.run(run())
    assert len(seen) == 4


@pytest.mark.parametrize("status", [200, 401, 404, 500])
def test_http_status(monkeypatch, status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "fixture"})

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(request={"url": "https://example.test"}), ADMIN
        )
    )

    assert result.status_code == status
    assert result.error is None
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
def test_transport_errors(monkeypatch, failure):
    calls = []

    def handler(request):
        calls.append(request)
        raise failure("fixture secret must not appear", request=request)

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(request={"url": "https://example.test"}), ADMIN
        )
    )

    assert result.error
    assert "secret" not in result.error
    assert result.status_code is None
    assert len(calls) == 1


def test_body_limits(monkeypatch):
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(300):
                yield b"x" * 1024

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Stream())
        ),
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(request={"url": "https://example.test"}, formatter="{}"), ADMIN
        )
    )

    assert result.bytes_read == authoring.MAX_RESPONSE_BYTES + 1024
    assert len(result.text) == authoring.MAX_SAMPLE_CHARS
    assert not result.body_complete and result.text_truncated
    assert not result.formatted.valid
    assert "not evaluated" in result.formatted.error
    assert closed == [True]


@pytest.mark.parametrize("action", ["cancel", "timeout"])
def test_interrupted_stream(monkeypatch, action):
    closed = []

    async def run():
        started = asyncio.Event()

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b"unreachable"

            async def aclose(self):
                closed.append(True)

        monkeypatch.setattr(
            authoring,
            "NetworkTransport",
            lambda **_: httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Stream())
            ),
        )
        task = asyncio.create_task(
            authoring.probe_http_request(
                HTTPProbe(request={"url": "https://example.test", "timeout": 1}),
                ADMIN,
            )
        )
        await started.wait()
        if action == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert "timed out" in result.error

    asyncio.run(run())
    assert closed == [True]


def test_redirect_limit(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "/loop"})

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(request={"url": "https://example.test/loop"}), ADMIN
        )
    )

    assert len(seen) == 6
    assert result.error and result.status_code is None


def test_head(monkeypatch):
    def handler(request):
        assert request.method == "HEAD"
        return httpx.Response(204, headers={"content-length": "2000000"})

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(request={"url": "https://example.test", "method": "HEAD"}),
            ADMIN,
        )
    )

    assert result.status_code == 204 and result.body_complete
    assert result.text == "" and result.bytes_read == 0
    assert not result.text_truncated


def test_rendered_request_limit(monkeypatch):
    network = AsyncMock(side_effect=AssertionError("No request expected"))
    monkeypatch.setattr(http_node.HTTPNode, "send_request", network)
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(
                request={
                    "url": "https://example.test",
                    "method": "POST",
                    "body": "{{ 'x' * 65536 }}",
                }
            ),
            ADMIN,
        )
    )

    assert "64 KiB" in result.error
    network.assert_not_awaited()


def test_extract_before_clipping(monkeypatch):
    payload = {"padding": "x" * 4000, "id": "last"}
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda request: httpx.Response(200, json=payload)
        ),
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(
                request={"url": "https://example.test"},
                formatter='{"id":{{ response.text|jsonpath_first("$.id")|tojson }}}',
            ),
            ADMIN,
        )
    )

    assert result.body_complete and result.text_truncated
    assert "last" not in result.text
    assert result.formatted.value == {"id": "last"}


def test_compressed_response(monkeypatch):
    payload = '{"title":"中文"}'.encode()
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={
                    "content-encoding": "gzip",
                    "content-type": "application/json; charset=utf-8",
                },
                content=gzip.compress(payload),
            )
        ),
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(
                request={"url": "https://example.test"},
                formatter='{"title":'
                '{{ response.text|jsonpath_first("$.title")|tojson }}}',
            ),
            ADMIN,
        )
    )

    assert result.error is None
    assert result.bytes_read == len(payload)
    assert result.formatted.value == {"title": "中文"}


@pytest.mark.parametrize("formatter", ['{"url":"{{ response.url }}"}', "invalid"])
def test_formatter_result(monkeypatch, formatter):
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda request: httpx.Response(404, text="missing")
        ),
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(request={"url": "https://example.test"}, formatter=formatter),
            ADMIN,
        )
    )

    assert result.status_code == 404 and result.error is None
    assert result.text == "missing"
    if formatter == "invalid":
        assert not result.formatted.valid
        assert "JSONDecodeError" in result.formatted.error
    else:
        assert result.formatted.value == {"url": "https://example.test"}


def test_response_headers(monkeypatch):
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers=[
                    (b"x-title", b"caf\xe9"),
                    (b"x-repeat", b"one"),
                    (b"x-repeat", b"two"),
                ],
            )
        ),
    )
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(
                request={"url": "https://example.test"},
                formatter='{"title":{{ response.headers["x-title"]|tojson }},'
                '"values":{{ response.headers.get_list("x-repeat")|tojson }}}',
            ),
            ADMIN,
        )
    )

    assert result.error is None
    assert result.formatted.value == {"title": "caf\u00e9", "values": ["one", "two"]}


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/example",
        "ftp://example.test",
        "http://user:pass@example.test",
        "{{ missing }}",
    ],
)
def test_invalid_url(monkeypatch, url):
    network = AsyncMock(side_effect=AssertionError("No request expected"))
    monkeypatch.setattr(http_node.HTTPNode, "send_request", network)
    result = asyncio.run(
        authoring.probe_http_request(HTTPProbe(request={"url": url}), ADMIN)
    )

    assert result.error
    network.assert_not_awaited()


def test_permissions(monkeypatch):
    network = AsyncMock(side_effect=AssertionError("No request expected"))
    monkeypatch.setattr(http_node.HTTPNode, "send_request", network)
    with pytest.raises(ForbiddenException):
        asyncio.run(
            authoring.probe_http_request(
                HTTPProbe(request={"url": "https://example.test"}), READER
            )
        )
    result = asyncio.run(
        tools.execute_tool(
            "probe_http_request",
            '{"request":{"url":"https://example.test"}}',
            READER,
            conversation_id=1,
            call_id="denied",
        )
    )

    assert not result["ok"]
    network.assert_not_awaited()


def test_jsonc_null():
    assert loads_jsonc("/* sample */ null") is None
    with pytest.raises(ValueError):
        loads_jsonc("invalid")


def test_curl_request(monkeypatch):
    sent = []
    closed = []
    stopped = []

    class Response:
        status_code = 201
        url = "https://example.test/created"
        headers = httpx.Headers(
            {"Content-Type": "application/json", "Set-Cookie": "private=fixture"}
        )
        quit_now = SimpleNamespace(set=lambda: stopped.append(True))
        astream_task = None

        async def aiter_content(self):
            yield b'{"id":"created"}'

        async def aclose(self):
            closed.append("response")

    class Client:
        def __init__(self, **kwargs):
            cookies = kwargs.pop("cookies")
            assert isinstance(cookies, CookieJar) and len(cookies) == 0
            assert kwargs == {
                "allow_redirects": True,
                "max_redirects": 5,
                "trust_env": False,
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            closed.append("client")

        async def request(self, *args, **kwargs):
            sent.append((args, kwargs))
            return Response()

    proxy = AsyncMock(return_value="http://proxy.example.test:8080")
    monkeypatch.setattr(http_node, "resolve_proxy", proxy)
    monkeypatch.setattr(authoring, "AsyncSession", Client)
    result = asyncio.run(
        authoring.probe_http_request(
            HTTPProbe(
                request={
                    "client": "curl_cffi",
                    "method": "POST",
                    "url": "https://example.test",
                    "headers": {
                        "User-Agent": "ignored",
                        "Content-Type": "application/json",
                    },
                    "body": '{"page": 1,}',
                }
            ),
            ADMIN,
        )
    )

    assert result.status_code == 201 and result.body_complete
    assert sent[0][1]["json"] == {"page": 1}
    assert sent[0][1]["impersonate"] == "chrome"
    assert sent[0][1]["proxy"] == "http://proxy.example.test:8080"
    assert sent[0][1]["stream"]
    assert sent[0][1]["headers"] == [("content-type", "application/json")]
    assert closed == ["response", "client"]
    assert stopped == [True]
    assert "set-cookie" not in result.headers


@pytest.mark.parametrize("action", ["limit", "cancel"])
def test_curl_stalled_stream(monkeypatch, action):
    closed = []

    async def run():
        started = asyncio.Event()
        transfer = asyncio.create_task(asyncio.Event().wait())

        class Response:
            status_code = 200
            url = "https://example.test"
            headers = httpx.Headers()
            quit_now = asyncio.Event()
            astream_task = transfer

            async def aiter_content(self):
                started.set()
                if action == "limit":
                    yield b"x" * (authoring.MAX_RESPONSE_BYTES + 1)
                await asyncio.Event().wait()

            async def aclose(self):
                try:
                    await transfer
                finally:
                    closed.append("response")

        class Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                transfer.cancel()
                await asyncio.gather(transfer, return_exceptions=True)
                closed.append("client")

            async def request(self, *args, **kwargs):
                return Response()

        monkeypatch.setattr(authoring, "AsyncSession", Client)
        monkeypatch.setattr(http_node, "resolve_proxy", AsyncMock(return_value=None))
        task = asyncio.create_task(
            authoring.probe_http_request(
                HTTPProbe(
                    request={"url": "https://example.test", "client": "curl_cffi"},
                    formatter="{}",
                ),
                ADMIN,
            )
        )
        await started.wait()
        if action == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
        else:
            result = await asyncio.wait_for(task, 0.5)
            assert result.status_code == 200 and result.error is None
            assert not result.body_complete and result.text_truncated
            assert len(result.text) == authoring.MAX_SAMPLE_CHARS
            assert not result.formatted.valid
        assert transfer.cancelled()

    asyncio.run(run())
    assert closed == ["response", "client"]


def test_assistant_parity(monkeypatch):
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda request: httpx.Response(200, json={"id": "one"})
        ),
    )

    async def run():
        data = {
            "request": {"url": "https://example.test"},
            "formatter": '{"id":{{ response.text|jsonpath_first("$.id")|tojson }}}',
        }
        direct = await authoring.probe_http_request(HTTPProbe(**data), ADMIN)
        result = await tools.execute_tool(
            "probe_http_request",
            dumps(data).decode(),
            ADMIN,
            conversation_id=1,
            call_id="probe",
        )
        assert result["ok"]
        assert result["data"] == direct.model_dump(mode="json")
        fixture = {
            "template": "{{ items|length }}",
            "context": {"items": [1, 2]},
            "mode": "expression",
        }
        result = await tools.execute_tool(
            "validate_workflow_template",
            dumps(fixture).decode(),
            READER,
            conversation_id=1,
            call_id="validation",
        )
        assert result["ok"]
        assert result["data"] == authoring.validate_workflow_template(
            TemplateValidation(**fixture)
        ).model_dump(mode="json")

    asyncio.run(run())
