"""Unit tests for the remote media and image proxy target policy."""

import asyncio
import socket
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlencode

import httpx
import pytest
from sanic import HTTPResponse, Request, Sanic
from sanic.compat import Header
from sanic.models.protocol_types import TransportProtocol
from sanic.response import ResponseStream

from app.core import network
from app.core.config import KaloscopeConfig
from app.core.exceptions import ForbiddenException, KaloscopeException
from app.models.user import UserRole
from app.routes import image, media

APP_NAME = "proxy-route-test"
ADMIN = SimpleNamespace(role=UserRole.ADMIN)
READER = SimpleNamespace(role=UserRole.USER)
ROUTES = {
    "media": (media.proxy_remote_media, "/_api/media/proxy"),
    "image": (image.proxy_remote_image, "/_api/image/proxy"),
}
HOSTS = {
    "localhost": ["::1", "127.0.0.1"],
    "cdn.test": ["93.184.215.14"],
    "mirror.test": ["2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "router.test": ["192.168.1.1"],
    "mixed.test": ["93.184.215.14", "10.0.0.5"],
    "fakeip.test": ["198.18.0.7"],
}


@pytest.fixture(autouse=True)
def application():
    app = Sanic(APP_NAME, configure_logging=False)
    try:
        yield app
    finally:
        Sanic.unregister_app(app)


@pytest.fixture(autouse=True)
def resolver(monkeypatch):
    async def resolve(host: str, port: int) -> list[str]:
        if host in HOSTS:
            return HOSTS[host]
        # numeric forms such as `2130706433` resolve without a DNS query
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
        )
        return [str(info[4][0]) for info in infos]

    monkeypatch.setattr(network, "resolve", resolve, raising=False)


@pytest.fixture(autouse=True)
def images(tmp_path, monkeypatch):
    directory = tmp_path / "images"
    directory.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))
    return directory


def request(path: str, user, headers: dict[str, str], **query: object) -> Request:
    """Create a real Sanic request for a proxy route.

    Args:
        path: Route path without the query string.
        user: Session account, or `None` for a request without one.
        headers: Incoming request headers.
        **query: Query parameters for `RemoteProxy`.

    Returns:
        Request suitable for the validation decorator.
    """
    result = Request(
        url_bytes=f"{path}?{urlencode(query)}".encode(),
        headers=Header(headers),
        version="1.1",
        method="GET",
        transport=cast(TransportProtocol, None),
        app=Sanic.get_app(APP_NAME),
    )
    if user is not None:
        result.ctx.user = user
    return result


async def proxy(
    route: str, handler, user=READER, headers: dict[str, str] | None = None, **query
) -> tuple[int, bytes]:
    """Call a proxy route against a fake upstream and drain its stream.

    Args:
        route: Key of `ROUTES`.
        handler: Mock transport handler for upstream requests.
        user: Session account; `None` sends no account.
        headers: Incoming request headers.
        **query: Query parameters for `RemoteProxy`.

    Returns:
        Upstream status and the bytes served.
    """
    callback, path = ROUTES[route]
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        Sanic.get_app(APP_NAME).ctx.httpx = client
        response = await cast(Callable[[Request], Awaitable[HTTPResponse]], callback)(
            request(path, user, headers or {}, **query)
        )
        assert isinstance(response, ResponseStream)
        chunks: list[bytes] = []

        async def write(chunk: bytes):
            chunks.append(chunk)

        stream = SimpleNamespace(
            response=SimpleNamespace(status=0, headers={}), write=write
        )
        await response.streaming_fn(cast(ResponseStream, stream))
        return stream.response.status, b"".join(chunks)


class Upstream:
    """Serve fake upstream responses and record every request."""

    def __init__(self, redirects: dict[str, str] | None = None):
        """Keep the redirects to answer.

        Args:
            redirects: `Location` values by requested URL.
        """
        self.redirects = redirects or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer one upstream request.

        Args:
            request: Request sent by the proxy.

        Returns:
            A redirect for a configured URL, otherwise the URL as the body.
        """
        self.requests.append(request)
        if location := self.redirects.get(str(request.url)):
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, stream=httpx.ByteStream(str(request.url).encode()))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://localhost:9000/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://2130706433/",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.168.1.1/cgi-bin/luci",
        "http://100.100.100.100/",
        "http://router.test/",
        "http://mixed.test/a.png",
        "http://☃.test/",
    ],
)
def test_rejected(route, url):
    upstream = Upstream()

    with pytest.raises(ForbiddenException):
        asyncio.run(proxy(route, upstream, url=url))

    assert upstream.requests == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("user", [READER, None])
def test_anonymous_rejected(route, user):
    upstream = Upstream()

    with pytest.raises(ForbiddenException):
        asyncio.run(proxy(route, upstream, user=user, url="http://10.0.0.1/"))

    assert upstream.requests == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.test/covers/a.png",
        "https://mirror.test/v/seg-1.ts",
        "https://fakeip.test/a.png",
        "https://93.184.215.14/a.png",
        "https://unresolved.test/a.png",
    ],
)
def test_allowed(route, url):
    upstream = Upstream()

    status, body = asyncio.run(proxy(route, upstream, url=url))

    assert (status, body) == (200, url.encode())
    assert [str(r.url) for r in upstream.requests] == [url]
    assert all(r.extensions.get(network.PUBLIC_ONLY) for r in upstream.requests)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://router.test/",
    ],
)
def test_redirect_rejected(route, location):
    upstream = Upstream({"https://cdn.test/a.png": location})

    with pytest.raises(ForbiddenException):
        asyncio.run(proxy(route, upstream, url="https://cdn.test/a.png"))

    assert [str(r.url) for r in upstream.requests] == ["https://cdn.test/a.png"]


@pytest.mark.parametrize("route", ROUTES)
def test_redirect_allowed(route):
    upstream = Upstream(
        {
            "https://cdn.test/a.png": "/b.png",
            "https://cdn.test/b.png": "https://mirror.test/c.png",
        }
    )

    status, body = asyncio.run(proxy(route, upstream, url="https://cdn.test/a.png"))

    assert (status, body) == (200, b"https://mirror.test/c.png")
    assert [str(r.url) for r in upstream.requests] == [
        "https://cdn.test/a.png",
        "https://cdn.test/b.png",
        "https://mirror.test/c.png",
    ]
    assert all(r.extensions.get(network.PUBLIC_ONLY) for r in upstream.requests)
    assert upstream.requests[-1].headers["host"] == "mirror.test"


@pytest.mark.parametrize("route", ROUTES)
def test_redirect_loop(route):
    upstream = Upstream({"https://cdn.test/a.png": "https://cdn.test/a.png"})

    with pytest.raises(KaloscopeException) as exc_info:
        asyncio.run(proxy(route, upstream, url="https://cdn.test/a.png"))

    assert isinstance(exc_info.value.__cause__, httpx.TooManyRedirects)
    assert len(upstream.requests) == 21


@pytest.mark.parametrize("route", ROUTES)
def test_admin(route):
    upstream = Upstream({"https://cdn.test/a.png": "http://127.0.0.1:8080/admin"})

    status, body = asyncio.run(
        proxy(route, upstream, user=ADMIN, url="https://cdn.test/a.png")
    )

    assert (status, body) == (200, b"http://127.0.0.1:8080/admin")
    assert len(upstream.requests) == 2


@pytest.mark.parametrize("route", ROUTES)
def test_cache_headers(route):
    upstream = Upstream()
    headers = {
        "x-hishel-ttl": "86400",
        "x-hishel-spec-ignore": "true",
        "range": "bytes=0-",
    }

    asyncio.run(proxy(route, upstream, headers=headers, url="https://cdn.test/a"))

    (sent,) = upstream.requests
    assert "x-hishel-ttl" not in sent.headers
    assert "x-hishel-spec-ignore" not in sent.headers
    assert sent.headers["range"] == "bytes=0-"
