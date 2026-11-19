from sanic import Blueprint, HTTPResponse, Request, json
from sanic_ext import validate

from app.core.decorators import authorize
from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.media import MediaResource
from app.services.media import MediaItemService
from app.services.subtitle import SubtitleService

subtitle = Blueprint("subtitle", url_prefix="/subtitle")


class SubtitleContentQuery(MediaResource):
    """Query parameters for loading local subtitle content."""

    stream: int | None = None
    """The embedded subtitle stream index."""


@subtitle.post("/tracks")
@authorize()
@validate(json=MediaResource)
async def list_tracks(request: Request, body: MediaResource) -> HTTPResponse:
    """List subtitle tracks for an accessible media item.

    Args:
        request: Authenticated request with loaded library permissions.
        body: Media item path.

    Returns:
        Response containing the requested data.

    Raises:
        ForbiddenException: If no accessible media item has the path.
    """
    await MediaItemService.get_by_path(body.path, request.ctx.user)
    subtitles = await SubtitleService.list_tracks(body.path)
    return json([s.model_dump() for s in subtitles])


@subtitle.get("/content")
@authorize()
@validate(query=SubtitleContentQuery)
async def load_content(request: Request, query: SubtitleContentQuery) -> HTTPResponse:
    """Load subtitle content that belongs to an accessible media item.

    Args:
        request: Authenticated request with loaded library permissions.
        query: Subtitle or video path and optional embedded stream index.

    Returns:
        Response containing the requested data.

    Raises:
        ForbiddenException: If no accessible media item owns the subtitle.
        KaloscopeException: If the subtitle cannot be loaded.
    """
    await SubtitleService.check_access(query.path, query.stream, request.ctx.user)
    result = await SubtitleService.load_content(query.path, query.stream)
    if not result:
        raise KaloscopeException(ErrorCode.FILE_NOT_EXISTS)
    content, content_type = result
    return HTTPResponse(
        content,
        content_type=content_type,
        headers={"Cache-Control": "no-cache"},
    )
