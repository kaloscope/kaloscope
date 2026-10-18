import re
from pathlib import Path
from typing import cast

import httpx
from aiofiles import os as async_os
from sanic import Blueprint, HTTPResponse, Request, empty, json, redirect
from sanic.exceptions import InvalidRangeType, RangeNotSatisfiable
from sanic.log import logger
from sanic.response import ResponseStream, file_stream
from sanic_ext import validate

from app.core.config import KaloscopeConfig
from app.core.decorators import authorize
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
    content_error,
)
from app.core.media.common import ContentError
from app.core.media.shelver import (
    gen_nfo,
    get_nfo_path,
    get_nfo_type,
)
from app.core.media.watcher import LibWatcher
from app.core.transcode import (
    delete_tasks,
    ensure_transcode,
    list_tasks,
    output_dir,
    probe_media,
    read_m3u8,
    stop_tasks,
)
from app.models.base import IDs, Range
from app.models.flow import GraphCategory
from app.models.media import (
    LibType,
    MediaAssetQuery,
    MediaContentQuery,
    MediaDel,
    MediaItem,
    MediaLib,
    MediaLibUpsert,
    MediaMetadata,
    MediaQuery,
    MediaResource,
    TranscodeQuery,
    TranscodeTaskQuery,
)
from app.models.user import UserRole
from app.services.flow import FlowTriggerService
from app.services.media import MediaItemService, MediaLibService
from app.utils.extractor import extract_title
from app.utils.proxy import PROXY_RESPONSE_HEADERS, RemoteProxy, remote_proxy_request

media = Blueprint("media", url_prefix="/media")


@media.get("/lib/list")
@authorize()
async def list_libraries(request: Request) -> HTTPResponse:
    """List the media libraries.

    Args:
        request: Incoming request with session and application context.

    Returns:
        Response containing the requested data.
    """
    return json(
        await MediaLibService.list_libraries(
            request.ctx.user, request.app.ctx.lib_watcher
        )
    )


@media.post("/lib/sort")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def sort_libraries(_, body: IDs) -> HTTPResponse:
    """Sort the media libraries.

    Args:
        _: Unused request.
        body: Record IDs in display order.

    Returns:
        Empty response on success.
    """
    await MediaLibService.update_priorities(body.ids)
    return empty()


@media.post("/lib/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=MediaLibUpsert)
async def upsert_library(_, body: MediaLibUpsert) -> HTTPResponse:
    """Create or update a media library.

    Args:
        _: Unused request.
        body: Media library paths and settings.

    Returns:
        Response containing the requested data.
    """
    lib = await MediaLibService.upsert(body)
    return json(await MediaLibService.dump(lib))


@media.post("/lib/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_libraries(_, body: IDs) -> HTTPResponse:
    """Delete the media libraries.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    for id in body.ids:
        try:
            await MediaLibService.delete(int(id))
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to delete the media library: %s", id, exc_info=True)
    return empty()


@media.get("/lib/<id:int>/scan")
@authorize(role=UserRole.ADMIN)
async def scan_library(request: Request, id: int) -> HTTPResponse:
    """Scan the media library.

    Args:
        request: Incoming request with session and application context.
        id: Media library ID.

    Returns:
        Empty response on success.
    """
    lib = await MediaLib.get(id=id)
    watcher: LibWatcher = request.app.ctx.lib_watcher
    try:
        await watcher.scan_directory(lib, validate_request=True)
    except ContentError as error:
        raise content_error(error, {}) from error
    return empty()


@media.get("/list")
@authorize()
@validate(query=MediaQuery)
async def list_items(request: Request, query: MediaQuery) -> HTTPResponse:
    """List the media items.

    Args:
        request: Incoming request with session and application context.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    return json(await MediaItemService.list_items(query, request.ctx.user))


@media.post("/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=MediaDel)
async def delete_items(_, body: MediaDel) -> HTTPResponse:
    """Delete the media items.

    Args:
        _: Unused request.
        body: Media item IDs and local file deletion options.

    Returns:
        Empty response on success.
    """
    for id in body.ids:
        try:
            await MediaItemService.delete(int(id), body.local)
        except ContentError as error:
            raise content_error(error, {}) from error
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to delete the media item: %s", id, exc_info=True)
    return empty()


@media.get("/<id:int>")
@authorize()
async def get_item_details(request: Request, id: int) -> HTTPResponse:
    """Get current file metadata for an accessible media item.

    Args:
        request: Authenticated request with loaded library permissions.
        id: Requested media item ID.

    Returns:
        Details with current NFO, OPF or ComicInfo metadata.
    """
    item = await MediaItemService.get_details(id, request.ctx.user)
    if lib := item.get("lib"):
        # attach the triggers
        lib["triggers"] = await FlowTriggerService.get_triggers(
            GraphCategory.INGEST, lib["id"]
        )
    return json(item, headers={"Cache-Control": "private, no-store"})


@media.get("/<id:int>/assets/cover")
@authorize()
async def get_item_cover(request: Request, id: int) -> HTTPResponse:
    """Serve the current reading cover without accepting a client filesystem path.

    Args:
        request: Authenticated request with loaded library permissions.
        id: Reading item whose server-selected cover is requested.

    Returns:
        Bounded image bytes with their verified MIME type and no browser caching.

    Raises:
        NotFoundException: If the item or an available cover is missing.
        ForbiddenException: If library access is denied.
        KaloscopeException: If source reading fails with a controlled error.
    """
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        cover = await MediaItemService.get_cover(id, request.ctx.user)
    except ContentError as error:
        status = (
            409
            if error.code == "content_changed"
            else 503
            if error.code == "media_source_unavailable"
            else 422
        )
        raise KaloscopeException(
            error.code, status_code=status, headers=headers
        ) from error
    except KaloscopeException as error:
        error.headers = {**error.headers, **headers}
        raise
    if cover is None:
        raise NotFoundException(headers=headers)
    return HTTPResponse(
        cover.data,
        content_type=cover.mime_type,
        headers=headers,
    )


@media.get("/<id:int>/content")
@authorize()
@validate(query=MediaContentQuery)
async def get_item_content(
    request: Request, id: int, query: MediaContentQuery
) -> HTTPResponse:
    """Serve indexed novel content or a bounded page of comic image URLs.

    Args:
        request: Authenticated request with loaded library permissions.
        id: Requested reading item ID.
        query: Optional chapter, expected version and comic pagination.

    Returns:
        Reading content with private caching disabled and a bounded JSON body.

    Raises:
        KaloscopeException: If access, source, index or response limits fail.
    """
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        content = await MediaItemService.get_content(id, request.ctx.user, query)
        response = json(content.model_dump(), headers=headers)
        # bound the complete content payload, including chapter labels
        if len(response.body or b"") > 1024 * 1024:
            raise ContentError("media_limit_exceeded")
        return response
    except ContentError as error:
        raise content_error(error, headers) from error
    except KaloscopeException as error:
        error.headers = {**error.headers, **headers}
        raise


@media.get("/<id:int>/assets/<asset_id:str>")
@authorize()
@validate(query=MediaAssetQuery)
async def get_item_asset(
    request: Request, id: int, asset_id: str, query: MediaAssetQuery
) -> HTTPResponse:
    """Serve one indexed image after revalidating access, source and content version.

    Args:
        request: Authenticated request, optionally carrying If-None-Match.
        id: Source item owning the requested resource.
        asset_id: Opaque image ID from its content response.
        query: Required published content version.

    Returns:
        Verified image bytes, or 304 only after the same access and source checks.

    Raises:
        KaloscopeException: If the source, version, resource or access is invalid.
    """
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    try:
        data, mime = await MediaItemService.get_asset(
            id, request.ctx.user, asset_id, query.version
        )
    except ContentError as error:
        raise content_error(error, headers) from error
    except KaloscopeException as error:
        error.headers = {**error.headers, **headers}
        raise
    etag = f'"{query.version}-{asset_id}"'
    headers.update(
        {"Cache-Control": "private, max-age=0, must-revalidate", "ETag": etag}
    )
    if any(
        tag.strip().removeprefix("W/") in ("*", etag)
        for tag in request.headers.get("if-none-match", "").split(",")
    ):
        return empty(status=304, headers=headers)
    return HTTPResponse(data, content_type=mime, headers=headers)


@media.post("/<id:int>/metadata")
@authorize(role=UserRole.ADMIN)
@validate(json=MediaMetadata)
async def save_metadata(_, body: MediaMetadata, id: int) -> HTTPResponse:
    """Save a confirmed reading candidate before reporting success.

    Args:
        _: Authenticated administrator's request.
        body: Selected workflow and complete metadata candidate.
        id: Existing reading work or comic chapter to update.

    Returns:
        An empty response after file publication and list-summary synchronization.

    Raises:
        KaloscopeException: If the candidate, source or metadata save is invalid.
    """
    headers = {"Cache-Control": "private, no-store"}
    try:
        await MediaItemService.save_metadata(id, body.metadata, overwrite=True)
    except ContentError as error:
        raise content_error(error, headers) from error
    return empty(headers=headers)


@media.post("/<id:int>/gen_nfo")
@authorize(role=UserRole.ADMIN)
@validate(json=MediaMetadata)
async def generate_nfo(_, body: MediaMetadata, id: int) -> HTTPResponse:
    """Validate and save video metadata before refreshing dependent episodes.

    Args:
        _: Authenticated administrator's request.
        body: Selected workflow and metadata candidate.
        id: Top-level video item receiving the metadata.

    Returns:
        An empty response after saving and synchronizing the metadata.

    Raises:
        BadRequestException: If the target is invalid or publication was skipped.
        KaloscopeException: If the candidate cannot be saved safely.
    """
    item = await MediaItem.get_or_none(
        id=id,
        parent_id__isnull=True,
    ).select_related("lib")
    if not item:
        raise BadRequestException
    # overwrite the NFO file and update the metadata immediately
    lib = item.lib
    episode_ids = (
        cast(
            list[int],
            await MediaItem.filter(parent_id=item.id).values_list("id", flat=True),
        )
        if lib.lib_type == LibType.TV_SHOW
        else None
    )
    nfo_type = get_nfo_type(lib.lib_type)
    nfo_path = item.nfo_path or get_nfo_path(item.path)
    try:
        written = await gen_nfo(
            nfo_type,
            nfo_path,
            body.metadata,
            overwrite=True,
            item_id=item.id,
            refresh=True,
        )
    except ContentError as error:
        raise content_error(error) from error
    if not written:
        raise BadRequestException
    # also update the metadata of the child episodes if it's a TV show
    if lib.lib_type == LibType.TV_SHOW:
        await MediaItemService.refresh_episodes(item, body, episode_ids=episode_ids)
    return empty()


@media.get("/title")
@validate(query=MediaResource)
async def get_item_title(_, query: MediaResource) -> HTTPResponse:
    """Extract a scrape title from the media resource path.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    path = Path(query.path)
    return json({"title": extract_title(path.name if path.is_dir() else path.stem)})


@media.get("/probe")
@validate(query=MediaResource)
async def probe_media_metadata(_, query: MediaResource) -> HTTPResponse:
    """Probe media duration and embedded chapters via ffprobe.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    path = query.path
    if not await MediaItem.filter(path=path).exists():
        raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
    metadata = await probe_media(path)
    return json(
        {
            "duration": metadata.duration or 0,
            "chapters": [
                {
                    "id": chapter.id,
                    "title": chapter.title,
                    "start": chapter.start,
                    "end": chapter.end,
                }
                for chapter in metadata.chapters
            ],
        }
    )


@media.get("/stream")
@validate(query=TranscodeQuery)
async def get_item_stream(
    request: Request, query: TranscodeQuery
) -> HTTPResponse | ResponseStream:
    """Get media item stream with optional real-time ffmpeg transcoding.

    Args:
        request: Incoming request with session and application context.
        query: Query filters and options.

    Returns:
        File response or streamed content.
    """
    path = query.path
    if not await MediaItem.filter(path=path).exists():
        raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
    if not await async_os.path.exists(path):
        raise KaloscopeException(ErrorCode.FILE_NOT_EXISTS)

    # -------------------- Transcoding with ffmpeg and HLS --------------------
    if query.transcode:
        options = await query.options()

        # resolve the media hash
        media_hash = await MediaItemService.resolve_media_hash(path)

        # start or wait for the transcoding process to produce the M3U8 output
        media_hash, profile = await ensure_transcode(path, media_hash, options)

        # redirect to the deterministic M3U8 path
        return redirect(f"/_api/media/hls/{media_hash}/{profile}/index.m3u8")

    # -------------------- Direct file streaming --------------------
    stat = await async_os.stat(path)
    total = stat.st_size
    headers = {"Accept-Ranges": "bytes"}

    # get the range header from the request
    range = request.headers.get("Range")
    if range:
        # parse the range header
        match = re.match(r"bytes=(\d*)-(\d*)", range)
        if not match:
            raise InvalidRangeType

        start, end = match.groups()
        start = int(start) if start else 0
        end = min(int(end), total - 1) if end else total - 1

        # validate range
        if start >= total or start > end:
            raise RangeNotSatisfiable

        # stream the requested range
        return await file_stream(
            path,
            headers=headers,
            _range=Range(start=start, end=end, size=end - start + 1, total=total),
        )

    # if no range header, return the entire file
    return await file_stream(path, headers=headers)


@media.get("/hls/<hash>/<profile>/<filename:ext=m3u8|ts>")
async def serve_hls_file(
    _, hash: str, profile: str, filename: str, ext: str
) -> HTTPResponse | ResponseStream:
    """Serve any file from an HLS output directory (M3U8 playlist or TS segment).

    Args:
        _: Unused request.
        hash: Media source hash.
        profile: Transcode profile name.
        filename: Output file name without the extension.
        ext: Output file extension.

    Returns:
        File response or streamed content.
    """
    file_path = (output_dir(hash, profile) / f"{filename}.{ext}").resolve()
    transcoded = Path(KaloscopeConfig.get_workspace("transcoded")).resolve()
    if not file_path.is_relative_to(transcoded):
        raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
    if not file_path.is_file():
        raise KaloscopeException(ErrorCode.FILE_NOT_EXISTS)

    # M3U8 playlist
    if ext == "m3u8":
        content = await read_m3u8(file_path)
        if content is None:
            raise BadRequestException("HLS output not found")
        return HTTPResponse(
            content,
            content_type="application/vnd.apple.mpegurl",
            headers={
                "Accept-Ranges": "none",
                "Cache-Control": "no-cache",
            },
        )

    # TS segment
    return await file_stream(
        file_path,
        headers={"Cache-Control": "no-store"},
    )


@media.get("/proxy")
@validate(query=RemoteProxy)
async def proxy_remote_media(
    request: Request, query: RemoteProxy
) -> HTTPResponse | ResponseStream:
    """Proxy a remote media stream from the given URL.

    Args:
        request: Incoming request with session and application context.
        query: Query filters and options.

    Returns:
        File response or streamed content.
    """
    url, headers = remote_proxy_request(
        query.url, query.referer, query.ua, request.headers
    )
    client: httpx.AsyncClient = request.app.ctx.httpx

    async def _stream(stream):
        """Forward remote media bytes and selected response headers.

        Args:
            stream: Outgoing response stream.
        """
        try:
            async with client.stream("GET", url, headers=headers) as r:
                stream.response.status = r.status_code
                # copy the response headers to the stream response
                for header in PROXY_RESPONSE_HEADERS:
                    if value := r.headers.get(header):
                        stream.response.headers[header.title()] = value
                # iterate over the response content and write it to the stream
                async for chunk in r.aiter_raw():
                    await stream.write(chunk)
        except httpx.RequestError as e:
            logger.error(
                "An error occurred while proxying remote media %s.",
                url,
                exc_info=True,
            )
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED) from e

    return ResponseStream(_stream)


@media.get("/transcode/list")
@validate(query=TranscodeTaskQuery)
async def list_transcodes(_, query: TranscodeTaskQuery) -> HTTPResponse:
    """List in-memory and finished transcode tasks.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    tasks = await list_tasks()

    # attach media item info to tasks if available
    hashes = {task["hash"] for task in tasks if task.get("hash")}
    if hashes:
        items = await MediaItem.filter(hash__in=hashes).values(
            "hash",
            "name",
            "title",
            "path",
            "season",
            "episode",
            parent_name="parent__name",
            parent_title="parent__title",
        )
        hash_items = {}
        for item in items:
            hash_items.setdefault(item["hash"], item)
        for task in tasks:
            if item := hash_items.get(task["hash"]):
                title = item["title"] or item["name"]
                if item["season"] is not None and item["episode"] is not None:
                    title = f"S{item['season']}E{item['episode']} - {title}"
                parent = item["parent_title"] or item["parent_name"]
                task["title"] = parent or title
                if parent:
                    task["subtitle"] = title
                task["path"] = item["path"]

    # filter tasks by state and keyword
    if query.state:
        tasks = [task for task in tasks if task["state"] == query.state]
    if query.keyword:
        keyword = query.keyword.lower()
        tasks = [
            task
            for task in tasks
            if any(
                keyword in value.lower()
                for value in (
                    task.get("title") or task["name"],
                    task.get("subtitle"),
                )
                if value
            )
        ]

    # sort tasks by ordering field
    if query.ordering:
        reverse = query.ordering.startswith("-")
        field = query.ordering[1:] if reverse else query.ordering
        tasks.sort(
            key=lambda task: (task.get(field) is None, task.get(field)),
            reverse=reverse,
        )

    return json(tasks)


@media.post("/transcode/stop")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def stop_transcodes(_, body: IDs) -> HTTPResponse:
    """Stop running transcode tasks by ID.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Response containing the requested data.
    """
    ids = await stop_tasks([str(id) for id in body.ids])
    return json({"ids": ids})


@media.post("/transcode/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_transcodes(_, body: IDs) -> HTTPResponse:
    """Delete non-running transcode outputs by ID.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Response containing the requested data.
    """
    ids = await delete_tasks([str(id) for id in body.ids])
    return json({"ids": ids})
