import os
import stat
import sys
from mimetypes import guess_file_type
from pathlib import Path

from pydantic import BaseModel
from sanic import Blueprint, HTTPResponse, json
from sanic_ext import validate

from app.core.exceptions import BadRequestException, ErrorCode, ForbiddenException
from app.utils.disk import disk_usage, format_bytes

# the concatenation of the drive and root
ANCHOR = Path(__file__).anchor

# subroutes for all filesystem related operations
filesystem = Blueprint("filesystem", url_prefix="/filesystem")


class ListRequest(BaseModel):
    """Request model for listing files in a directory."""

    path: str | None = None
    only_dirs: bool = False
    expand_to: str | None = None


@filesystem.get("/list")
@validate(query=ListRequest)
async def list_files(_, query: ListRequest) -> HTTPResponse:
    """List the files in the given directory."""
    path = Path(query.path or ANCHOR)
    if not os.access(path, os.R_OK) or not path.is_dir():
        raise BadRequestException
    expand_to = Path(query.expand_to) if query.expand_to else None
    if expand_to is not None and not os.access(expand_to, os.R_OK):
        expand_to = None

    # check if the path is readable under the current listing filter
    def _readable(p: Path, *, only_dirs: bool) -> bool:
        try:
            return os.access(p, os.R_OK) and (not only_dirs or p.is_dir())
        except OSError:
            return False

    # check if the directory can be expanded in the current listing filter
    def _expandable(p: Path) -> bool:
        try:
            return any(
                _readable(child, only_dirs=query.only_dirs) for child in p.iterdir()
            )
        except OSError:
            return False

    # check if the directory is physically empty
    def _is_empty(p: Path) -> bool:
        try:
            return next(p.iterdir(), None) is None
        except OSError:
            return False

    # check if the file is hidden
    def _is_hidden(p: Path) -> bool:
        if sys.platform == "win32":
            try:
                return bool(p.stat().st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN)
            except OSError:
                return False
        return p.name.startswith(".")

    # recursively build the file entry
    def _build_entry(p: Path) -> dict:
        absolute_path = str(p.resolve())
        is_dir = p.is_dir()
        is_empty = _is_empty(p) if is_dir else None
        expandable = _expandable(p) if is_dir else False
        entry = {
            "name": p.name,
            "path": absolute_path,
            "is_dir": is_dir,
            "is_empty": is_empty,
            "is_hidden": _is_hidden(p),
            "expandable": expandable,
            "file_type": guess_file_type(p)[0] if p.is_file() else None,
            "open": False,
        }
        # expand children along the expand_to path
        if expand_to is not None:
            if not expandable:
                return entry
            if expand_to.is_relative_to(absolute_path):
                children = sorted(
                    filter(
                        lambda child: _readable(child, only_dirs=query.only_dirs),
                        p.iterdir(),
                    ),
                    key=lambda f: (not f.is_dir(), f.name),
                )
                entry["children"] = [_build_entry(f) for f in children]
                entry["open"] = True
        return entry

    files = filter(
        lambda child: _readable(child, only_dirs=query.only_dirs), path.iterdir()
    )
    return json(
        [
            _build_entry(file)
            for file in sorted(files, key=lambda f: (not f.is_dir(), f.name))
        ]
    )


class StatsRequest(BaseModel):
    """Request model for getting path statistics."""

    path: str


@filesystem.get("/stats")
@validate(query=StatsRequest)
async def get_stats(_, query: StatsRequest) -> HTTPResponse:
    """Get the statistics of the given path."""
    path = Path(query.path)
    if not os.access(path, os.R_OK):
        raise BadRequestException

    absolute_path = str(path.resolve())
    stats = {
        "name": path.name,
        "path": absolute_path,
        "is_dir": path.is_dir(),
        "readable": True,  # os.access(path, os.R_OK),
        "writable": os.access(path, os.W_OK),
        "size": format_bytes(path.stat().st_size),
    }
    if path.is_dir():
        usage = disk_usage(absolute_path)
        stats["total"] = usage.total_space()
        stats["used"] = usage.used_space()
        stats["free"] = usage.free_space()
    return json(stats)


class CreateDirRequest(BaseModel):
    """Request model for creating a directory."""

    parent: str
    name: str


@filesystem.post("/mkdir")
@validate(json=CreateDirRequest)
async def create_dir(_, body: CreateDirRequest) -> HTTPResponse:
    """Create a child directory in the given parent directory."""
    parent = Path(body.parent)
    name = body.name.strip()
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
        or len(Path(name).parts) != 1
    ):
        raise BadRequestException
    if not parent.is_dir():
        raise BadRequestException
    if not os.access(parent, os.R_OK | os.W_OK | os.X_OK):
        raise ForbiddenException(ErrorCode.PERMISSION_DENIED)

    path = parent / name
    if path.exists():
        raise BadRequestException(ErrorCode.DUPLICATE_DIRECTORY)

    try:
        path.mkdir()
    except OSError as exc:
        raise BadRequestException from exc
    return json(str(path.resolve()))


class DeleteDirRequest(BaseModel):
    """Request model for deleting a directory."""

    path: str


@filesystem.post("/rmdir")
@validate(json=DeleteDirRequest)
async def delete_dir(_, body: DeleteDirRequest) -> HTTPResponse:
    """Delete an empty directory and return its parent directory."""
    path = Path(body.path)
    parent = path.parent
    if not path.is_dir() or parent == path:
        raise BadRequestException
    if not os.access(parent, os.W_OK | os.X_OK):
        raise ForbiddenException(ErrorCode.PERMISSION_DENIED)

    try:
        path.rmdir()
    except OSError as exc:
        raise BadRequestException from exc
    return json(str(parent.resolve()))
