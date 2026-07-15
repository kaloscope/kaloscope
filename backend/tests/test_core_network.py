"""Unit tests for network helpers."""

from app.core.network import _build_proxy_url
from app.models.network import HTTPProxy, ProxyProtocol
from app.utils.crypto import xor_encrypt


def test_encode_proxy_credentials():
    """Test that proxy credentials are safely encoded as URL userinfo."""
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
