import asyncio
import re
import sys
import tomllib
from pathlib import Path

import httpx
from sanic import Blueprint, HTTPResponse, Request, json

from app.core.exceptions import ErrorCode, KaloscopeException

# path to the project's pyproject.toml (backend/pyproject.toml)
_PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"

system = Blueprint("system", url_prefix="/system")


@system.get("/platform")
async def get_platform(_) -> HTTPResponse:
    """Get current platform.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    return json({"platform": sys.platform})


@system.get("/version")
async def get_app_version(request: Request) -> HTTPResponse:
    """Get the current version and optionally check Docker Hub for updates.

    Args:
        request: Request with an optional `check_update=true` query parameter.

    Returns:
        Current version, plus the latest stable version and update availability
        when an update check is requested.

    Raises:
        KaloscopeException: If the update check fails or versions are unavailable.
    """
    try:
        with open(_PYPROJECT, "rb") as f:
            data = tomllib.load(f)
        version = data["project"]["version"]
    except Exception:
        version = ""
    result: dict[str, str | bool] = {"version": version}
    if request.args.get("check_update") == "true":
        try:
            if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
                raise ValueError("Current version is unavailable")
            versions = []
            page = 1
            client: httpx.AsyncClient = request.app.ctx.httpx
            async with asyncio.timeout(10):
                while True:
                    response = await client.get(
                        "https://hub.docker.com/v2/namespaces/kaloscope/repositories/kaloscope/tags",
                        params={"page_size": 100, "page": page},
                        headers={"Cache-Control": "no-cache"},
                        timeout=10,
                    )
                    response.raise_for_status()
                    tags = response.json()
                    for tag in tags["results"]:
                        if re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", tag["name"]):
                            versions.append(
                                tuple(
                                    map(int, tag["name"].removeprefix("v").split("."))
                                )
                            )
                    if not tags["next"]:
                        break
                    page += 1
            latest = max(versions)
            result["latest_version"] = ".".join(map(str, latest))
            result["has_update"] = latest > tuple(map(int, version.split(".")))
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            raise KaloscopeException(
                ErrorCode.HTTP_REQUEST_FAILED, status_code=502
            ) from exc
    return json(result)
