import asyncio
import ipaddress
import socket
import time
from collections.abc import Iterable
from fnmatch import fnmatch
from urllib.parse import quote

import dns.asyncquery
import dns.flags
import dns.message
import dns.rdatatype
import httpcore
import httpx
from sanic.log import logger

from app.core.exceptions import ForbiddenException
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

PUBLIC_ONLY = "public_only"
"""Request extension that limits a request and its redirects to public addresses."""

# DNS64 synthesizes these addresses for IPv4-only hosts on IPv6-only networks
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
# fake-ip proxies (Clash, mihomo, OpenClash) answer every name from this range and
# resolve the real host themselves, so blocking it would block every hostname
_FAKE_IP = ipaddress.ip_network("198.18.0.0/15")


def public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check whether an untrusted request may connect to `address`.

    Args:
        address: Literal or resolved IP address.

    Returns:
        `True` for globally routable unicast addresses and fake-ip proxy addresses.
        `False` for loopback, private, link-local, shared (`100.64.0.0/10`),
        reserved, documentation, and multicast addresses. IPv4-mapped and NAT64
        addresses are judged by the IPv4 address they carry.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        elif address in _NAT64:
            address = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        elif address.is_site_local:
            return False
    if address in _FAKE_IP:
        return True
    return address.is_global and not (address.is_multicast or address.is_reserved)


async def resolve(host: str, port: int) -> list[str]:
    """Resolve `host` with the system resolver.

    Args:
        host: Hostname or literal IP address, without IPv6 brackets.
        port: TCP port to resolve for.

    Returns:
        Distinct addresses in resolver order. Numeric forms such as `127.1` resolve
        to the address they denote.

    Raises:
        OSError: If the host cannot be resolved.
    """
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def public_addresses(host: str, port: int) -> list[str]:
    """Resolve `host` and require every address to be public.

    Rejecting a host when any address is not public keeps a name with mixed
    records from reaching an internal address on a later connection.

    Args:
        host: Hostname or literal IP address, without IPv6 brackets.
        port: TCP port to resolve for.

    Returns:
        Public addresses in resolver order.

    Raises:
        OSError: If the host cannot be resolved.
        ForbiddenException: If any resolved address is not public.
    """
    addresses = await resolve(host, port)
    for address in addresses:
        # drop an IPv6 zone such as `%en0`; the scope does not change the class
        if not public_address(ipaddress.ip_address(address.split("%", 1)[0])):
            logger.warning("Blocked a public-only request to %s (%s).", host, address)
            raise ForbiddenException
    return addresses


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """A network backend that dials only public addresses it resolved itself.

    The check runs on the address actually dialed, so a hostname whose DNS answer
    changes after an earlier check (DNS rebinding) still cannot reach an internal
    address. TLS keeps the hostname for SNI and certificate verification.
    """

    def __init__(self, backend: httpcore.AsyncNetworkBackend | None = None):
        """Wrap the backend that opens the checked connections.

        Args:
            backend: Backend used to dial each checked address. `None` uses AnyIO.
        """
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Resolve and check `host`, then dial its public addresses in order.

        Args:
            host: Origin hostname or literal IP address.
            port: Origin TCP port.
            timeout: Connect timeout for each address, in seconds.
            local_address: Local address to bind, if configured.
            socket_options: Socket options for the connection.

        Returns:
            Stream connected to the first reachable public address.

        Raises:
            ForbiddenException: If any resolved address is not public.
            httpcore.ConnectError: If the host cannot be resolved or dialed.
        """
        try:
            addresses = await public_addresses(host, port)
        except OSError as e:
            raise httpcore.ConnectError(str(e)) from e
        error: Exception = httpcore.ConnectError(f"No address for {host}")
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                error = e
        raise error

    async def sleep(self, seconds: float) -> None:
        """Wait between connection retries.

        Args:
            seconds: Delay in seconds.
        """
        await self._backend.sleep(seconds)


class DNSCache:
    """Per-DNS server DNS cache with TTL expiration."""

    def __init__(self, ttl: int = _DNS_CACHE_TTL):
        """Create a separate DNS cache for each server.

        Args:
            ttl: Cache duration in seconds; defaults to six hours.
        """
        self._ttl = ttl
        # {nameserver: {host: (ip, expires_at)}}
        self._cache: dict[str, dict[str, tuple[str, float]]] = {}

    def get(self, nameserver: str, host: str) -> str | None:
        """Get cached IP for host from a specific DNS server.

        Args:
            nameserver: DNS server used for resolution.
            host: Hostname to look up in the cache.

        Returns:
            Cached IP address if valid, or `None` if not found / expired.
        """
        entries = self._cache.get(nameserver)
        if entries and (entry := entries.get(host)):
            ip, expires_at = entry
            if time.monotonic() < expires_at:
                return ip
            del entries[host]
        return None

    def set(self, nameserver: str, host: str, ip: str):
        """Cache resolved IP for host under a specific DNS server.

        Args:
            nameserver: DNS server used for resolution.
            host: Hostname that was resolved.
            ip: Resolved IP address to cache.
        """
        entries = self._cache.setdefault(nameserver, {})
        entries[host] = (ip, time.monotonic() + self._ttl)


class NetworkTransport(httpx.AsyncHTTPTransport):
    """An HTTP transport that routes requests via custom DNS and proxy rules."""

    def __init__(self, **kwargs):
        """Keep transport options for custom DNS and proxy routing.

        Args:
            **kwargs: Options forwarded to the base and proxy HTTP transports.
        """
        self._transport_kwargs = kwargs
        self._proxy_transports: dict[str, httpx.AsyncHTTPTransport] = {}
        self._dns_cache = DNSCache()
        self._public_transport: httpx.AsyncHTTPTransport | None = None
        super().__init__(**kwargs)

    async def aclose(self):
        """Close proxy and public-only transports before the default transport."""
        for transport in self._proxy_transports.values():
            await transport.aclose()
        self._proxy_transports.clear()
        if self._public_transport is not None:
            await self._public_transport.aclose()
            self._public_transport = None
        await super().aclose()

    def _public_pool(self) -> httpx.AsyncHTTPTransport:
        """Create or reuse the transport for `PUBLIC_ONLY` requests.

        A separate pool keeps those requests off connections that trusted callers
        opened to internal hosts, and its backend checks every dialed address.

        Returns:
            Transport whose connections reach only public addresses.
        """
        if self._public_transport is None:
            transport = httpx.AsyncHTTPTransport(**self._transport_kwargs)
            # httpx exposes no option for the pool's network backend
            transport._pool._network_backend = PublicNetworkBackend()
            self._public_transport = transport
        return self._public_transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Apply matching DNS and proxy rules before sending a request.

        Args:
            request: HTTP request to route. DNS rules send a copy addressed to the
                resolved IP, so the caller's URL keeps its hostname for redirects
                and cookie scope. The `PUBLIC_ONLY` extension dials only public
                addresses; through a configured proxy, which resolves the host
                itself, only the address resolved here can be checked.

        Returns:
            HTTP response from the selected transport.

        Raises:
            ForbiddenException: If a `PUBLIC_ONLY` request targets a non-public
                address.
        """
        url = str(request.url)
        host = request.url.host
        logger.debug("Handling request for URL: %s", url)

        # step 1: DNS resolution
        routed = request
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
                    routed = httpx.Request(
                        request.method,
                        request.url.copy_with(host=ip),
                        headers=request.headers,
                        stream=request.stream,
                        # preserve original hostname for Host header and TLS SNI
                        extensions={**request.extensions, "sni_hostname": host},
                    )
                    routed.headers["host"] = host

        # step 2: proxy routing
        proxy_rules = (
            await URLRule.filter(http_proxy=True, proxy_id__not_isnull=True)
            .select_related("proxy")
            .order_by("priority")
        )
        public = bool(request.extensions.get(PUBLIC_ONLY))
        proxy_rule = self._match(url, proxy_rules)
        if proxy_rule is not None and (proxy := proxy_rule.proxy):
            logger.debug(
                "URL matches pattern '%s', routing via proxy '%s'",
                proxy_rule.pattern,
                proxy.name,
            )
            if public:
                # the proxy dials on its own, so check the secure DNS or system answer
                target = routed.url.raw_host.decode("ascii")
                port = routed.url.port or (443 if routed.url.scheme == "https" else 80)
                try:
                    await public_addresses(target, port)
                except OSError as e:
                    raise httpx.ConnectError(str(e), request=request) from e
            if proxy.protocol == ProxyProtocol.HTTP and routed.url.host != host:
                # for HTTP proxies, ensure we use the original hostname in the URL
                routed.url = routed.url.copy_with(host=host)
            return await self._proxy_request(proxy, routed)

        if public:
            return await self._public_pool().handle_async_request(routed)
        # use the default transport if no proxy rules match
        return await super().handle_async_request(routed)

    def _match(self, url: str, rules: list[URLRule]) -> URLRule | None:
        """Find the first URL rule that matches the given URL.

        Args:
            url: The URL to match against the rules.
            rules: A list of URL rules to check.

        Returns:
            First matching URL rule, or `None` if no rules match.
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
            host: Hostname to resolve.
            resolvers: A list of DNS resolvers to query for the hostname.

        Returns:
            Resolved IP address, or `None` if no rules match.
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
            host: Hostname to resolve.
            resolver: The DNS resolver configuration.

        Returns:
            Resolved IP address, or `None` on failure / DNSSEC validation error.
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
            request: Original request that needs to be proxied.

        Returns:
            Response from the proxied request.
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
        address: String to check.

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
        url: Request URL to match against configured proxy rules.

    Returns:
        First matching proxy URL, or `None` if no rule matches.
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
        proxy: Proxy configuration to encode.

    Returns:
        Proxy URL.
    """
    scheme = "socks5" if proxy.protocol == ProxyProtocol.SOCKS5 else "http"
    if (username := proxy.username) and (password := proxy.password):
        username = quote(username, safe="")
        password = quote(xor_decrypt(password), safe="")
        return f"{scheme}://{username}:{password}@{proxy.host}:{proxy.port}"
    return f"{scheme}://{proxy.host}:{proxy.port}"
