"""Serve the read-only MCP endpoint using existing session authorization."""

from urllib.parse import urlsplit

from sanic import Blueprint, HTTPResponse, Request, raw

from app.core.decorators import authorize
from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.mcp import handle_http

mcp = Blueprint("mcp", url_prefix="/mcp")


@mcp.route("", methods=["POST", "GET", "DELETE"])
@authorize()
async def dispatch(request: Request) -> HTTPResponse:
    """Accept authenticated MCP requests without creating another server process.

    Args:
        request: Request authenticated by the application's session middleware.

    Returns:
        SDK JSON response, or `405` for unsupported streaming/session operations.

    Raises:
        UnauthorizedException: If bearer authentication is missing.
        ForbiddenException: If a browser sends a foreign or malformed origin.
    """
    if not request.token:
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
