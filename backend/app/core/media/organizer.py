import asyncio
import hashlib
import mimetypes
import os
import tempfile
from contextlib import suppress
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from lxml import etree
from tortoise.transactions import in_transaction

from app.models.download import DownloadTask
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.utils.disk import rename_exclusive

_SUBTITLES = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".lrc"}


class OrganizePendingError(RuntimeError):
    """A persisted organization must finish before this library is scanned."""


def _safe_path(root: Path, path: Path):
    """Validate a path before accessing it during organization.

    Args:
        root: The absolute media library root.
        path: The absolute source or destination path to validate.

    Raises:
        ValueError: If the path escapes the library, exceeds path limits, or
            traverses a directory symlink.
    """
    if not path.is_absolute() or not path.is_relative_to(root):
        raise ValueError(f"path is outside the library: {path}")
    if ".." in path.relative_to(root).parts:
        raise ValueError("parent traversal is not allowed")
    if len(str(path)) > 4096 or any(
        len(part.encode("utf-8")) > 255 for part in path.relative_to(root).parts
    ):
        raise ValueError("path exceeds filesystem or database limits")
    for ancestor in (root, *path.parents):
        if ancestor.is_relative_to(root) and ancestor.is_symlink():
            raise ValueError(f"directory symlink is not allowed: {ancestor}")
    if path.is_symlink() and path.is_dir():
        raise ValueError(f"directory symlink is not allowed: {path}")


def _metadata(path: Path, lib_type: str, root_tag: str) -> dict:
    """Extract organization metadata from a complete NFO document.

    Args:
        path: The NFO file path.
        lib_type: The library type used to select a metadata handler.
        root_tag: The required XML root element name.

    Raises:
        OSError: If the NFO file cannot be read.
        etree.LxmlError: If the NFO document cannot be parsed.
        ValueError: If the root element is unexpected or the title is missing.

    Returns:
        The extracted metadata, including the NFO path.
    """
    # require a complete XML document before changing files
    from app.core.media.handlers.base import get_handler

    tree = etree.parse(
        path,
        etree.XMLParser(recover=False, resolve_entities=False, no_network=True),
    )
    if tree.getroot().tag != root_tag:
        raise ValueError(f"unexpected NFO type: {path}")
    metadata = get_handler(lib_type).extract_meta(tree)
    metadata.nfo_path = str(path)
    data = asdict(metadata)
    if not data.get("title"):
        raise ValueError(f"NFO has no title: {path}")
    return data


def _identity(metadata: dict) -> tuple | None:
    """Get the external identity used to match media directories.

    Args:
        metadata: The parsed media metadata.

    Returns:
        The NFO source and unique ID, or `None` if no unique ID is available.
    """
    value = metadata.get("unique_id")
    return (metadata.get("nfo_source"), value) if value else None


def _fingerprint(path: Path) -> list[int]:
    """Read file attributes used to detect changes during recovery.

    Args:
        path: The file or symlink path to inspect without following the link.

    Returns:
        A list containing the device ID, inode number, byte size, and modification
        time in nanoseconds.
    """
    stat = path.lstat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _companions(path: Path) -> list[Path]:
    """Find the NFO and subtitle files associated with a video basename.

    Args:
        path: The video file path.

    Returns:
        The matching files and symlinks in the video's directory, accepting
        case-insensitive extensions and excluding subtitles with a longer
        matching video basename.
    """
    siblings = [
        sibling
        for sibling in path.parent.iterdir()
        if sibling.is_file() or sibling.is_symlink()
    ]
    prefix = f"{path.stem}."
    other_video_prefixes = tuple(
        f"{sibling.stem}."
        for sibling in siblings
        if sibling.stem.startswith(prefix)
        and (mimetypes.guess_file_type(sibling)[0] or "").startswith("video/")
    )
    return [
        sibling
        for sibling in siblings
        if (sibling.stem == path.stem and sibling.suffix.lower() == ".nfo")
        or (
            sibling.suffix.lower() in _SUBTITLES
            and sibling.name.startswith(prefix)
            and not sibling.name.startswith(other_video_prefixes)
        )
    ]


def _context(metadata: dict, parent: dict | None) -> dict:
    """Build template values from media and optional parent metadata.

    Args:
        metadata: The media item's parsed metadata.
        parent: The parent show's metadata, or `None` for a standalone item.

    Returns:
        A metadata copy with show fields and missing values inherited from the
        parent where supported.
    """
    result = dict(metadata)
    if parent:
        result["show_title"] = parent.get("title")
        result["show_originaltitle"] = parent.get("originaltitle")
        result["show_year"] = parent.get("year")
        for name in ("year", "season", "nfo_source"):
            if result.get(name) is None:
                result[name] = parent.get(name)
    return result


def _move_files(root: Path, payload: dict):
    """Apply or resume a journal's filesystem changes under the library lock.

    Preserve recorded permissions when publishing copied NFOs. Accept legacy
    journals without a recorded `mode`, retaining their private temporary-file mode.

    Args:
        root: The absolute media library root.
        payload: The persisted organization plan with file identities and edits.

    Raises:
        OrganizePendingError: If a file changed or disappeared unexpectedly.
        ValueError: If a journal path is no longer safe to access.
        OSError: If a filesystem operation fails or a destination already exists.
    """
    for move in payload["moves"]:
        source, destination = Path(move["src"]), Path(move["dst"])
        _safe_path(root, source)
        _safe_path(root, destination)
        if source.exists() or source.is_symlink():
            if _fingerprint(source) != move["identity"]:
                raise OrganizePendingError(
                    f"source changed during organization: {source}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            _safe_path(root, destination)
            rename_exclusive(source, destination)
        elif not (destination.exists() or destination.is_symlink()):
            raise OrganizePendingError(f"source and destination are missing: {source}")
        elif _fingerprint(destination) != move["identity"]:
            # allow rewritten NFOs to have a new inode when their contents match
            edit = next(
                (
                    edit
                    for edit in payload["nfo_edits"]
                    if edit["path"] == str(destination)
                ),
                None,
            )
            link = next(
                (
                    link
                    for link in payload["symlinks"]
                    if link["path"] == str(destination)
                ),
                None,
            )
            fixed_link = (
                link
                and destination.is_symlink()
                and os.readlink(destination) == link["target"]
            )
            if not fixed_link and (
                edit is None or destination.read_bytes() != edit["content"].encode()
            ):
                raise OrganizePendingError(f"destination was replaced: {destination}")
    for create in payload["creates"]:
        path = Path(create["path"])
        _safe_path(root, path)
        content = create["content"].encode()
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_bytes() != content:
                raise OrganizePendingError(f"copied NFO destination changed: {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(root, path)
        descriptor, name = tempfile.mkstemp(prefix=".organizing-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if (mode := create.get("mode")) is not None:
                    os.fchmod(stream.fileno(), mode)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            rename_exclusive(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    for link in payload["symlinks"]:
        path = Path(link["path"])
        _safe_path(root, path)
        current = os.readlink(path)
        if current == link["target"]:
            continue
        if current != link["before"]:
            raise OrganizePendingError(f"symlink changed during organization: {path}")
        descriptor, name = tempfile.mkstemp(prefix=".organizing-", dir=path.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            temporary.unlink()
            temporary.symlink_to(link["target"])
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    for edit in payload["nfo_edits"]:
        path = Path(edit["path"])
        _safe_path(root, path)
        current = path.read_bytes()
        content = edit["content"].encode()
        if current == content:
            continue
        if hashlib.sha256(current).hexdigest() != edit["before"]:
            raise OrganizePendingError(f"NFO changed during organization: {path}")
        # replace only the original NFO contents verified against the journal
        descriptor, name = tempfile.mkstemp(prefix=".organizing-", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


async def _write_in_thread(function, *args):
    """Run a filesystem writer without releasing its lock on cancellation.

    The caller holds the library lock until this function finishes waiting for
    the worker thread, including when the calling task is cancelled.

    Args:
        function: The synchronous filesystem operation to run in a worker thread.
        *args: The positional arguments passed to the operation.

    Raises:
        asyncio.CancelledError: If cancelled, after the worker has stopped, even
            if its filesystem operation fails.

    Returns:
        The result of the filesystem operation.
    """
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # keep the caller's library lock until its filesystem writer has stopped
        while not task.done():
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        with suppress(asyncio.CancelledError, Exception):
            task.result()
        raise


async def _finish(lib: MediaLib, event: MediaEvent) -> dict[str, str]:
    """Finish a journal's filesystem and database updates under the library lock.

    Leave episode NFOs eligible for scanning when their complete metadata was
    not included in the journal, including plans persisted by older versions.
    Retain filesystem events for normal consumption because external writers can
    reuse source paths or change destinations before the database commit.

    Args:
        lib: The media library whose lock is held by the caller.
        event: The persisted organization event containing the plan to finish.

    Raises:
        OrganizePendingError: If the plan cannot be completed and needs recovery.

    Returns:
        The mapping from original media paths to their organized destinations.
    """
    payload = event.payload
    try:
        await _write_in_thread(_move_files, Path(lib.dir).absolute(), payload)
        async with in_transaction("default"):
            parent = payload["parent"]
            parent_id = None
            if parent:
                data = {key: value for key, value in parent.items() if key != "id"}
                data["nfo_mtime"] = datetime.fromtimestamp(
                    Path(data["nfo_path"]).stat().st_mtime, tz=UTC
                )
                if parent["id"] is None:
                    row, _ = await MediaItem.get_or_create(
                        lib_id=lib.id, path=data.pop("path"), defaults=data
                    )
                    parent_id = row.id
                else:
                    await MediaItem.filter(id=parent["id"], lib_id=lib.id).update(
                        **data
                    )
                    parent_id = parent["id"]
            for update in payload["updates"]:
                data = {key: value for key, value in update.items() if key != "id"}
                if data.get("parent_id") == "target":
                    data["parent_id"] = parent_id
                if data.get("nfo_path") and Path(data["nfo_path"]).exists():
                    if lib.lib_type == LibType.TV_SHOW and "title" not in data:
                        data["nfo_mtime"] = None
                    else:
                        data["nfo_mtime"] = datetime.fromtimestamp(
                            Path(data["nfo_path"]).stat().st_mtime, tz=UTC
                        )
                await MediaItem.filter(id=update["id"], lib_id=lib.id).update(**data)
            if (
                payload["delete_parent"]
                and not await MediaItem.filter(
                    parent_id=payload["delete_parent"]
                ).exists()
            ):
                await MediaItem.filter(id=payload["delete_parent"]).delete()
            file_moves = {move["src"]: move["dst"] for move in payload["moves"]}
            for task in await DownloadTask.filter(transfer_lib_id=lib.id):
                targets = task.transfer_targets or {}
                updated = {
                    key: file_moves.get(value, value) for key, value in targets.items()
                }
                if updated != targets:
                    await DownloadTask.filter(id=task.id).update(
                        transfer_targets=updated
                    )
            await event.delete()
        cleanup = payload.get("cleanup")
        if cleanup:
            await _write_in_thread(_remove_empty_directories, Path(cleanup))
        return payload["mapping"]
    except Exception as error:
        raise OrganizePendingError(
            f"organization {event.id} needs recovery "
            f"before library {lib.id} can continue"
        ) from error


def _remove_empty_directories(path: Path):
    """Remove empty directories from the source tree after organization.

    Args:
        path: The source directory to clean up from its leaves to its root.
    """
    if not path.exists():
        return
    for directory, _, _ in os.walk(path, topdown=False, followlinks=False):
        with suppress(OSError):
            Path(directory).rmdir()


async def recover_organizing(lib: MediaLib) -> dict[str, str]:
    """Finish persisted plans before accepting filesystem events or scan results.

    The caller must hold the library lock throughout recovery.

    Args:
        lib: The media library whose pending organization events are recovered.

    Raises:
        OrganizePendingError: If a persisted plan cannot be completed.

    Returns:
        The combined mapping from original paths to recovered destinations.
    """
    mapping = {}
    for event in await MediaEvent.filter(lib_id=lib.id, event_type="organize"):
        mapping.update(await _finish(lib, event))
    return mapping
