"""Serve MCP with dedicated tokens and manage them through browser sessions."""

from urllib.parse import urlsplit

from sanic import Blueprint, HTTPResponse, Request, empty, json, raw
from sanic_ext import validate

from app.core.decorators import authorize
from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.mcp import handle_http
from app.models.mcp import MCPAccessTokenCreate
from app.services.mcp import MCPAccessTokenService

mcp = Blueprint("mcp", url_prefix="/mcp")


@mcp.route("", methods=["POST", "GET", "DELETE"], strict_slashes=True)
@authorize()
async def dispatch(request: Request) -> HTTPResponse:
    """Accept authenticated MCP requests without creating another server process.

    Args:
        request: Request authenticated with a dedicated MCP access token.

    Returns:
        SDK JSON response, or `405` for unsupported streaming/session operations.

    Raises:
        UnauthorizedException: If dedicated MCP authentication is missing.
        ForbiddenException: If a browser sends a foreign or malformed origin.
    """
    if not getattr(request.ctx, "mcp_authenticated", False):
        raise UnauthorizedException
    if origin := request.headers.get("origin"):
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise ForbiddenException from None
        if (
            parsed.scheme != request.scheme
            or parsed.netloc.lower() != request.host.lower()
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ForbiddenException
    if request.method != "POST":
        return raw(b"", status=405, headers={"Allow": "POST"})
    return await handle_http(request)


@mcp.get("/token/list")
@authorize()
async def list_tokens(request: Request) -> HTTPResponse:
    """List the signed-in user's MCP tokens without revealing credentials.

    Args:
        request: Request authenticated through a browser login session.

    Returns:
        Public metadata for the user's active and expired tokens.
    """
    return json(
        await MCPAccessTokenService.list_tokens(request.ctx.user.id),
        headers={"Cache-Control": "no-store"},
    )


@mcp.post("/token/create")
@authorize()
@validate(json=MCPAccessTokenCreate)
async def create_token(request: Request, body: MCPAccessTokenCreate) -> HTTPResponse:
    """Issue a scoped MCP credential for the signed-in user.

    Args:
        request: Request authenticated through a browser login session.
        body: Validated token name, grant, and lifetime.

    Returns:
        Public metadata and the only response containing the full token.
    """
    return json(
        await MCPAccessTokenService.create(request.ctx.user.id, body),
        headers={"Cache-Control": "no-store"},
    )


@mcp.post("/token/<id:int>/delete")
@authorize()
async def delete_token(request: Request, id: int) -> HTTPResponse:
    """Delete an active or expired token belonging to the signed-in user.

    Args:
        request: Request authenticated through a browser login session.
        id: Token identifier to delete.

    Returns:
        Empty response after deletion.

    Raises:
        NotFoundException: If the token is missing or belongs to another account.
    """
    await MCPAccessTokenService.delete(id, request.ctx.user.id)
    return empty()
