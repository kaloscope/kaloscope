from typing import Any, cast

import httpx
from curl_cffi import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.session import HttpMethod
from sanic import Sanic
from sanic.log import logger

from app.core.constants import ENCODING
from app.core.exceptions import ErrorCode, KaloscopeException
from app.core.flow.context import Context
from app.core.flow.fields import (
    CodeField,
    DividerField,
    KVPairsField,
    NumberField,
    SelectField,
    URLField,
)
from app.core.flow.nodes.base import Node, general_node
from app.core.network import resolve_proxy
from app.core.renderer import render
from app.utils.json import try_loads

_IMPERSONATION_HEADERS = {
    "user-agent",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
}


@general_node(order=1, icon="globe")
class HTTPNode(Node):
    """Send HTTP; merge formatter JSONC objects into context.

    Templated URL/headers/body; headers `[{"key":"Accept","value":"application/json"}]`.
    JSONC body + Content-Type selects JSON/form, otherwise bytes.
    Shared cookies; auth: `var('$auth').token`.
    Only formatter sees `response.text/status_code/headers`:
    `{"items": {{ response.text|jsonpath_all('$.items[*]')|tojson }}}`.
    Default stores text as `response`. Non-2xx does not auto-fail.
    """

    client = SelectField(
        required=True,
        options={"httpx": "httpx", "curl_cffi": "curl_cffi"},
        default="httpx",
        span=50,
    )
    timeout = NumberField("timeout", required=True, min=1, default=30, span=50)
    method = SelectField(
        required=True,
        options={
            "GET": "GET",
            "POST": "POST",
            "PUT": "PUT",
            "PATCH": "PATCH",
            "DELETE": "DELETE",
            "HEAD": "HEAD",
            "OPTIONS": "OPTIONS",
        },
        default="GET",
        span=20,
    )
    url = URLField(required=True, maxlength=1024, span=80)
    headers = KVPairsField(placeholder=("key", "value"))
    body = CodeField(language="jinja2", width="32rem", collapse=True)
    ___ = DividerField("response")
    formatter = CodeField(
        "formatter",
        tooltip="format_response",
        language="jinja2",
        width="32rem",
        darkmode=True,
        default='{\n  "response": {{ response.text|tojson }}\n}',
    )

    @classmethod
    def prepare_request(
        cls, node_data: dict[str, Any], context: dict, *, sandboxed: bool = False
    ) -> dict[str, Any]:
        """Render and encode a request for execution or an isolated probe.

        Args:
            node_data: HTTP node fields, with omitted fields using their defaults.
            context: Variables explicitly available to this request.
            sandboxed: Whether to restrict Python access in templates.

        Returns:
            Arguments accepted by the HTTPX client, including cache extensions.
        """
        method = cast(HttpMethod, str(cls.method.extract(node_data)))
        url = render(cls.url.extract(node_data), context, sandboxed=sandboxed)
        timeout = cls.timeout.extract(node_data)

        # request headers
        headers = render(cls.headers.extract(node_data), context, sandboxed=sandboxed)
        headers = httpx.Headers(
            [
                (k, v)
                for h in headers
                if (k := h["key"].strip()) and (v := h["value"].strip())
            ]
        )

        # extract X-Hishel-* headers into extensions
        # https://hishel.com/metadata.html
        extensions: dict[str, Any] = {}
        hishel_headers: list[str] = []
        for key, value in headers.items():
            if key.lower().startswith("x-hishel-"):
                hishel_headers.append(key)
                # convert X-Hishel-Ttl → hishel_ttl
                hishel_key = key[2:].lower().replace("-", "_")
                # convert value to appropriate type
                if value.lower() in ("true", "false"):
                    extensions[hishel_key] = value.lower() == "true"
                else:
                    try:
                        extensions[hishel_key] = float(value)
                    except ValueError:
                        extensions[hishel_key] = value
        # remove hishel headers
        for key in hishel_headers:
            del headers[key]

        # request body
        binary, form, json = None, None, None
        if body := render(cls.body.extract(node_data), context, sandboxed=sandboxed):
            if (obj := try_loads(body, with_comments=True)) is not None:
                content_type = headers.get("content-type", "").lower()
                if "application/x-www-form-urlencoded" in content_type:
                    if isinstance(obj, dict):
                        form = obj
                elif "application/json" in content_type:
                    json = obj
            if json is None and form is None:
                binary = body.encode(ENCODING)

        return {
            "method": method,
            "url": url,
            "headers": headers,
            "content": binary,
            "data": form,
            "json": json,
            "timeout": timeout,
            "extensions": extensions,
        }

    @classmethod
    async def send_request(
        cls,
        options: dict[str, Any],
        client: str,
        httpx_client: httpx.AsyncClient | None,
        curl_client: AsyncSession | None,
        *,
        stream: bool = False,
    ) -> Any:
        """Send prepared arguments through the selected workflow HTTP client.

        Args:
            options: Arguments returned by `prepare_request`.
            client: Registered client selection.
            httpx_client: HTTPX client, required when selected.
            curl_client: Curl client, required when selected.
            stream: Whether the caller will consume and close a bounded stream.

        Returns:
            Client response with the same formatter-facing attributes as execution.
        """
        if client == "curl_cffi":
            return await curl_client.request(
                options["method"],
                options["url"],
                headers=[
                    (key, value)
                    for key, value in options["headers"].multi_items()
                    if key.lower() not in _IMPERSONATION_HEADERS
                ],
                data=options["content"]
                if options["content"] is not None
                else options["data"],
                json=options["json"],
                timeout=options["timeout"],
                proxy=await resolve_proxy(options["url"]),
                impersonate="chrome",
                stream=stream,
            )
        if stream:
            return await httpx_client.send(
                httpx_client.build_request(**options), stream=True
            )
        return await httpx_client.request(**options)

    @classmethod
    async def execute(cls, *, node_data: dict[str, Any], context: Context, **kwargs):
        """Send the configured request and merge its rendered response.

        Args:
            node_data: Configured fields and mutable node state.
            context: Shared variables and storage for this flow run.
            **kwargs: Unused execution arguments supplied by the flow engine.

        Raises:
            KaloscopeException: If the HTTP request fails.
        """
        options = cls.prepare_request(node_data, context._context)
        app_ctx = Sanic.get_app().ctx
        try:
            response = await cls.send_request(
                options,
                cls.client.extract(node_data),
                getattr(app_ctx, "httpx", None),
                getattr(app_ctx, "curl_cffi", None),
            )
            if isinstance(response, httpx.Response) and response.extensions.get(
                "hishel_from_cache"
            ):
                logger.debug(
                    'HTTP Response: served from cache "%s %s"',
                    response.http_version,
                    response.status_code,
                )

            formatter = cls.formatter.extract(node_data)
            # merge the rendered response into context
            rendered = render(formatter, {**context._context, "response": response})
            if not rendered:
                return
            if isinstance((obj := try_loads(rendered, with_comments=True)), dict):
                context.update(obj)
        except (httpx.RequestError, RequestException) as e:
            logger.error(
                "An error occurred while requesting %s.", options["url"], exc_info=True
            )
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED) from e
