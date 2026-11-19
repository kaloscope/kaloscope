"""Remote resource proxy utilities."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from pydantic import BaseModel, Field
from sanic import Request

from app.core.exceptions import ForbiddenException
from app.core.network import PUBLIC_ONLY, public_addresses
from app.models.user import UserRole

SENSITIVE_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "proxy",
    "proxy-authorization",
}

PROXY_RESPONSE_HEADERS = [
    "accept-ranges",
    "cache-control",
    "content-encoding",
    "content-disposition",
    "content-length",
    "content-range",
    "content-type",
    "etag",
    "expires",
    "last-modified",
]


class RemoteProxy(BaseModel):
    """Request model for proxying a remote resource."""

    url: str = Field(min_length=1)
    store: bool = False
    referer: str | None = None
    ua: str | None = None


def remote_proxy_request(
    url: str,
    referer: str | None,
    user_agent: str | None = None,
    request_headers: Mapping[str, str] | None = None,
) -> tuple[str, dict[str, str]]:
    """Build upstream URL and headers for a remote proxy request.

    Args:
        url: User-provided remote resource URL.
        referer: Optional referrer header override for the upstream request.
        user_agent: Optional user agent override for the upstream request. `None` uses
            the default user agent.
        request_headers: Incoming request headers to selectively forward. `None`
            forwards no incoming headers.

    Returns:
        Sanitized upstream URL and request headers.

    Raises:
        ForbiddenException: If the URL is not an absolute HTTP(S) URL.
    """
    parsed = urlparse(url)
    query_params: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        # strip app-only proxy params before forwarding the upstream request
        if key == "proxy":
            continue
        if key == "referer":
            referer = referer or value or None
            continue
        query_params.append((key, value))

    url = urlunparse(parsed._replace(query=urlencode(query_params)))
    parsed = urlparse(url)
    # only proxy absolute http urls to avoid local file or internal path access
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ForbiddenException

    # forward browser playback headers while dropping credentials and proxy-only headers
    dropped = SENSITIVE_REQUEST_HEADERS | {"host", "referer"}
    if user_agent:
        dropped |= {"user-agent"}
    headers = {
        str(key): str(value)
        for key, value in (request_headers or {}).items()
        if str(key).lower() not in dropped
    }
    headers["Host"] = parsed.netloc
    headers["Referer"] = referer or url
    if user_agent:
        headers["User-Agent"] = user_agent
    return url, headers


async def check_proxy_target(url: httpx.URL) -> None:
    """Reject a proxy hop whose host resolves here to a non-public address.

    This early check answers with a clean `403` and also covers cached responses,
    which never reach the transport. A host that does not resolve here passes,
    because the transport may resolve it through secure DNS rules; the transport
    then checks the address it actually dials.

    Args:
        url: Upstream URL of the first request or a redirect.

    Raises:
        ForbiddenException: If any address of the host is not public.
    """
    port = url.port or (443 if url.scheme == "https" else 80)
    with suppress(OSError):
        await public_addresses(url.raw_host.decode("ascii"), port)


async def proxy_policy(request: Request, url: str) -> bool:
    """Decide whether a proxy request may reach only public addresses.

    Administrators keep unrestricted access, as their workflows already fetch any
    address. Every other account, or a request without one, is limited to public
    addresses so that it cannot read loopback, LAN, or cloud metadata services.

    Args:
        request: Incoming proxy request, with `ctx.user` set by authentication.
        url: Upstream URL returned by `remote_proxy_request`.

    Returns:
        Whether `proxy_stream` must check every hop.

    Raises:
        ForbiddenException: If a restricted caller targets a non-public host or a
            URL that the HTTP client cannot parse.
    """
    user = getattr(request.ctx, "user", None)
    if getattr(user, "role", None) == UserRole.ADMIN:
        return False
    try:
        target = httpx.URL(url)
    except httpx.InvalidURL as e:
        raise ForbiddenException from e
    await check_proxy_target(target)
    return True


@asynccontextmanager
async def proxy_stream(
    client: httpx.AsyncClient, url: str, headers: Mapping[str, str], public: bool
) -> AsyncIterator[httpx.Response]:
    """Stream an upstream `GET`, checking each redirect hop for restricted callers.

    Restricted requests carry the `PUBLIC_ONLY` extension, so the shared transport
    dials only public addresses, and their redirects are followed one hop at a time
    so that each new host is checked before it is requested.

    Args:
        client: Shared application HTTP client.
        url: Upstream URL returned by `remote_proxy_request`.
        headers: Upstream headers returned by `remote_proxy_request`.
        public: Result of `proxy_policy` for this request.

    Yields:
        Final upstream response with an unread body.

    Raises:
        ForbiddenException: If a restricted request is redirected to a non-public
            host.
        httpx.TooManyRedirects: If redirects exceed the client's limit.
    """
    if not public:
        async with client.stream("GET", url, headers=headers) as response:
            yield response
        return

    # workflow cache controls must not steer a restricted request into shared entries
    headers = {
        k: v for k, v in headers.items() if not k.lower().startswith("x-hishel-")
    }
    request = client.build_request(
        "GET", url, headers=headers, extensions={PUBLIC_ONLY: True}
    )
    redirects = 0
    while True:
        response = await client.send(request, stream=True, follow_redirects=False)
        if (next_request := response.next_request) is None:
            break
        await response.aclose()
        redirects += 1
        if redirects > client.max_redirects:
            raise httpx.TooManyRedirects(
                "Exceeded maximum allowed redirects.", request=next_request
            )
        await check_proxy_target(next_request.url)
        request = next_request
    try:
        yield response
    finally:
        await response.aclose()
