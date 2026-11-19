"""Unit tests for network helpers."""

import asyncio
import ipaddress
import socket
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from app.core import network
from app.core.exceptions import ForbiddenException
from app.core.network import (
    PUBLIC_ONLY,
    NetworkTransport,
    PublicNetworkBackend,
    _build_proxy_url,
    public_address,
)
from app.models.network import HTTPProxy, ProxyProtocol
from app.utils.crypto import xor_encrypt
from app.utils.proxy import check_proxy_target


def test_encode_proxy_credentials():
    """Test that proxy credentials are safely encoded in the URL credentials."""
    proxy = HTTPProxy(
        name="proxy",
        protocol=ProxyProtocol.HTTP,
        host="proxy.example",
        port=8080,
        username="user@example.com",
        password=xor_encrypt("p#ss:word"),
    )

    assert (
        _build_proxy_url(proxy)
        == "http://user%40example.com:p%23ss%3Aword@proxy.example:8080"
    )


class Rules:
    """Return fixed URL rules from awaited ORM-style queries."""

    def __init__(self, dns):
        """Keep the secure-DNS rules returned by the fake query.

        Args:
            dns: Rules matched for secure DNS; proxy queries return none.
        """
        self.dns = dns
        self.rules = []

    def filter(self, **kwargs):
        """Select DNS or proxy rules like the ORM filter.

        Args:
            **kwargs: Query filters; `secure_dns` selects the DNS rules.

        Returns:
            Chainable awaitable query.
        """
        self.rules = self.dns if kwargs.get("secure_dns") else []
        return self

    def prefetch_related(self, *_):
        """Ignore relation loading.

        Returns:
            This query.
        """
        return self

    def select_related(self, *_):
        """Ignore relation loading.

        Returns:
            This query.
        """
        return self

    def order_by(self, *_):
        """Ignore ordering.

        Returns:
            This query.
        """
        return self

    def __await__(self):
        """Resolve the selected rules.

        Returns:
            Awaitable rule list.
        """
        return asyncio.sleep(0, self.rules).__await__()


def test_dns_redirect(monkeypatch):
    routed = []

    async def send(_, request):
        routed.append(request)
        if request.url.path == "/login":
            assert await request.aread() == b"user=demo"
            return httpx.Response(
                302, headers={"location": "/home", "set-cookie": "fresh=v; Path=/"}
            )
        # the saved hostname cookie must survive the relative redirect
        cookies = request.headers.get("cookie", "")
        return httpx.Response(200 if "sid=saved" in cookies else 401)

    rule = SimpleNamespace(
        pattern="https://example.test/*",
        resolvers=[SimpleNamespace(resolver=SimpleNamespace(nameserver="dns.test"))],
    )
    monkeypatch.setattr(network, "URLRule", Rules([rule]))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)

    async def run():
        transport = NetworkTransport()
        monkeypatch.setattr(
            transport, "_resolve_host", AsyncMock(return_value="192.0.2.10")
        )
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True
        ) as client:
            client.cookies.set("sid", "saved", domain="example.test", path="/")
            response = await client.post(
                "https://example.test/login", content=b"user=demo"
            )
            return response, {(c.domain, c.name) for c in client.cookies.jar}

    response, stored = asyncio.run(run())

    assert response.status_code == 200
    assert str(response.url) == "https://example.test/home"
    assert response.history[0].request.url.host == "example.test"
    assert stored == {("example.test", "sid"), ("example.test", "fresh")}
    assert [request.url.host for request in routed] == ["192.0.2.10"] * 2
    for request in routed:
        assert request.headers["host"] == "example.test"
        assert request.extensions["sni_hostname"] == "example.test"


class ProxyRules(Rules):
    """Return fixed proxy rules and no secure-DNS rules."""

    def filter(self, **kwargs):
        """Select proxy rules like the ORM filter.

        Args:
            **kwargs: Query filters; `http_proxy` selects the proxy rules.

        Returns:
            Chainable awaitable query.
        """
        self.rules = self.dns if kwargs.get("http_proxy") else []
        return self


class Dialer(httpcore.AsyncMockBackend):
    """Record dialed addresses and answer each connection with `200 ok`."""

    def __init__(self, refused: set[str] | None = None):
        """Prepare a canned HTTP response for every connection.

        Args:
            refused: Addresses that fail to connect.
        """
        super().__init__([b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"])
        self.refused = refused or set()
        self.dialed: list[str] = []

    async def connect_tcp(self, host, port, **kwargs):
        """Record the address and open a mock stream.

        Args:
            host: Dialed address.
            port: Dialed port.
            **kwargs: Unused connection options.

        Returns:
            Mock network stream.

        Raises:
            httpcore.ConnectError: If `host` is refused.
        """
        self.dialed.append(host)
        if host in self.refused:
            raise httpcore.ConnectError(f"refused {host}")
        return await super().connect_tcp(host, port, **kwargs)


def answers(*results):
    """Build a fake resolver that returns `results` in order.

    Args:
        *results: Address lists, or an exception to raise, one per lookup.

    Returns:
        Asynchronous resolver with the signature of `network.resolve`.
    """
    pending = list(results)

    async def resolve(host, port):
        result = pending.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    return resolve


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.215.14", True),
        ("2606:4700::6810:84e5", True),
        ("198.18.0.7", True),
        ("::ffff:93.184.215.14", True),
        ("64:ff9b::5db8:d70e", True),
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.100.100.100", False),
        ("192.0.2.1", False),
        ("224.0.0.251", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
        ("::", False),
        ("::1", False),
        ("::ffff:127.0.0.1", False),
        ("::7f00:1", False),
        ("64:ff9b::a00:1", False),
        ("2002:7f00:1::", False),
        ("fc00::1", False),
        ("fd12:3456::1", False),
        ("fe80::1", False),
        ("fec0::1", False),
        ("ff02::1", False),
    ],
)
def test_public_address(address, public):
    assert public_address(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(
    "result", [["10.0.0.5"], ["93.184.215.14", "fd00::5"], ["fe80::1%en0"]]
)
def test_dial_rejected(monkeypatch, result):
    monkeypatch.setattr(network, "resolve", answers(result))
    dialer = Dialer()

    with pytest.raises(ForbiddenException):
        asyncio.run(PublicNetworkBackend(dialer).connect_tcp("nas.test", 80))

    assert dialer.dialed == []


def test_dial_order(monkeypatch):
    monkeypatch.setattr(
        network, "resolve", answers(["2606:4700::6810:84e5", "93.184.215.14"])
    )
    dialer = Dialer(refused={"2606:4700::6810:84e5"})

    asyncio.run(PublicNetworkBackend(dialer).connect_tcp("cdn.test", 443))

    assert dialer.dialed == ["2606:4700::6810:84e5", "93.184.215.14"]


def test_dial_unresolved(monkeypatch):
    monkeypatch.setattr(network, "resolve", answers(socket.gaierror("no such host")))

    with pytest.raises(httpcore.ConnectError):
        asyncio.run(PublicNetworkBackend(Dialer()).connect_tcp("gone.test", 80))


def test_rebinding(monkeypatch):
    # the name passes the early check, then answers with loopback when dialed
    monkeypatch.setattr(network, "resolve", answers(["93.184.215.14"], ["127.0.0.1"]))
    dialer = Dialer()

    async def run():
        await check_proxy_target(httpx.URL("http://rebind.test/"))
        await PublicNetworkBackend(dialer).connect_tcp("rebind.test", 80)

    with pytest.raises(ForbiddenException):
        asyncio.run(run())

    assert dialer.dialed == []


@pytest.mark.parametrize(
    ("result", "dialed"), [(["93.184.215.14"], ["93.184.215.14"]), (["10.0.0.5"], [])]
)
def test_public_pool(monkeypatch, result, dialed):
    monkeypatch.setattr(network, "URLRule", Rules([]))
    monkeypatch.setattr(network, "resolve", answers(result))
    dialer = Dialer()

    async def run():
        transport = NetworkTransport()
        transport._public_pool()._pool._network_backend = PublicNetworkBackend(dialer)
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get(
                "http://cdn.test/a.png", extensions={PUBLIC_ONLY: True}
            )
            return response.content

    if dialed:
        assert asyncio.run(run()) == b"ok"
    else:
        with pytest.raises(ForbiddenException):
            asyncio.run(run())

    assert dialer.dialed == dialed


def test_public_route(monkeypatch):
    used = []

    async def send(self, request):
        used.append(self)
        return httpx.Response(200)

    monkeypatch.setattr(network, "URLRule", Rules([]))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)

    async def run():
        transport = NetworkTransport()
        async with httpx.AsyncClient(transport=transport) as client:
            await client.get("https://cdn.test/a", extensions={PUBLIC_ONLY: True})
            await client.get("https://cdn.test/a")
        return transport

    transport = asyncio.run(run())

    assert used == [transport._public_transport, transport]


@pytest.mark.parametrize(
    ("result", "proxied"),
    [
        (["93.184.215.14"], True),
        (["10.0.0.5"], False),
        (socket.gaierror("no such host"), False),
    ],
)
def test_public_proxy(monkeypatch, result, proxied):
    rule = SimpleNamespace(
        pattern="http://*",
        proxy=SimpleNamespace(name="proxy", protocol=ProxyProtocol.HTTP),
    )
    monkeypatch.setattr(network, "URLRule", ProxyRules([rule]))
    monkeypatch.setattr(network, "resolve", answers(result))

    async def run():
        transport = NetworkTransport()
        proxy_request = AsyncMock(return_value=httpx.Response(200))
        transport._proxy_request = proxy_request
        async with httpx.AsyncClient(transport=transport) as client:
            with suppress(ForbiddenException, httpx.ConnectError):
                await client.get("http://nas.test/", extensions={PUBLIC_ONLY: True})
            public_calls = proxy_request.await_count
            # a trusted request still uses the proxy without a check
            await client.get("http://nas.test/")
        return public_calls, proxy_request.await_count

    assert asyncio.run(run()) == (int(proxied), int(proxied) + 1)
