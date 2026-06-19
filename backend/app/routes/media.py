import re
from pathlib import Path

from aiofiles import os as async_os
from sanic import Blueprint, HTTPResponse, Request, empty, json, redirect
from sanic.exceptions import InvalidRangeType, RangeNotSatisfiable
from sanic.log import logger
from sanic.response import ResponseStream, file_stream
from sanic_ext import validate
from tortoise.expressions import Q, RawSQL

from app.core.decorators import authorize
from app.core.exceptions import BadRequestException, ErrorCode, KaloscopeException
from app.core.media.shelver import (
    gen_nfo,
    get_nfo_path,
    get_nfo_type,
    parse_nfo,
    update_metadata,
)
from app.core.media.watcher import LibWatcher
from app.models.base import IDs, Range
from app.models.flow import GraphCategory
from app.models.media import (
    LibType,
    MediaDel,
    MediaItem,
    MediaLib,
    MediaLibUpsert,
    MediaMetadata,
    MediaQuery,
    MediaResource,
    TranscodeQuery,
)
from app.models.user import UserInfo, UserRole
from app.services.flow import FlowTriggerService
from app.services.media import MediaItemService, MediaLibService
from app.utils.extractor import extract_title
from app.utils.transcode import (
    ensure_transcode,
    output_dir,
    probe_duration,
    read_m3u8,
)

# subroutes for all media related operations
media = Blueprint("media", url_prefix="/media")


@media.get("/lib/list")
@authorize()
async def list_libraries(request: Request) -> HTTPResponse:
    """List the media libraries."""
    queries = []
    # filter the libraries by the user's permissions
    user: UserInfo = request.ctx.user
    if user.perms is not None:
        queries.append(Q(id__in=user.perms.media_lib_ids))
    # list the libraries without pagination
    media_libs = await MediaLibService.dump_list(MediaLib.filter(*queries))
    # attach the triggers and scanning status for each library
    watcher: LibWatcher = request.app.ctx.lib_watcher
    for lib in media_libs:
        lib["triggers"] = await FlowTriggerService.get_triggers(
            GraphCategory.INGEST, lib["id"]
        )
        lib["scanning"] = watcher.is_scanning(lib["dir"])
    return json(media_libs)


@media.post("/lib/sort")
@validate(json=IDs)
async def sort_libraries(_, body: IDs) -> HTTPResponse:
    """Sort the media libraries."""
    await MediaLibService.update_priorities(body.ids)
    return empty()


@media.post("/lib/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=MediaLibUpsert)
async def upsert_library(_, body: MediaLibUpsert) -> HTTPResponse:
    """Create or update a media library."""
    lib = await MediaLibService.upsert(body)
    return json(await MediaLibService.dump(lib))


@media.post("/lib/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_libraries(_, body: IDs) -> HTTPResponse:
    """Delete the media libraries."""
    for id in body.ids:
        try:
            await MediaLibService.delete(int(id))
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to delete the media library: %s", id, exc_info=True)
    return empty()


@media.get("/lib/<id:int>/scan")
async def scan_library(request: Request, id: int) -> HTTPResponse:
    """Scan the media library."""
    lib = await MediaLib.get(id=id)
    watcher: LibWatcher = request.app.ctx.lib_watcher
    await watcher.scan_directory(lib, valid=True)
    return empty()


@media.get("/list")
@validate(query=MediaQuery)
async def list_items(_, query: MediaQuery) -> HTTPResponse:
    """List the media items."""
    queries = [
        # only list the top-level items if no path is specified
        Q(path=query.path) if query.path else Q(visible=True, parent_id__isnull=True)
    ]
    if query.lib_id:
        queries.append(Q(lib_id=query.lib_id))
    if query.keyword:
        queries.append(Q(keyword__icontains=query.keyword))
    page = await MediaItem.page(
        *queries,
        **query.page_params,
        annotations={"keyword": RawSQL("IFNULL(title, name)")},
    )
    return json(
        await MediaItemService.dump_page(page, exclude={"lib", "parent", "children"})
    )


@media.post("/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=MediaDel)
async def delete_items(_, body: MediaDel) -> HTTPResponse:
    """Delete the media items."""
    for id in body.ids:
        try:
            await MediaItemService.delete(int(id), body.local)
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to delete the media item: %s", id, exc_info=True)
    return empty()


@media.get("/<id:int>")
async def get_item_details(_, id: int) -> HTTPResponse:
    """Get the details of the media item."""
    item = await MediaItemService.dump(await MediaItem.get(id=id))
    if lib := item.get("lib"):
        # attach the triggers
        lib["triggers"] = await FlowTriggerService.get_triggers(
            GraphCategory.INGEST, lib["id"]
        )
        # attach the metadata
        if nfo_path := item.get("nfo_path"):
            item["metadata"] = parse_nfo(lib["lib_type"], nfo_path)
    return json(item)


@media.post("/<id:int>/gen_nfo")
@authorize(role=UserRole.ADMIN)
@validate(json=MediaMetadata)
async def generate_nfo(_, body: MediaMetadata, id: int) -> HTTPResponse:
    """Generate the NFO file for the media item."""
    item = await MediaItem.get_or_none(
        id=id,
        parent_id__isnull=True,
    ).select_related("lib")
    if not item:
        raise BadRequestException
    # overwrite the NFO file and update the metadata immediately
    lib = item.lib
    nfo_type = get_nfo_type(lib.lib_type)
    nfo_path = item.nfo_path or get_nfo_path(item.path)
    if await gen_nfo(nfo_type, nfo_path, body.metadata, overwrite=True):
        await update_metadata(lib, nfo_path, alternative=body.metadata)
    # also update the metadata of the child episodes if it's a TV show
    if lib.lib_type == LibType.TV_SHOW:
        await MediaItemService.refresh_episodes(item, body)
    return empty()


@media.get("/title")
@validate(query=MediaResource)
async def get_item_title(_, query: MediaResource) -> HTTPResponse:
    """Extract a scrape title from the media resource path."""
    path = Path(query.path)
    return json({"title": extract_title(path.name if path.is_dir() else path.stem)})


@media.get("/probe")
@validate(query=MediaResource)
async def probe_media_duration(_, query: MediaResource) -> HTTPResponse:
    """Probe the media file duration via ffprobe."""
    duration = await probe_duration(query.path)
    return json({"duration": duration or 0})


@media.get("/stream")
@validate(query=TranscodeQuery)
async def get_item_stream(
    request: Request, query: TranscodeQuery
) -> HTTPResponse | ResponseStream:
    """Get the media item stream with optional real-time ffmpeg transcoding."""
    path = query.path
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
        end = int(end) if end else total - 1

        # validate range
        if start >= total or end >= total or start > end:
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
    """Serve any file from an HLS output directory (M3U8 playlist or TS segment)."""
    # check if the requested file exists in the output directory
    file_path = output_dir(hash, profile) / f"{filename}.{ext}"
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
