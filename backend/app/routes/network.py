from sanic import Blueprint, HTTPResponse, empty, json
from sanic_ext import validate
from tortoise.expressions import Q

from app.core.decorators import authorize
from app.models.base import IDs
from app.models.network import (
    DNSResolver,
    DNSResolverUpsert,
    HTTPProxy,
    HTTPProxyUpsert,
    URLRule,
    URLRuleQuery,
    URLRuleToggle,
    URLRuleUpsert,
)
from app.models.user import UserRole
from app.services.network import DNSResolverService, HTTPProxyService, URLRuleService

network = Blueprint("network", url_prefix="/network")


@network.get("/rule/list")
@validate(query=URLRuleQuery)
async def list_rules(_, query: URLRuleQuery) -> HTTPResponse:
    """List the URL rules.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    queries = []
    if query.pattern:
        queries.append(Q(pattern__icontains=query.pattern))
    rules = await URLRuleService.dump_list(URLRule.filter(*queries))
    for rule in rules:
        rule["proxy_id"] = p["id"] if (p := rule.pop("proxy", None)) else None
        rule["resolver_ids"] = [r["resolver_id"] for r in rule.pop("resolvers", [])]
    return json(rules)


@network.post("/rule/sort")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def sort_rules(_, body: IDs) -> HTTPResponse:
    """Sort the URL rules.

    Args:
        _: Unused request.
        body: Record IDs in display order.

    Returns:
        Empty response on success.
    """
    await URLRuleService.update_priorities(body.ids)
    return empty()


@network.post("/rule/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=URLRuleUpsert)
async def upsert_rule(_, body: URLRuleUpsert) -> HTTPResponse:
    """Create or update a URL rule.

    Args:
        _: Unused request.
        body: URL matching and routing settings.

    Returns:
        Response containing the requested data.
    """
    rule = await URLRuleService.upsert(body)
    return json(await URLRuleService.dump(rule))


@network.post("/rule/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_rules(_, body: IDs) -> HTTPResponse:
    """Delete the URL rules.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    await URLRule.filter(id__in=body.ids).delete()
    return empty()


@network.post("/rule/toggle")
@authorize(role=UserRole.ADMIN)
@validate(json=URLRuleToggle)
async def toggle_rule(_, body: URLRuleToggle) -> HTTPResponse:
    """Toggle boolean fields of a URL rule.

    Args:
        _: Unused request.
        body: URL rule ID and routing flags to change.

    Returns:
        Empty response on success.
    """
    fields = {}
    if body.secure_dns is not None:
        fields["secure_dns"] = body.secure_dns
    if body.http_proxy is not None:
        fields["http_proxy"] = body.http_proxy
    if fields:
        await URLRule.filter(id=body.id).update(**fields)
    return empty()


@network.get("/dns/list")
async def list_dns_resolvers(_) -> HTTPResponse:
    """List the DNS resolvers.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    resolvers = await DNSResolverService.dump_list(DNSResolver.all())
    return json(resolvers)


@network.post("/dns/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=DNSResolverUpsert)
async def upsert_dns_resolver(_, body: DNSResolverUpsert) -> HTTPResponse:
    """Create or update a DNS resolver.

    Args:
        _: Unused request.
        body: DNS server and connection settings.

    Returns:
        Response containing the requested data.
    """
    resolver = await DNSResolverService.upsert(body)
    return json(await DNSResolverService.dump(resolver))


@network.post("/dns/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_dns_resolvers(_, body: IDs) -> HTTPResponse:
    """Delete the DNS resolvers.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    await DNSResolver.filter(id__in=body.ids).delete()
    return empty()


@network.get("/proxy/list")
async def list_proxy_servers(_) -> HTTPResponse:
    """List the HTTP proxy servers.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    servers = await HTTPProxyService.dump_list(HTTPProxy.all())
    return json(servers)


@network.post("/proxy/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=HTTPProxyUpsert)
async def upsert_proxy_server(_, body: HTTPProxyUpsert) -> HTTPResponse:
    """Create or update an HTTP proxy server.

    Args:
        _: Unused request.
        body: HTTP proxy connection settings.

    Returns:
        Response containing the requested data.
    """
    server = await HTTPProxyService.upsert(body)
    return json(await HTTPProxyService.dump(server))


@network.post("/proxy/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_proxy_servers(_, body: IDs) -> HTTPResponse:
    """Delete the HTTP proxy servers.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    await HTTPProxy.filter(id__in=body.ids).delete()
    return empty()
