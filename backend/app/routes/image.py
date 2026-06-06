import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import aiofiles
import httpx
from pydantic import BaseModel, Field
from sanic import Blueprint, HTTPResponse, Request, empty, raw
from sanic.response import ResponseStream
from sanic_ext import validate

from app.core.config import KaloscopeConfig

# subroutes for all image related operations
image = Blueprint("image")

# define the response headers that we want to copy from the proxied request
RESPONSE_HEADERS = [
    "content-type",
    "content-length",
    "content-encoding",
    "last-modified",
    "cache-control",
    "expires",
    "etag",
]


@image.get("/<dir>/<filename:ext=jpg|jpeg|png|gif|webp>")
async def get_image(_, dir: str, filename: str, ext: str) -> HTTPResponse:
    """Get an image from the given path."""
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


class ImageProxy(BaseModel):
    """Request model for proxying an image."""

    url: str = Field(min_length=1)
    store: bool = False


@image.get("/image/proxy")
@validate(query=ImageProxy)
async def proxy_image(
    request: Request, query: ImageProxy
) -> HTTPResponse | ResponseStream:
    """Proxy an image from the given URL."""
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
    # adjust the request headers
    headers = dict(request.headers)
    headers["host"] = urlparse(url).netloc
    headers["referer"] = url
    headers.pop("cookie", None)
    headers.pop("authorization", None)
    client: httpx.AsyncClient = request.app.ctx.httpx

    async def _stream(stream):
        async with client.stream("GET", url, headers=headers) as r:
            stream.response.status = r.status_code
            # copy the response headers to the stream response
            for header in RESPONSE_HEADERS:
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

    return ResponseStream(_stream)
