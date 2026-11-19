from sanic import Blueprint, HTTPResponse, Request, empty, json
from sanic_ext import validate

from app.core.decorators import authorize
from app.models.media import MediaResource
from app.services.danmaku import (
    AnimeConfirm,
    DanmakuQuery,
    DanmakuService,
    EpisodeConfirm,
)
from app.services.media import MediaItemService

danmaku = Blueprint("danmaku", url_prefix="/danmaku")


@danmaku.post("/match")
@authorize()
@validate(json=MediaResource)
async def match_danmakus(request: Request, body: MediaResource) -> HTTPResponse:
    """Match overlay comments for an accessible media item.

    Args:
        request: Authenticated request with loaded library permissions.
        body: Media item path.

    Returns:
        Response containing the requested data.

    Raises:
        ForbiddenException: If no accessible media item has the path.
    """
    await MediaItemService.get_by_path(body.path, request.ctx.user)
    danmakus = await DanmakuService.match_danmakus(body.path)
    return json(danmakus.model_dump())


@danmaku.post("/delete")
@authorize()
@validate(json=MediaResource)
async def delete_danmakus(request: Request, body: MediaResource) -> HTTPResponse:
    """Delete the locally cached overlay comments for an accessible media item.

    Args:
        request: Authenticated request with loaded library permissions.
        body: Media item path.

    Returns:
        Empty response on success.

    Raises:
        ForbiddenException: If no accessible media item has the path.
    """
    await MediaItemService.get_by_path(body.path, request.ctx.user)
    await DanmakuService.delete_danmakus(body.path)
    return empty()


@danmaku.post("/search")
@authorize()
@validate(json=DanmakuQuery)
async def search_episodes(request: Request, body: DanmakuQuery) -> HTTPResponse:
    """Search the comment server for episodes of an accessible media item.

    Args:
        request: Authenticated request with loaded library permissions.
        body: Media item path and search title.

    Returns:
        Response containing the requested data.

    Raises:
        ForbiddenException: If no accessible media item has the path.
    """
    await MediaItemService.get_by_path(body.path, request.ctx.user)
    episodes = await DanmakuService.search_episodes(body.path, body.title)
    return json([e.model_dump() for e in episodes])


@danmaku.post("/confirm")
@authorize()
@validate(json=EpisodeConfirm)
async def confirm_episode(request: Request, body: EpisodeConfirm) -> HTTPResponse:
    """Confirm the episode match result for an accessible media item.

    Args:
        request: Authenticated request with loaded library permissions.
        body: Media item path and selected episode.

    Returns:
        Response containing the requested data.

    Raises:
        ForbiddenException: If no accessible media item has the path.
    """
    await MediaItemService.get_by_path(body.path, request.ctx.user)
    danmakus = await DanmakuService.confirm_episode(body.path, body.metadata)
    return json(danmakus.model_dump())


@danmaku.post("/anime/search")
@authorize()
@validate(json=DanmakuQuery)
async def search_anime(request: Request, body: DanmakuQuery) -> HTTPResponse:
    """Search anime for an accessible media library item.

    Args:
        request: Authenticated request with loaded library permissions.
        body: Media item path and search title.

    Returns:
        Response containing the requested data.

    Raises:
        ForbiddenException: If no accessible media item has the path.
    """
    await MediaItemService.get_by_path(body.path, request.ctx.user)
    results = await DanmakuService.search_anime(body.path, body.title)
    return json([a.model_dump() for a in results])


@danmaku.post("/anime/confirm")
@authorize()
@validate(json=AnimeConfirm)
async def confirm_anime(request: Request, body: AnimeConfirm) -> HTTPResponse:
    """Confirm an anime match for all files in an accessible media library item.

    Args:
        request: Authenticated request with loaded library permissions.
        body: Media item path and selected series.

    Returns:
        Response containing the requested data.

    Raises:
        ForbiddenException: If no accessible media item has the path.
    """
    await MediaItemService.get_by_path(body.path, request.ctx.user)
    return json(await DanmakuService.confirm_anime(body.path, body.metadata))
