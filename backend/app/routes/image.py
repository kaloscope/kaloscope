import mimetypes
from pathlib import Path

import aiofiles
import httpx
from sanic import Blueprint, HTTPResponse, Request, empty, raw
from sanic.log import logger
from sanic.response import ResponseStream
from sanic_ext import validate

from app.core.config import KaloscopeConfig
from app.core.exceptions import ErrorCode, KaloscopeException
from app.utils.proxy import (
    PROXY_RESPONSE_HEADERS,
    RemoteProxy,
    proxy_policy,
    proxy_stream,
    remote_proxy_request,
)

image = Blueprint("image")


@image.get("/<dir>/<filename:ext=jpg|jpeg|png|gif|webp>")
async def get_image(_, dir: str, filename: str, ext: str) -> HTTPResponse:
    """Get an image from the given path.

    Args:
        _: Unused request.
        dir: Image cache subdirectory.
        filename: Output file name without the extension.
        ext: Output file extension.

    Returns:
        Response containing the requested data.
    """
    image_dir = Path(KaloscopeConfig.get_workspace("images")) / dir
    image_path = image_dir / f"{filename}.{ext}"
    if not image_path.exists():
        return empty(status=404)
    # set the content type based on the file extension
    mime_type, _ = mimetypes.guess_file_type(image_path)
    content_type = mime_type or "application/octet-stream"
    # return the image with a cache control header
    async with aiofiles.open(image_path, "rb") as f:
        return raw(
            await f.read(),
            content_type=content_type,
            headers={"Cache-Control": "max-age=31536000"},
        )


@image.get("/image/proxy")
@validate(query=RemoteProxy)
async def proxy_remote_image(
    request: Request, query: RemoteProxy
) -> HTTPResponse | ResponseStream:
    """Proxy a remote image from the given URL.

    Args:
        request: Incoming request with session and application context.
        query: Query filters and options.

    Returns:
        File response or streamed content.

    Raises:
        ForbiddenException: If the URL is not absolute HTTP(S), or a non-admin
            account targets or is redirected to a non-public address.
    """
    url = query.url
    store = query.store

    # check if the URL starts with http:// or https://
    http = url.lower().startswith("http://")
    https = url.lower().startswith("https://")

    # try to load the image from the local workspace
    local_path = Path(KaloscopeConfig.get_workspace("images"))
    local_path = local_path / (url[7:] if http else url[8:] if https else url)
    if local_path.exists() and local_path.is_file() and local_path.stat().st_size > 0:
        # guess the MIME type based on the file extension
        mime_type, _ = mimetypes.guess_file_type(local_path)
        content_type = mime_type or "application/octet-stream"
        async with aiofiles.open(local_path, "rb") as f:
            return raw(
                await f.read(),
                content_type=content_type,
                headers={"Cache-Control": "max-age=31536000"},
            )

    # request the image from the URL if it is not a local file
    if not (http or https):
        return empty(status=404)

    url, headers = remote_proxy_request(url, query.referer, query.ua, request.headers)
    public = await proxy_policy(request, url)
    client: httpx.AsyncClient = request.app.ctx.httpx

    async def _stream(stream):
        """Forward remote image bytes and optionally save a local copy.

        Args:
            stream: Outgoing response stream.
        """
        try:
            async with proxy_stream(client, url, headers, public) as r:
                stream.response.status = r.status_code
                # copy the response headers to the stream response
                for header in PROXY_RESPONSE_HEADERS:
                    if store and header == "content-encoding":
                        continue
                    if value := r.headers.get(header):
                        stream.response.headers[header.title()] = value
                # iterate over the response content and write it to the stream
                if store:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    async with aiofiles.open(local_path, "wb") as f:
                        async for chunk in r.aiter_bytes():
                            await stream.write(chunk)
                            await f.write(chunk)
                else:
                    async for chunk in r.aiter_raw():
                        await stream.write(chunk)
        except httpx.RequestError as e:
            logger.error(
                "An error occurred while proxying remote image %s.",
                url,
                exc_info=True,
            )
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED) from e

    return ResponseStream(_stream)
