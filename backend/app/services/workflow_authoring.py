"""Probe explicit HTTP inputs and validate templates without running workflows."""

import asyncio
from contextlib import AsyncExitStack, suppress

import httpx
import orjson
from curl_cffi import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from jinja2 import TemplateError

from app.core.exceptions import ForbiddenException
from app.core.flow.nodes.general.http import HTTPNode
from app.core.flow.outputs import check_output
from app.core.network import NetworkTransport
from app.core.renderer import render
from app.models.user import UserInfo, UserRole
from app.models.workflow_authoring import (
    HTTPProbe,
    HTTPProbeResult,
    TemplateResult,
    TemplateValidation,
)
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
        return TemplateResult(
            valid=False,
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
            line=getattr(exc, "lineno", None),
        )


async def probe_http_request(args: HTTPProbe, user: UserInfo) -> HTTPProbeResult:
    """Send one explicitly requested HTTP probe with disposable client state.

    Args:
        args: Request fields, explicit fixtures and optional HTTP formatter.
        user: Current account; only administrators can send server-side probes.

    Returns:
        Observed status, final URL, selected headers and a bounded text sample.
        Optional extraction uses the complete bounded body before sample clipping.
        Non-2xx responses remain HTTP observations, not transport failures. Cookies
        remain local to this call; probes bypass persistent response caching.

    Raises:
        ForbiddenException: If the account cannot author workflows.
    """
    if user.role != UserRole.ADMIN:
        raise ForbiddenException
    node_data = args.request.model_dump()
    node_data["headers"] = [
        {"key": key, "value": value} for key, value in args.request.headers.items()
    ]
    try:
        options = HTTPNode.prepare_request(node_data, args.context, sandboxed=True)
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
                    )
                )
            else:
                curl_client = await stack.enter_async_context(
                    AsyncSession(allow_redirects=True, max_redirects=5, trust_env=False)
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
                    key.lower(): value[:512]
                    for key, value in response.headers.items()
                    if key.lower() in RESPONSE_HEADERS
                },
                text=sample.text[:MAX_SAMPLE_CHARS],
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
                    # only this response and explicit fixtures enter the formatter
                    validation = TemplateValidation(
                        template=args.formatter, mode="object", context=args.context
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
