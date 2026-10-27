"""Probe explicit HTTP inputs and validate templates without running workflows."""

import asyncio
import json
import re
from collections.abc import Iterable
from contextlib import AsyncExitStack, suppress
from copy import copy
from http.cookiejar import Cookie, CookieJar
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import orjson
from curl_cffi import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from jinja2 import TemplateError
from jinja2.utils import htmlsafe_json_dumps
from sanic import Sanic

from app.core.exceptions import ForbiddenException
from app.core.flow.context import AUTH_KEY
from app.core.flow.nodes.base import start_config
from app.core.flow.nodes.general.http import HTTPNode
from app.core.flow.outputs import check_auth, check_output
from app.core.network import NetworkTransport
from app.core.renderer import render
from app.models.flow import FlowGraph, GraphCategory
from app.models.user import UserInfo, UserRole
from app.models.workflow_authoring import (
    HTTPProbe,
    HTTPProbeResult,
    HTTPRequest,
    TemplateResult,
    TemplateValidation,
)
from app.services.flow import FlowGraphService
from app.utils.json import dumps, loads_jsonc

MAX_RESPONSE_BYTES = 256 * 1024
MAX_SAMPLE_CHARS = 2000
MAX_RENDER_BYTES = 8000
RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "retry-after",
    "last-modified",
    "location",
}
# shorter cookie values are preferences such as `lang=en`; redacting them corrupts text
MIN_COOKIE_SECRET_CHARS = 8
# a literal scheme and host at the start of an HTTP node URL template
LITERAL_ORIGIN = re.compile(r"\s*https?://([^/?#\s{}]+)(?=[/?#\s]|$)", re.IGNORECASE)


class ProbeCookieJar(CookieJar):
    """Hold one probe's cookies and remember every value seen for redaction."""

    def __init__(self):
        """Start an empty disposable jar."""
        super().__init__()
        # name, value and HttpOnly flag of every cookie copied, stored or received
        self.seen: set[tuple[str, str, bool]] = set()

    def set_cookie(self, cookie: Cookie):
        """Store a copied or received cookie and remember its value.

        Args:
            cookie: Cookie set by either HTTP client, including replaced values.
        """
        if cookie.value:
            # HTTPX keeps the attribute as sent; curl_cffi records `http_only`
            http_only = (
                cookie.has_nonstandard_attr("HttpOnly")
                or cookie.get_nonstandard_attr("http_only") == "True"
            )
            self.seen.add((cookie.name, cookie.value, http_only))
        super().set_cookie(cookie)

    def extract_cookies(self, response, request):
        """Remember each HTTPX response's cookies, including rejected ones.

        Args:
            response: Response adapter exposing raw headers through `info()`.
            request: Request adapter used by the cookie policy.
        """
        self.record(response.info().get_all("Set-Cookie") or [])
        super().extract_cookies(response, request)

    def record(self, headers: Iterable[str]):
        """Remember the values carried by raw `Set-Cookie` headers.

        Args:
            headers: Header values such as `sid=value; Path=/`.
        """
        for header in headers:
            pair, *attributes = header.split(";")
            name, separator, value = pair.partition("=")
            name, value = name.strip(), value.strip()
            if separator and value:
                http_only = any(
                    item.strip().lower() == "httponly" for item in attributes
                )
                self.seen.add((name, value, http_only))
                if len(value) > 2 and value[0] == value[-1] == '"':
                    self.seen.add((name, value[1:-1], http_only))

    def secrets(self, session: str | None) -> set[str]:
        """Select the seen cookie values that may authenticate a request.

        Args:
            session: Cookie name the source indexer configures as its login session.

        Returns:
            Values of that session cookie and of HttpOnly cookies at any length, plus
            every other value of at least eight characters.
        """
        return {
            value
            for name, value, http_only in self.seen
            if http_only or name == session or len(value) >= MIN_COOKIE_SECRET_CHARS
        }


def validate_workflow_template(
    args: TemplateValidation, *, response: httpx.Response | None = None
) -> TemplateResult:
    """Evaluate explicit fixtures using the workflow renderer's selected semantics.

    Args:
        args: Template, supplied context, output form and undefined-value policy.
            Business modes check end-node output against supplied fixtures only;
            `auth` also checks the supplied `auth_start` configuration.
        response: Detached probe response for HTTP formatter evaluation only.
            The probe redacts formatter errors before clipping their messages.
            Without it, a `response` fixture object with `text` (and optional
            `url`, `status_code`, `headers`) becomes a detached response, so
            formatters using `response.url.join(...)` check supplied page samples.

    Returns:
        Bounded JSON-compatible result or evaluation error. No workflow, script,
        persistent cache, global variable or account cookie is loaded or changed.
        Explicit filesystem tests retain their normal path checks. This does not
        validate an entire workflow or prove a remote endpoint works.
    """
    probed = response is not None
    try:
        if response is None:
            response = _sample_response(args.context.get("response"))
        rendered = render(
            args.template,
            {**args.context, "response": response}
            if response is not None
            else args.context,
            raw=args.mode == "expression",
            strict=args.strict,
            sandboxed=True,
        )
        value = (
            loads_jsonc(rendered)
            if args.mode not in {"text", "expression"}
            else rendered
        )
        if args.mode == "object" and not isinstance(value, dict):
            return TemplateResult(valid=False, error="Expected a JSON object.")
        if args.mode == "array" and not isinstance(value, list):
            return TemplateResult(valid=False, error="Expected a JSON array.")
        business = args.mode in {"search", "details", "movie", "auth"}
        issues, warnings, outcome = [], [], None
        if args.mode == "auth":
            # the input model requires config for this mode
            issues, warnings, outcome = check_auth(value, args.config or "")
        elif business:
            issues, warnings = check_output(args.mode, value, args.context)
        result = TemplateResult(
            valid=not issues,
            rendered=None if business else rendered,
            value=value,
            value_type=type(value).__name__,
            issues=issues,
            warnings=warnings,
            outcome=outcome,
            error="Correct the reported output fields and validate again."
            if issues
            else None,
        )
        try:
            encoded = orjson.dumps(result.model_dump())
        except TypeError:
            return TemplateResult(
                valid=False,
                value_type=type(value).__name__,
                error="Result is not JSON-serializable. Select XPath text/attributes "
                "or materialize iterators with |list.",
            )
        if len(encoded) > MAX_RENDER_BYTES:
            return TemplateResult(
                valid=False, error="Rendered result exceeds 8000 bytes."
            )
        return result
    except Exception as exc:
        message = str(exc)
        return TemplateResult(
            valid=False,
            error=f"{type(exc).__name__}: " + (message if probed else message[:300]),
            line=getattr(exc, "lineno", None),
        )


def _sample_response(fixture: Any) -> httpx.Response | None:
    """Build a detached HTTP response from a supplied page sample.

    Args:
        fixture: Template fixture value under `response`.

    Returns:
        Response with the sample's text, status, headers and page URL, or `None`
        when the fixture is not a page sample. Text is already decoded; headers
        remain available without decompressing or decoding it again.
    """
    if not isinstance(fixture, dict) or not isinstance(fixture.get("text"), str):
        return None
    sample = httpx.Response(
        fixture.get("status_code", 200),
        text=fixture["text"],
        request=httpx.Request("GET", fixture.get("url") or "https://sample.invalid/"),
    )
    # text fixtures are unicode, not the original compressed or encoded bytes
    sample.encoding = "utf-8"
    sample.headers = httpx.Headers(fixture.get("headers") or {})
    return sample


async def probe_http_request(args: HTTPProbe, user: UserInfo) -> HTTPProbeResult:
    """Probe explicit inputs using optional saved indexer authentication.

    Args:
        args: Request fields, fixtures, formatter and optional source indexer.
        user: Current account; only administrators can send server-side probes.

    Returns:
        Observed status, final URL, selected headers and a bounded text sample.
        Optional extraction uses the complete bounded body before sample clipping.
        Non-2xx responses remain HTTP observations, not transport failures. An
        explicit indexer supplies a snapshot of the cookie jar used by HTTP nodes,
        where normal cookie domain/path rules still apply, and its unexpired
        `$auth` only when the prepared request targets a host written in its
        published HTTP node URLs; such requests do not follow redirects. A request
        reading `$auth` for another host is refused before sending. Response
        cookies remain local and persistent caching is bypassed. Saved
        authentication strings, the indexer's configured session cookie and HttpOnly
        cookies at any length, and other saved or received cookie values of at
        least eight characters are redacted, with or without an indexer.

    Raises:
        ForbiddenException: If the account cannot author or access the workflow.
        NotFoundException: If the selected workflow no longer exists.
    """
    if user.role != UserRole.ADMIN:
        raise ForbiddenException
    context = args.context
    cookies = ProbeCookieJar()
    auth = session = auth_hosts = None
    if args.workflow_id is not None:
        graph = await FlowGraphService.get_accessible(args.workflow_id, user)
        if graph.category != GraphCategory.INDEXER:
            return HTTPProbeResult(error="Select an indexer to reuse its login state.")
        saved_cookies = getattr(Sanic.get_app().ctx, "cookies", None)
        auth = await FlowGraphService.indexer_auth(graph.id)
        config = (await start_config(graph, "auth"))["auth"] or {}
        if (
            (config.get("login") or {}).get("required")
            and auth is None
            and await FlowGraphService.indexer_auth(graph.id, saved_cookies) is None
        ):
            return HTTPProbeResult(
                error="Indexer login is missing or expired. "
                "Log in on its search page, then retry."
            )
        if saved_cookies is not None:
            for cookie in saved_cookies:
                if not cookie.is_expired():
                    cookies.set_cookie(copy(cookie))
        if isinstance(session_cookie := config.get("cookie"), dict):
            session = session_cookie.get("name")
        # boot parameters also override stored variables during workflow execution
        if auth is not None and AUTH_KEY not in context:
            context = {AUTH_KEY: auth, **context}
            auth_hosts = _request_hosts(graph)

    result = await _probe_http_request(args, context, cookies, auth_hosts=auth_hosts)
    secrets = cookies.secrets(session)

    def collect(value):
        """Collect stored authentication strings for result redaction.

        Args:
            value: Authentication field, including nested objects and arrays.
        """
        if isinstance(value, str) and value:
            secrets.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect({key: value for key, value in (auth or {}).items() if key != "name"})
    secrets |= (
        {quote(value, safe="") for value in secrets}
        | {
            json.dumps(value, ensure_ascii=ascii_only)[1:-1]
            for value in secrets
            for ascii_only in (False, True)
        }
        | {
            str(htmlsafe_json_dumps(value, ensure_ascii=ascii_only))[1:-1]
            for value in secrets
            for ascii_only in (False, True)
        }
    )
    return _redact_probe_result(result, secrets)


def _request_hosts(graph: FlowGraph) -> set[str]:
    """Collect hosts that the indexer's published HTTP nodes write literally.

    Args:
        graph: Source indexer whose saved login may be borrowed.

    Returns:
        Normalized hosts from URL templates starting with a literal HTTP(S) origin.
        URLs whose host is templated are skipped, so their login is never borrowed.
    """
    hosts = set()
    nodes = (graph.definition or {}).get("nodes")
    for node in nodes if isinstance(nodes, list) else []:
        data = node.get("data") if isinstance(node, dict) else None
        if not isinstance(data, dict) or data.get("$schema") != "http":
            continue
        url = data.get("url")
        if isinstance(url, str) and (match := LITERAL_ORIGIN.match(url)):
            with suppress(httpx.InvalidURL):
                hosts.add(httpx.URL(f"http://{match[1]}").host)
    return hosts


def _reads_auth(request: HTTPRequest) -> bool:
    """Check whether a probe's request templates refer to saved login.

    Args:
        request: URL, header and body templates that will be sent.

    Returns:
        Whether any of them names `$auth`.
    """
    return any(
        AUTH_KEY in text
        for text in (
            request.url,
            request.body,
            *request.headers,
            *request.headers.values(),
        )
    )


def _redact_probe_result(result: HTTPProbeResult, secrets: set[str]) -> HTTPProbeResult:
    """Hide known saved credentials before clipping returned HTTP observations.

    Args:
        result: Probe observation, including optional formatter output.
        secrets: Stored or received cookie values and authentication strings.

    Returns:
        Bounded observation with literal and encoded credential values replaced.
        Redaction does not claim to recognize arbitrary transformed credentials.
    """
    pattern = (
        re.compile(
            "|".join(
                re.escape(value) for value in sorted(secrets, key=len, reverse=True)
            )
        )
        if secrets
        else None
    )

    def redact(value):
        """Replace credential strings inside a JSON-compatible value.

        Args:
            value: Text, object, array or scalar returned by the probe.

        Returns:
            Value with known credential strings replaced.
        """
        if pattern is None:
            return value
        if isinstance(value, str):
            redacted = pattern.sub("[redacted]", value)
            result.redacted |= redacted != value
            return redacted
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        return value

    result.url = redact(result.url)
    result.text = redact(result.text)
    if len(result.text) > MAX_SAMPLE_CHARS:
        result.text = result.text[:MAX_SAMPLE_CHARS]
        result.text_truncated = True
    result.headers = {key: value[:512] for key, value in redact(result.headers).items()}
    result.error = redact(result.error)
    if result.formatted is not None:
        result.formatted.rendered = redact(result.formatted.rendered)
        result.formatted.value = redact(result.formatted.value)
        result.formatted.error = redact(result.formatted.error)
        if result.formatted.error is not None:
            result.formatted.error = result.formatted.error[:300]
        if len(dumps(result.formatted.model_dump())) > MAX_RENDER_BYTES:
            result.formatted = TemplateResult(
                valid=False, error="Redacted extraction exceeds 8000 bytes."
            )
    return result


def _body_size(options: dict, client: str) -> int:
    """Measure a prepared request body as the selected client encodes it.

    Args:
        options: Arguments returned by `HTTPNode.prepare_request`.
        client: Selected workflow HTTP client.

    Returns:
        Size in bytes of the raw, JSON or form-encoded body sent on the wire.
    """
    if options["content"] is not None:
        return len(options["content"])
    if options["json"] is not None:
        # curl_cffi escapes non-ASCII JSON while HTTPX sends UTF-8
        return len(
            json.dumps(
                options["json"],
                separators=(",", ":"),
                ensure_ascii=client == "curl_cffi",
            ).encode()
        )
    if options["data"] is not None:
        if client == "curl_cffi":
            return len(urlencode(options["data"]))
        return len(
            httpx.Request("POST", "http://body.invalid", data=options["data"]).read()
        )
    return 0


async def _probe_http_request(
    args: HTTPProbe,
    context: dict,
    cookies: ProbeCookieJar,
    *,
    auth_hosts: set[str] | None = None,
) -> HTTPProbeResult:
    """Send a bounded request without modifying the source authentication.

    Args:
        args: Explicit request and optional formatter.
        context: Request fixtures merged with any selected authentication.
        cookies: Disposable cookie snapshot, updated only within this call; it also
            records every received cookie value for redaction.
        auth_hosts: Hosts that may receive the borrowed `$auth` in `context`, or
            `None` when no saved login is borrowed. The request is prepared once and
            that exact request is checked and sent; with the login it follows no
            redirects, so a 3xx response and its `location` are returned instead.

    Returns:
        HTTP observation using the bounded body before redaction and sample clipping,
        or a bounded request/formatter error.
    """
    node_data = args.request.model_dump()
    node_data["headers"] = [
        {"key": key, "value": value} for key, value in args.request.headers.items()
    ]
    try:
        options = HTTPNode.prepare_request(node_data, context, sandboxed=True)
        if auth_hosts is not None and httpx.URL(options["url"]).host not in auth_hosts:
            if _reads_auth(args.request):
                return HTTPProbeResult(
                    error=f"Workflow {args.workflow_id}'s saved login is only sent "
                    "to hosts in its HTTP node URLs: "
                    + (", ".join(sorted(auth_hosts)[:5]) or "none written literally")
                    + ". Remove $auth or omit workflow_id for other hosts."
                )
            # nothing names the borrowed login, so send this host a request without it
            context = {key: value for key, value in context.items() if key != AUTH_KEY}
            options = HTTPNode.prepare_request(node_data, context, sandboxed=True)
            auth_hosts = None
        # a redirect could carry the borrowed login to another host
        follow = auth_hosts is None
        url = httpx.URL(options["url"])
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            return HTTPProbeResult(
                error="Use an HTTP(S) URL without embedded credentials."
            )
        size = len(str(url).encode()) + sum(
            len(key) + len(value) for key, value in options["headers"].raw
        )
        size += _body_size(options, args.request.client)
        if size > 65536:
            return HTTPProbeResult(error="Rendered request exceeds 64 KiB.")
        async with asyncio.timeout(args.request.timeout), AsyncExitStack() as stack:
            http_client = curl_client = None
            if args.request.client == "httpx":
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=NetworkTransport(http2=True),
                        follow_redirects=follow,
                        max_redirects=5,
                        cookies=cookies,
                    )
                )
            else:
                curl_client = await stack.enter_async_context(
                    AsyncSession(
                        allow_redirects=follow,
                        max_redirects=5,
                        trust_env=False,
                        cookies=cookies,
                    )
                )
            response = await HTTPNode.send_request(
                options, args.request.client, http_client, curl_client, stream=True
            )
            body = bytearray()
            count = 0
            complete = True
            try:
                chunks = (
                    response.aiter_bytes()
                    if args.request.client == "httpx"
                    else response.aiter_content()
                )
                async for chunk in chunks:
                    count += len(chunk)
                    body.extend(chunk[: max(0, MAX_RESPONSE_BYTES - len(body))])
                    if count > MAX_RESPONSE_BYTES:
                        complete = False
                        break
            finally:
                if args.request.client == "curl_cffi":
                    if response.quit_now is not None:
                        response.quit_now.set()
                    # aclose only waits; cancel transfers that may receive no more bytes
                    if response.astream_task is not None:
                        response.astream_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await response.aclose()
                else:
                    await response.aclose()

            # the stream is already decompressed; attach headers after loading bytes
            sample = httpx.Response(
                response.status_code,
                content=bytes(body),
                request=httpx.Request(options["method"], str(response.url)),
            )
            sample.headers = httpx.Headers(response.headers.raw)
            # the formatter can read final headers, including cookies the jar rejected
            cookies.record(sample.headers.get_list("set-cookie"))
            result = HTTPProbeResult(
                status_code=response.status_code,
                url=str(response.url),
                headers={
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in RESPONSE_HEADERS
                },
                text=sample.text,
                bytes_read=count,
                body_complete=complete,
                text_truncated=not complete or len(sample.text) > MAX_SAMPLE_CHARS,
            )
            if args.formatter is not None:
                if not complete:
                    result.formatted = TemplateResult(
                        valid=False,
                        error="Response incomplete; extraction was not evaluated.",
                    )
                else:
                    validation = TemplateValidation(
                        template=args.formatter, mode="object", context=context
                    )
                    result.formatted = validate_workflow_template(
                        validation, response=sample
                    )
            return result
    except (TimeoutError, httpx.TimeoutException):
        return HTTPProbeResult(error="Request timed out; no retry was sent.")
    except (httpx.RequestError, RequestException):
        return HTTPProbeResult(error="HTTP transport failed; no retry was sent.")
    except (TemplateError, ValueError, TypeError) as exc:
        return HTTPProbeResult(error=f"Invalid request: {type(exc).__name__}.")
