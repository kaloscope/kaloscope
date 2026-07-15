import asyncio
import ipaddress
import time
from fnmatch import fnmatch
from urllib.parse import quote

import dns.asyncquery
import dns.flags
import dns.message
import dns.rdatatype
import httpx
from sanic.log import logger

from app.models.network import (
    DNSProtocol,
    DNSResolver,
    HTTPProxy,
    ProxyProtocol,
    URLRule,
)
from app.utils.crypto import xor_decrypt

# default DNS cache TTL in seconds
_DNS_CACHE_TTL = 6 * 60 * 60


class DNSCache:
    """Per-nameserver DNS cache with TTL expiration."""

    def __init__(self, ttl: int = _DNS_CACHE_TTL):
        self._ttl = ttl
        # {nameserver: {host: (ip, expires_at)}}
        self._cache: dict[str, dict[str, tuple[str, float]]] = {}

    def get(self, nameserver: str, host: str) -> str | None:
        """Get cached IP for host from a specific nameserver.

        Args:
            nameserver: The nameserver that was used for resolution.
            host: The hostname to look up in the cache.

        Returns:
            The cached IP address if valid, or `None` if not found / expired.
        """
        entries = self._cache.get(nameserver)
        if entries and (entry := entries.get(host)):
            ip, expires_at = entry
            if time.monotonic() < expires_at:
                return ip
            del entries[host]
        return None

    def set(self, nameserver: str, host: str, ip: str):
        """Cache resolved IP for host under a specific nameserver.

        Args:
            nameserver: The nameserver that was used for resolution.
            host: The hostname that was resolved.
            ip: The resolved IP address to cache.
        """
        entries = self._cache.setdefault(nameserver, {})
        entries[host] = (ip, time.monotonic() + self._ttl)


class NetworkTransport(httpx.AsyncHTTPTransport):
    """An HTTP transport that routes requests via custom DNS and proxy rules."""

    def __init__(self, **kwargs):
        self._transport_kwargs = kwargs
        self._proxy_transports: dict[str, httpx.AsyncHTTPTransport] = {}
        self._dns_cache = DNSCache()
        super().__init__(**kwargs)

    async def aclose(self):
        for transport in self._proxy_transports.values():
            await transport.aclose()
        self._proxy_transports.clear()
        await super().aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = request.url.host
        logger.debug("Handling request for URL: %s", url)

        # step 1: DNS resolution
        if not ip_address(host):
            dns_rules = (
                await URLRule.filter(secure_dns=True)
                .prefetch_related("resolvers__resolver")
                .order_by("priority")
            )
            dns_rule = self._match(url, dns_rules)
            if dns_rule is not None:
                logger.debug(
                    "URL matches pattern '%s', applying secure DNS resolution",
                    dns_rule.pattern,
                )
                resolvers: list[DNSResolver] = [
                    r.resolver for r in dns_rule.resolvers if r.resolver
                ]
                ip = await self._resolve_host(host, resolvers)
                if ip is not None:
                    logger.debug("Resolved '%s' -> '%s' via secure DNS", host, ip)
                    request.url = request.url.copy_with(host=ip)
                    # preserve original hostname for Host header and TLS SNI
                    request.headers["host"] = host
                    request.extensions["sni_hostname"] = host

        # step 2: proxy routing
        proxy_rules = (
            await URLRule.filter(http_proxy=True, proxy_id__not_isnull=True)
            .select_related("proxy")
            .order_by("priority")
        )
        proxy_rule = self._match(url, proxy_rules)
        if proxy_rule is not None and (proxy := proxy_rule.proxy):
            logger.debug(
                "URL matches pattern '%s', routing via proxy '%s'",
                proxy_rule.pattern,
                proxy.name,
            )
            if proxy.protocol == ProxyProtocol.HTTP and request.url.host != host:
                # for HTTP proxies, ensure we use the original hostname in the URL
                request.url = request.url.copy_with(host=host)
            return await self._proxy_request(proxy, request)

        # use the default transport if no proxy rules match
        return await super().handle_async_request(request)

    def _match(self, url: str, rules: list[URLRule]) -> URLRule | None:
        """Find the first URLRule that matches the given URL.

        Args:
            url: The URL to match against the rules.
            rules: A list of URLRule objects to check.

        Returns:
            The first matching URLRule, or `None` if no rules match.
        """
        for rule in rules:
            # ensure pattern ends with '*' for prefix matching
            pattern = p if (p := rule.pattern).endswith("*") else p + "*"
            if fnmatch(url, pattern):
                return rule
        return None

    async def _resolve_host(
        self, host: str, resolvers: list[DNSResolver]
    ) -> str | None:
        """Resolve host using the provided list of DNS resolvers.

        Args:
            host: The hostname to resolve.
            resolvers: A list of DNS resolvers to query for the hostname.

        Returns:
            The resolved IP address, or `None` if no rules match.
        """
        # check cache first, return the first cached result if available
        for resolver in resolvers:
            if cached := self._dns_cache.get(resolver.nameserver, host):
                return cached

        # query all resolvers concurrently, take the first valid result
        queries = [self._dns_query(host, resolver) for resolver in resolvers]
        for earliest in asyncio.as_completed(queries):
            if ip := await earliest:
                return ip

        return None

    async def _dns_query(self, host: str, resolver: DNSResolver) -> str | None:
        """Perform a single DNS query via DoT or DoH.

        Args:
            host: The hostname to resolve.
            resolver: The DNS resolver configuration.

        Returns:
            The resolved IP address, or `None` on failure / DNSSEC validation error.
        """
        try:
            query = dns.message.make_query(host, dns.rdatatype.A)
            if resolver.dnssec:
                query.flags |= dns.flags.AD
                query.want_dnssec(True)  # optional: AD flag is sufficient here

            if resolver.protocol == DNSProtocol.TLS:
                response = await dns.asyncquery.tls(query, resolver.nameserver)
            else:
                response = await dns.asyncquery.https(query, resolver.nameserver)

            # discard if DNSSEC validation failed
            if resolver.dnssec and not (response.flags & dns.flags.AD):
                logger.warning(
                    "DNSSEC validation failed for '%s' via %s, discarding",
                    host,
                    resolver.nameserver,
                )
                return None

            for rrset in response.answer:
                if rrset.rdtype == dns.rdatatype.A:
                    ip = str(rrset[0])
                    self._dns_cache.set(resolver.nameserver, host, ip)
                    return ip
        except Exception:
            logger.debug(
                "DNS query failed for '%s' via %s",
                host,
                resolver.nameserver,
                exc_info=True,
            )
        return None

    async def _proxy_request(
        self, proxy: HTTPProxy, request: httpx.Request
    ) -> httpx.Response:
        """Make an asynchronous HTTP request via the specified proxy.

        Args:
            proxy: The HTTP proxy to use for the request.
            request: The original request that needs to be proxied.

        Returns:
            The response from the proxied request.
        """

        proxy_url = _build_proxy_url(proxy)

        # create or reuse the transport for the proxy
        transport = self._proxy_transports.get(proxy_url)
        if transport is None:
            transport = httpx.AsyncHTTPTransport(
                proxy=proxy_url, **self._transport_kwargs
            )
            self._proxy_transports[proxy_url] = transport

        # send the request via the proxy transport
        return await transport.handle_async_request(request)


def ip_address(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Check if the given string is a valid IP address.

    Args:
        address: The string to check.

    Returns:
        An IP address object, or `None` if the string is not a valid IP address.
    """
    try:
        return ipaddress.ip_address(address)
    except ValueError:
        return None


async def resolve_proxy(url: str) -> str | None:
    """Resolve the proxy URL from the first matching rule for `url`.

    Args:
        url: The request URL to match against configured proxy rules.

    Returns:
        The first matching proxy URL, or `None` if no rule matches.
    """
    proxy_rules = (
        await URLRule.filter(http_proxy=True, proxy_id__not_isnull=True)
        .select_related("proxy")
        .order_by("priority")
    )
    for rule in proxy_rules:
        # ensure pattern ends with '*' for prefix matching
        pattern = p if (p := rule.pattern).endswith("*") else p + "*"
        if not fnmatch(url, pattern):
            continue
        if (proxy := rule.proxy) is None:
            return None
        logger.debug(
            "URL matches pattern '%s', routing via proxy '%s'",
            rule.pattern,
            proxy.name,
        )
        return _build_proxy_url(proxy)
    return None


def _build_proxy_url(proxy: HTTPProxy) -> str:
    """Build a URL for `proxy`, including credentials when configured.

    Args:
        proxy: The proxy configuration to encode.

    Returns:
        The proxy URL.
    """
    scheme = "socks5" if proxy.protocol == ProxyProtocol.SOCKS5 else "http"
    if (username := proxy.username) and (password := proxy.password):
        username = quote(username, safe="")
        password = quote(xor_decrypt(password), safe="")
        return f"{scheme}://{username}:{password}@{proxy.host}:{proxy.port}"
    return f"{scheme}://{proxy.host}:{proxy.port}"
