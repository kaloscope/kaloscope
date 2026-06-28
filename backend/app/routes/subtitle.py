from sanic import Blueprint, HTTPResponse, json
from sanic_ext import validate

from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.media import MediaResource
from app.services.subtitle import SubtitleService

# subroutes for all subtitle related operations
subtitle = Blueprint("subtitle", url_prefix="/subtitle")


class SubtitleContentQuery(MediaResource):
    """Query parameters for loading local subtitle content."""

    stream: int | None = None
    """The embedded subtitle stream index."""


@subtitle.post("/tracks")
@validate(json=MediaResource)
async def list_tracks(_, body: MediaResource) -> HTTPResponse:
    """List subtitle tracks for the given media resource."""
    subtitles = await SubtitleService.list_tracks(body.path)
    return json([s.model_dump() for s in subtitles])


@subtitle.get("/content")
@validate(query=SubtitleContentQuery)
async def load_content(_, query: SubtitleContentQuery) -> HTTPResponse:
    """Load local subtitle content."""
    result = await SubtitleService.load_content(query.path, query.stream)
    if not result:
        raise KaloscopeException(ErrorCode.FILE_NOT_EXISTS)
    content, content_type = result
    return HTTPResponse(
        content,
        content_type=content_type,
        headers={"Cache-Control": "no-cache"},
    )
