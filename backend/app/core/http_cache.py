import hashlib
import math
from contextlib import suppress
from typing import Any

import httpx
from hishel.httpx import AsyncCacheConfig

from app.core.network import NetworkTransport


def workflow_cache_extensions(headers: httpx.Headers) -> dict[str, Any]:
    """Remove workflow cache headers and translate them into HTTPX extensions.

    Args:
        headers: Rendered request headers, modified in place.

    Returns:
        Cache settings for this request. An invalid lifetime is ignored.
    """
    controls = {
        key: headers.pop(key) for key in list(headers) if key.startswith("x-hishel-")
    }
    enabled = {"true", "1", "yes", "on"}
    caching: AsyncCacheConfig = {}
    extensions: dict[str, Any] = {}
    if controls.get("x-hishel-spec-ignore", "").lower() in enabled:
        caching["rfc"] = False
    if controls.get("x-hishel-body-key", "").lower() in enabled:
        extensions["workflow_body_key"] = True
    if "x-hishel-ttl" in controls:
        with suppress(ValueError):
            ttl = float(controls["x-hishel-ttl"])
            if math.isfinite(ttl):
                ttl = max(0.0, ttl)
                extensions["workflow_max_age"] = round(ttl)
                if (
                    controls.get("x-hishel-refresh-ttl-on-access", "").lower()
                    in enabled
                ):
                    caching["tti"] = ttl
                else:
                    caching["ttl"] = ttl
    if caching:
        extensions["caching"] = caching
    return extensions


async def configure_request_cache(request: httpx.Request) -> None:
    """Apply cache headers from shared-client callers and resolve body keys per hop.

    Args:
        request: HTTPX request with its final URL and encoded body.
    """
    controls = workflow_cache_extensions(request.headers)
    caching = {**controls.pop("caching", {}), **request.extensions.get("caching", {})}
    for name, value in controls.items():
        request.extensions.setdefault(name, value)
    if request.extensions.get("workflow_body_key"):
        scope = request.method if caching.get("rfc") is False else "RFC"
        digest = hashlib.sha256(f"{scope} {request.url}\0".encode())
        digest.update(await request.aread())
        caching["key"] = digest.hexdigest()
    if caching:
        request.extensions["caching"] = caching


class CacheOriginTransport(NetworkTransport):
    """Apply workflow freshness overrides before Hishel evaluates origin responses."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Send a routed request and apply any explicit workflow freshness lifetime.

        Args:
            request: Request from the cache transport.

        Returns:
            Origin response with the workflow's optional `max-age` override.
        """
        response = await super().handle_async_request(request)
        if (max_age := request.extensions.get("workflow_max_age")) is not None:
            response.headers["cache-control"] = f"max-age={max_age}"
        return response
