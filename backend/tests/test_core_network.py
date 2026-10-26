"""Unit tests for network helpers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from app.core import network
from app.core.network import NetworkTransport, _build_proxy_url
from app.models.network import HTTPProxy, ProxyProtocol
from app.utils.crypto import xor_encrypt


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
