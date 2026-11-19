"""Unit tests for cached and proxied image routes."""

import asyncio
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

from app.core.config import KaloscopeConfig
from app.models.user import UserRole
from app.routes import image as routes

APP_NAME = "image-route-test"


@pytest.fixture(autouse=True)
def application():
    app = Sanic(APP_NAME, configure_logging=False)
    try:
        yield app
    finally:
        Sanic.unregister_app(app)


@pytest.fixture
def images(tmp_path, monkeypatch):
    directory = tmp_path / "workspace" / "images"
    (directory / "icons").mkdir(parents=True)
    (directory / "icons" / "logo.png").write_bytes(b"logo")
    (tmp_path / "workspace" / "secret.png").write_bytes(b"secret")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))
    return directory


def request(**query: object) -> Request:
    """Create a real Sanic request for the image proxy route.

    The caller is an administrator, so the public-address policy covered in
    `tests/test_routes_proxy.py` performs no DNS lookup here.

    Args:
        **query: Query parameters for `RemoteProxy`.

    Returns:
        Request suitable for the validation decorator.
    """
    result = Request(
        url_bytes=f"/_api/image/proxy?{urlencode(query)}".encode(),
        headers=Header({}),
        version="1.1",
        method="GET",
        transport=cast(TransportProtocol, None),
        app=Sanic.get_app(APP_NAME),
    )
    result.ctx.user = SimpleNamespace(role=UserRole.ADMIN)
    return result


async def proxy(handler, **query: object) -> tuple[HTTPResponse, bytes]:
    """Call the image proxy against a fake upstream and drain any stream.

    Args:
        handler: Mock transport handler for upstream image requests.
        **query: Query parameters for `RemoteProxy`.

    Returns:
        Response and the bytes it served.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        Sanic.get_app(APP_NAME).ctx.httpx = client
        callback = cast(
            Callable[[Request], Awaitable[HTTPResponse]], routes.proxy_remote_image
        )
        response = await callback(request(**query))
        if not isinstance(response, ResponseStream):
            return response, response.body or b""
        chunks: list[bytes] = []

        async def write(chunk: bytes):
            chunks.append(chunk)

        stream = SimpleNamespace(
            response=SimpleNamespace(status=0, headers={}), write=write
        )
        await response.streaming_fn(cast(ResponseStream, stream))
        return response, b"".join(chunks)


def upstream(_):
    return httpx.Response(200, stream=httpx.ByteStream(b"remote"))


def offline(_):
    pytest.fail("A local image must not contact the upstream server")


def test_local(images):
    response, body = asyncio.run(proxy(offline, url="icons/logo.png"))

    assert response.status == 200
    assert body == b"logo"


@pytest.mark.parametrize("url", ["../secret.png", "icons/../../secret.png"])
def test_local_escape(images, url):
    response, body = asyncio.run(proxy(offline, url=url))

    assert response.status == 404
    assert body == b""


def test_absolute_escape(images):
    url = str(images.parent / "secret.png")

    response, body = asyncio.run(proxy(offline, url=url))

    assert response.status == 404
    assert body == b""


def test_store(images):
    _, body = asyncio.run(
        proxy(upstream, url="https://example.test/covers/a.png", store=True)
    )

    assert body == b"remote"
    assert (images / "example.test" / "covers" / "a.png").read_bytes() == b"remote"


def test_store_escape(images):
    _, body = asyncio.run(
        proxy(upstream, url="http://example.test/../../../escaped.png", store=True)
    )

    assert body == b"remote"
    assert not any(images.parent.parent.rglob("escaped.png"))


@pytest.mark.parametrize(
    ("directory", "filename", "status"),
    [("icons", "logo", 200), ("..", "secret", 404), ("icons", "missing", 404)],
)
def test_cached(images, directory, filename, status):
    callback = cast(Callable[..., Awaitable[HTTPResponse]], routes.get_image)

    response = asyncio.run(callback(None, directory, filename, "png"))

    assert response.status == status
