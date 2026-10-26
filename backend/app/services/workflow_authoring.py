"""Probe explicit HTTP inputs and validate templates without running workflows."""

import asyncio
import json
import re
from contextlib import AsyncExitStack, suppress
from copy import copy
from http.cookiejar import CookieJar
from urllib.parse import quote

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
from app.core.flow.outputs import check_output
from app.core.network import NetworkTransport
from app.core.renderer import render
from app.models.flow import GraphCategory
from app.models.user import UserInfo, UserRole
from app.models.workflow_authoring import (
    HTTPProbe,
    HTTPProbeResult,
    TemplateResult,
    TemplateValidation,
)
from app.services.flow import FlowGraphService
from app.utils.json import dumps, loads_jsonc

MAX_RESPONSE_BYTES = 256 * 1024
MAX_SAMPLE_CHARS = 2000
MAX_RENDER_BYTES = 8000
RESPONSE_HEADERS = {"content-type", "content-length", "retry-after", "last-modified"}


def validate_workflow_template(
    args: TemplateValidation, *, response: httpx.Response | None = None
) -> TemplateResult:
    """Evaluate explicit fixtures using the workflow renderer's selected semantics.

    Args:
        args: Template, supplied context, output form and undefined-value policy.
            Business modes check end-node output against supplied fixtures only.
        response: Detached probe response for HTTP formatter evaluation only.
            The probe redacts formatter errors before clipping their messages.

    Returns:
        Bounded JSON-compatible result or evaluation error. No workflow, script,
        persistent cache, global variable or account cookie is loaded or changed.
        Explicit filesystem tests retain their normal path checks. This does not
        validate an entire workflow or prove a remote endpoint works.
    """
    try:
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
        business = args.mode in {"search", "details", "movie"}
        issues, warnings = (
            check_output(args.mode, value, args.context) if business else ([], [])
        )
        result = TemplateResult(
            valid=not issues,
            rendered=None if business else rendered,
            value=value,
            value_type=type(value).__name__,
            issues=issues,
            warnings=warnings,
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
            error=f"{type(exc).__name__}: "
            + (message if response is not None else message[:300]),
            line=getattr(exc, "lineno", None),
        )


async def probe_http_request(args: HTTPProbe, user: UserInfo) -> HTTPProbeResult:
    """Probe explicit inputs using optional saved indexer authentication.

    Args:
        args: Request fields, fixtures, formatter and optional source indexer.
        user: Current account; only administrators can send server-side probes.

    Returns:
        Observed status, final URL, selected headers and a bounded text sample.
        Optional extraction uses the complete bounded body before sample clipping.
        Non-2xx responses remain HTTP observations, not transport failures. An
        explicit indexer reuses its unexpired `$auth` and a snapshot of the cookie
        jar used by HTTP nodes. Normal cookie domain/path rules still apply.
        Response cookies remain local and persistent caching is bypassed. Known
        authentication values are redacted from the returned observation.

    Raises:
        ForbiddenException: If the account cannot author or access the workflow.
        NotFoundException: If the selected workflow no longer exists.
    """
    if user.role != UserRole.ADMIN:
        raise ForbiddenException
    context = args.context
    cookies = CookieJar()
    auth = None
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
        if auth is not None:
            # boot parameters also override stored variables during workflow execution
            context = {AUTH_KEY: auth, **context}

    secrets = {cookie.value for cookie in cookies if cookie.value}
    result = await _probe_http_request(args, context, cookies)
    if args.workflow_id is not None:
        secrets.update(cookie.value for cookie in cookies if cookie.value)

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


async def _probe_http_request(
    args: HTTPProbe, context: dict, cookies: CookieJar
) -> HTTPProbeResult:
    """Send a bounded request without modifying the source authentication.

    Args:
        args: Explicit request and optional formatter.
        context: Request fixtures merged with any selected authentication.
        cookies: Disposable cookie snapshot, updated only within this call.

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
        url = httpx.URL(options["url"])
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            return HTTPProbeResult(
                error="Use an HTTP(S) URL without embedded credentials."
            )
        size = len(str(url).encode()) + sum(
            len(key) + len(value) for key, value in options["headers"].raw
        )
        size += len(options["content"] or dumps([options["data"], options["json"]]))
        if size > 65536:
            return HTTPProbeResult(error="Rendered request exceeds 64 KiB.")
        async with asyncio.timeout(args.request.timeout), AsyncExitStack() as stack:
            http_client = curl_client = None
            if args.request.client == "httpx":
                http_client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=NetworkTransport(http2=True),
                        follow_redirects=True,
                        max_redirects=5,
                        cookies=cookies,
                    )
                )
            else:
                curl_client = await stack.enter_async_context(
                    AsyncSession(
                        allow_redirects=True,
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
