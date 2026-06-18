import sys
import tomllib
from pathlib import Path

from sanic import Blueprint, HTTPResponse, json

# subroutes for all system related operations
system = Blueprint("system", url_prefix="/system")

# path to the project's pyproject.toml (backend/pyproject.toml)
_PYPROJECT = Path(__file__).parents[2] / "pyproject.toml"


@system.get("/platform")
async def get_platform(_) -> HTTPResponse:
    """Get the current platform."""
    return json({"platform": sys.platform})


@system.get("/version")
async def get_app_version(_) -> HTTPResponse:
    """Get the current application version."""
    try:
        with open(_PYPROJECT, "rb") as f:
            data = tomllib.load(f)
        version = data["project"]["version"]
    except Exception:
        version = ""
    return json({"version": version})
