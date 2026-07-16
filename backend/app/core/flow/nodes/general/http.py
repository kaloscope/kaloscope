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
    client = SelectField(
        required=True,
        options={"httpx": "httpx", "curl_cffi": "curl_cffi"},
        default="httpx",
    )
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
    async def execute(cls, *, node_data: dict[str, Any], context: Context, **kwargs):
        method = cast(HttpMethod, str(cls.method.extract(node_data)))
        url = cls.url.extract(node_data, context=context)

        # request headers
        headers = cls.headers.extract(node_data, context=context)
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
        if body := cls.body.extract(node_data, context=context):
            if (obj := try_loads(body, with_comments=True)) is not None:
                content_type = headers.get("content-type", "").lower()
                if "application/x-www-form-urlencoded" in content_type:
                    if isinstance(obj, dict):
                        form = obj
                elif "application/json" in content_type:
                    json = obj
            if json is None and form is None:
                binary = body.encode(ENCODING)

        # make the request
        app_ctx = Sanic.get_app().ctx
        try:
            if cls.client.extract(node_data) == "curl_cffi":
                # use curl_cffi client
                curl_client: AsyncSession = app_ctx.curl_cffi
                response = await curl_client.request(
                    method,
                    url,
                    headers=[
                        (key, value)
                        for key, value in headers.multi_items()
                        if key.lower() not in _IMPERSONATION_HEADERS
                    ],
                    data=cast(
                        dict[str, str] | bytes | None,
                        binary if binary is not None else form,
                    ),
                    json=cast(dict[str, Any] | list[Any] | None, json),
                    proxy=await resolve_proxy(url),
                    impersonate="chrome",
                )
            else:
                # use HTTPX client
                httpx_client: httpx.AsyncClient = app_ctx.httpx
                response = await httpx_client.request(
                    method,
                    url,
                    headers=headers,
                    content=binary,
                    data=form,
                    json=json,
                    extensions=extensions,
                )
                if response.extensions.get("hishel_from_cache"):
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
            logger.error("An error occurred while requesting %s.", url, exc_info=True)
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED) from e
