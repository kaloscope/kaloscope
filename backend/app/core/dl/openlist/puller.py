import asyncio
import ctypes
import errno
import hashlib
import logging
import os
import shutil
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG
from time import monotonic
from typing import Literal

import aiofiles
import httpx

from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.manifest import RemoteManifestEntry, normalize_relative_path
from app.core.dl.openlist.models import OpenListErrorKind, RemoteLink
from app.models.download import OfflineDownloadErrorKind

_PROXY_SEGMENTS = frozenset({"p", "d", "ap", "ad", "ae", "sd", "sad"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HASH_ALGORITHMS = frozenset({"md5", "sha1", "sha256", "sha512"})
_HEX_DIGITS = frozenset("0123456789abcdef")
_HASH_CHUNK_SIZE = 1024 * 1024
_EXPIRED_LINK_STATUSES = frozenset({401, 403, 410})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
_MAX_ATTEMPTS = 3
_MAX_LINK_REFRESHES = 2
_MAX_REDIRECTS = 5
_PROGRESS_INTERVAL = 1.0
_HTTPX_REQUEST_LOG = 'HTTP Request: %s %s "%s %d %s"'
_HARDLINK_UNAVAILABLE = frozenset(
    {
        errno.EPERM,
        errno.EXDEV,
        errno.ENOSYS,
        errno.EOPNOTSUPP,
    }
)

type DataHeaders = Mapping[str, str | Sequence[str]]
type ProgressCallback = Callable[[int, int, int], Awaitable[None]]


class PullError(Exception):
    """Represent a classified local result-transfer failure.

    Args:
        kind: The persisted offline-download failure category.
        message: The safe user-facing failure message.
    """

    def __init__(self, kind: OfflineDownloadErrorKind, message: str):
        self.kind = kind
        super().__init__(message)


class PullFailedError(PullError):
    def __init__(self):
        super().__init__(
            OfflineDownloadErrorKind.PULL_FAILED,
            "File download failed",
        )


class InvalidLocalPathError(PullError):
    def __init__(self):
        super().__init__(
            OfflineDownloadErrorKind.LOCAL_PATH_INVALID,
            "Local path is invalid",
        )


class LocalFileConflictError(PullError):
    def __init__(self):
        super().__init__(
            OfflineDownloadErrorKind.LOCAL_FILE_CONFLICT,
            "Local file conflicts with existing data",
        )


class DirectLinkUnavailableError(PullError):
    def __init__(self):
        super().__init__(
            OfflineDownloadErrorKind.DIRECT_LINK_UNAVAILABLE,
            "Direct link is unavailable",
        )


class _RetryPull(Exception):
    """Signal that the current direct-link request should be retried."""


class _RefreshLink(Exception):
    """Signal that an expired direct link should be refreshed."""


class _HttpxURLFilter(logging.Filter):
    """Remove credentials and signed query values from HTTPX request logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            record.msg == _HTTPX_REQUEST_LOG
            and isinstance(args, tuple)
            and len(args) == 5
            and isinstance(args[1], httpx.URL)
        ):
            safe_url = args[1].copy_with(userinfo=None, query=None, fragment=None)
            record.args = (args[0], safe_url, *args[2:])
        return True


_HTTPX_URL_FILTER = _HttpxURLFilter()


class _ProgressReporter:
    """Throttle per-file progress callbacks and calculate interval speed."""

    def __init__(
        self,
        callback: ProgressCallback | None,
        total: int,
        completed: int,
    ):
        self._callback = callback
        self._total = total
        self._completed = completed
        self._reported_at = monotonic()
        self._emitted: int | None = None

    async def report(self, completed: int, *, final: bool = False):
        """Report current progress when the throttle interval has elapsed.

        Args:
            completed: The current completed size in bytes.
            final: Whether to emit the final value immediately.
        """
        if self._callback is None:
            return
        now = monotonic()
        elapsed = max(now - self._reported_at, 0)
        if (not final and elapsed < _PROGRESS_INTERVAL) or (
            final and self._emitted == completed
        ):
            return
        speed = int(max(completed - self._completed, 0) / elapsed) if elapsed else 0
        await self._callback(completed, self._total, speed)
        self._completed = completed
        self._reported_at = now
        self._emitted = completed


@dataclass(frozen=True, slots=True)
class LocalFileTarget:
    """Describe the final and resumable paths for one local file."""

    final_path: Path
    part_path: Path
    marker_path: Path
    offset: int


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    host = (url.host or "").lower().rstrip(".")
    return url.scheme.lower(), host, url.port or _DEFAULT_PORTS.get(url.scheme)


def _first_segment(path: str) -> str:
    return next(
        (part.lower() for part in path.replace("\\", "/").split("/") if part),
        "",
    )


def _is_proxy_path(path: str, base_path: str) -> bool:
    path = path.replace("\\", "/")
    candidates = [path]
    base_path = base_path.replace("\\", "/").rstrip("/")
    if base_path and base_path != "/":
        normalized_path = path.lower()
        normalized_base = base_path.lower()
        if normalized_path.startswith(f"{normalized_base}/"):
            candidates.append(path[len(base_path) :])
    return any(_first_segment(candidate) in _PROXY_SEGMENTS for candidate in candidates)


def validate_data_url(
    url: str | httpx.URL, *, openlist_base_url: str | httpx.URL
) -> httpx.URL:
    """Validate that a result URL is direct HTTP data rather than an OpenList proxy.

    Args:
        url: The result URL returned by OpenList.
        openlist_base_url: The configured OpenList endpoint URL.

    Raises:
        PullError: If the URL is invalid, unsupported, or an OpenList proxy route.

    Returns:
        The validated HTTPX URL.
    """
    try:
        target = httpx.URL(url)
        base = httpx.URL(openlist_base_url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise DirectLinkUnavailableError from exc
    if target.scheme not in _DEFAULT_PORTS or not target.host:
        raise DirectLinkUnavailableError
    # proxy routes are rooted beside `/api`, including reverse-proxy prefixes
    base_path = base.path.removesuffix("/api")
    if _origin(target) == _origin(base) and _is_proxy_path(target.path, base_path):
        raise DirectLinkUnavailableError
    return target


def _header_items(headers: DataHeaders | None) -> list[tuple[str, str]]:
    if headers is None:
        return []
    result: list[tuple[str, str]] = []
    for name, values in headers.items():
        if isinstance(values, str):
            result.append((name, values))
        else:
            result.extend((name, value) for value in values)
    return result


def _pull_headers(
    headers: DataHeaders | None, offset: int
) -> dict[str, str | Sequence[str]]:
    result = {
        name: values
        for name, values in (headers or {}).items()
        if name.lower() not in {"range", "accept-encoding"}
    }
    result["Accept-Encoding"] = "identity"
    if offset:
        result["Range"] = f"bytes={offset}-"
    return result


def _content_range_start(response: httpx.Response) -> int | None:
    unit, separator, value = response.headers.get("Content-Range", "").partition(" ")
    start, dash, _ = value.partition("-")
    if (
        separator
        and dash
        and unit.lower() == "bytes"
        and start.isascii()
        and start.isdecimal()
    ):
        return int(start)
    return None


def _write_mode(response: httpx.Response, offset: int) -> Literal["wb", "ab"]:
    if response.status_code == 200:
        return "wb"
    if response.status_code == 206 and _content_range_start(response) == offset:
        return "ab" if offset else "wb"
    raise PullFailedError


def _safe_local_path(task_dir: str | Path, relative_path: str) -> Path:
    try:
        relative_path = normalize_relative_path(relative_path)
        root = Path(task_dir).absolute()
        resolved_root = root.resolve()
        target = root.joinpath(*relative_path.split("/"))
        # resolve existing symlinks before enforcing task-directory containment
        target.resolve().relative_to(resolved_root)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise InvalidLocalPathError from exc
    return target


def _job_path(final_path: Path, job_id: str, suffix: str) -> Path:
    """Build a validated job-scoped sidecar path.

    Args:
        final_path: The final local file path.
        job_id: The offline job UUID owning the sidecar.
        suffix: The sidecar type appended to the generated name.

    Raises:
        PullError: If `job_id` cannot be embedded safely.

    Returns:
        The hidden sidecar path adjacent to `final_path`.
    """
    if len(job_id) != 32 or any(character not in _HEX_DIGITS for character in job_id):
        raise InvalidLocalPathError
    legacy_name = f".{final_path.name}.{job_id}.{suffix}"
    pathconf = getattr(os, "pathconf", None)
    try:
        name_max = pathconf(final_path.parent, "PC_NAME_MAX") if pathconf else 255
    except (OSError, ValueError):
        name_max = 255
    if name_max <= 0 or len(os.fsencode(legacy_name)) <= name_max:
        return final_path.with_name(legacy_name)

    # keep sidecars bounded while preserving the legacy path whenever it fits
    digest = hashlib.sha256(
        job_id.encode() + b"\0" + os.fsencode(final_path.name)
    ).hexdigest()[:32]
    return final_path.with_name(f".{digest}.{suffix}")


def _ownership_value(path: Path) -> bytes:
    """Build the filesystem identity stored in an ownership marker.

    Args:
        path: The regular file whose identity is being recorded.

    Returns:
        The encoded device, inode, size, and modification-time identity.
    """
    stat = path.lstat()
    return f"{stat.st_dev}:{stat.st_ino}:{stat.st_size}:{stat.st_mtime_ns}".encode()


def _owns_local_file(final_path: Path, marker_path: Path) -> bool:
    """Check whether a marker still identifies the installed file.

    Args:
        final_path: The installed file being checked.
        marker_path: The job-scoped ownership marker.

    Returns:
        `True` when both paths are regular and their identities match.
    """
    try:
        final_stat = final_path.lstat()
        marker_stat = marker_path.lstat()
        return (
            S_ISREG(final_stat.st_mode)
            and S_ISREG(marker_stat.st_mode)
            and 0 < marker_stat.st_size <= 128
            and marker_path.read_bytes() == _ownership_value(final_path)
        )
    except OSError:
        return False


def _remove_installed_part(final_path: Path, job_id: str):
    """Remove a partial path left linked to the installed file.

    Args:
        final_path: The verified file owned by the job.
        job_id: The offline job UUID owning the partial path.
    """
    part_path = _job_path(final_path, job_id, "part")
    with suppress(FileNotFoundError):
        if S_ISREG(part_path.lstat().st_mode) and part_path.samefile(final_path):
            part_path.unlink()


def delete_local_file(
    task_dir: str | Path,
    relative_path: str,
    job_id: str,
    *,
    keep_final: bool = False,
):
    """Delete one manifest file's job-scoped local paths.

    Args:
        task_dir: The local task root directory.
        relative_path: The canonical path relative to `task_dir`.
        job_id: The offline job UUID owning the local paths.
        keep_final: Whether to preserve the installed final file.

    Raises:
        PullError: If the path is unsafe or a local file cannot be deleted.
    """
    final_path = _safe_local_path(task_dir, relative_path)
    part_path = _job_path(final_path, job_id, "part")
    marker_path = _job_path(final_path, job_id, "done")
    owned = not keep_final and _owns_local_file(final_path, marker_path)
    paths = (final_path, part_path, marker_path) if owned else (part_path, marker_path)
    for path in paths:
        try:
            mode = path.lstat().st_mode
            if S_ISREG(mode) or S_ISLNK(mode):
                path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PullError(
                OfflineDownloadErrorKind.PULL_FAILED,
                "Local file could not be deleted",
            ) from exc


def delete_local_directory(task_dir: str | Path, relative_path: str):
    """Delete one empty manifest directory.

    Args:
        task_dir: The local task root directory.
        relative_path: The canonical path relative to `task_dir`.

    Raises:
        PullError: If the path is unsafe or an empty directory cannot be removed.
    """
    path = _safe_local_path(task_dir, relative_path)
    try:
        if not S_ISDIR(path.lstat().st_mode):
            return
        path.rmdir()
    except FileNotFoundError:
        return
    except OSError as exc:
        try:
            if any(path.iterdir()):
                return
        except OSError:
            pass
        raise PullError(
            OfflineDownloadErrorKind.PULL_FAILED,
            "Local directory could not be deleted",
        ) from exc


async def verify_local_file(path: Path, entry: RemoteManifestEntry) -> bool:
    """Verify one local regular file against its manifest entry.

    Size is always checked. Supported remote hashes are checked when available.

    Args:
        path: The local file path.
        entry: The expected remote manifest entry.

    Returns:
        `True` when the local file matches, otherwise `False`.
    """
    try:
        file_stat = path.lstat()
        if (
            entry.is_dir
            or not S_ISREG(file_stat.st_mode)
            or file_stat.st_size != entry.size
        ):
            return False
        expected = [
            (algorithm.lower().replace("-", ""), value.lower())
            for algorithm, value in entry.hashes
            if algorithm.lower().replace("-", "") in _HASH_ALGORITHMS
        ]
        if not expected:
            return True
        hashers = {
            algorithm: hashlib.new(algorithm, usedforsecurity=False)
            for algorithm, _ in expected
        }
        async with aiofiles.open(path, "rb") as file:
            while chunk := await file.read(_HASH_CHUNK_SIZE):
                for hasher in hashers.values():
                    hasher.update(chunk)
    except OSError:
        return False
    return all(hashers[algorithm].hexdigest() == value for algorithm, value in expected)


async def verify_local_manifest(
    task_dir: str | Path, entries: Sequence[RemoteManifestEntry]
) -> tuple[str, ...] | None:
    """Verify every local path represented by a remote manifest.

    Args:
        task_dir: The local task root directory.
        entries: The canonical remote manifest entries.

    Returns:
        The verified relative file paths, or `None` when any entry fails.
    """
    files = []
    for entry in entries:
        try:
            path = _safe_local_path(task_dir, entry.path)
        except PullError:
            return None
        if entry.is_dir:
            try:
                if not S_ISDIR(path.lstat().st_mode):
                    return None
            except OSError:
                return None
        elif await verify_local_file(path, entry):
            files.append(entry.path)
        else:
            return None
    return tuple(files)


async def find_completed_file(
    task_dir: str | Path, entry: RemoteManifestEntry, job_id: str
) -> Path | None:
    """Find and verify an existing completed local file.

    Args:
        task_dir: The local task root directory.
        entry: The expected remote manifest entry.
        job_id: The offline job UUID owning the completed file.

    Raises:
        PullError: If an existing path conflicts with the manifest.

    Returns:
        The verified completed path, or `None` when it does not exist.
    """
    final_path = _safe_local_path(task_dir, entry.path)
    marker_path = _job_path(final_path, job_id, "done")
    try:
        final_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LocalFileConflictError from exc
    if not _owns_local_file(final_path, marker_path):
        # recover the old publication order only when the job still owns a hard link
        part_path = _job_path(final_path, job_id, "part")
        try:
            if not S_ISREG(part_path.lstat().st_mode) or not part_path.samefile(
                final_path
            ):
                raise LocalFileConflictError
            ownership = _ownership_value(final_path)
        except OSError as exc:
            raise LocalFileConflictError from exc
        try:
            with marker_path.open("xb") as marker:
                marker.write(ownership)
                marker.flush()
                os.fsync(marker.fileno())
        except FileExistsError as exc:
            raise LocalFileConflictError from exc
        except OSError as exc:
            raise PullError(
                OfflineDownloadErrorKind.PULL_FAILED,
                "Local file ownership could not be recovered",
            ) from exc
    if await verify_local_file(final_path, entry):
        # remove a hard link left by an exit immediately after publication
        _remove_installed_part(final_path, job_id)
        return final_path
    delete_local_file(task_dir, entry.path, job_id)
    return None


def prepare_local_file(
    task_dir: str | Path, relative_path: str, job_id: str
) -> LocalFileTarget:
    """Prepare a safe final path and resumable partial file.

    Args:
        task_dir: The local task root directory.
        relative_path: The canonical path relative to `task_dir`.
        job_id: The offline job UUID owning the partial file.

    Raises:
        PullError: If the path is unsafe, conflicting, or cannot be prepared.

    Returns:
        The prepared local target and current resume offset.
    """
    final_path = _safe_local_path(task_dir, relative_path)
    part_path = _job_path(final_path, job_id, "part")
    marker_path = _job_path(final_path, job_id, "done")
    try:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            final_path.lstat()
        except FileNotFoundError:
            pass
        else:
            raise LocalFileConflictError

        try:
            marker_stat = marker_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not S_ISREG(marker_stat.st_mode):
                raise LocalFileConflictError
            marker_path.unlink()

        try:
            part_stat = part_path.lstat()
        except FileNotFoundError:
            # create `.part` exclusively so concurrent writers cannot replace it
            part_path.touch(exist_ok=False)
            part_stat = part_path.lstat()
        if not S_ISREG(part_stat.st_mode):
            raise LocalFileConflictError
    except PullError:
        raise
    except (FileExistsError, NotADirectoryError) as exc:
        raise LocalFileConflictError from exc
    except OSError as exc:
        raise PullError(
            OfflineDownloadErrorKind.PULL_FAILED,
            "Local file could not be prepared",
        ) from exc
    return LocalFileTarget(
        final_path=final_path,
        part_path=part_path,
        marker_path=marker_path,
        offset=part_stat.st_size,
    )


def prepare_local_directory(task_dir: str | Path, relative_path: str) -> Path:
    """Create and validate one local manifest directory.

    Args:
        task_dir: The local task root directory.
        relative_path: The canonical path relative to `task_dir`.

    Raises:
        PullError: If the path is unsafe, conflicting, or cannot be created.

    Returns:
        The validated local directory path.
    """
    path = _safe_local_path(task_dir, relative_path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:
        raise LocalFileConflictError from exc
    except OSError as exc:
        raise PullError(
            OfflineDownloadErrorKind.PULL_FAILED,
            "Local directory could not be prepared",
        ) from exc
    if path.is_symlink() or not path.is_dir():
        raise LocalFileConflictError
    return path


def _rename_exclusive(source: Path, destination: Path):
    """Publish a file atomically on filesystems without hard links.

    Args:
        source: The file to rename.
        destination: The final path, which must not already exist.

    Raises:
        OSError: If exclusive renaming is unavailable or fails.
    """
    if sys.platform == "win32":
        os.rename(source, destination)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        # request `RENAME_EXCL`
        arguments = (os.fsencode(source), os.fsencode(destination), 0x4)
    elif sys.platform == "linux" and hasattr(libc, "renameat2"):
        rename = libc.renameat2
        rename.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        # use `AT_FDCWD` and `RENAME_NOREPLACE`
        arguments = (-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        raise OSError(errno.ENOSYS, "Exclusive renaming is unavailable")
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _install_local_file_sync(target: LocalFileTarget):
    """Install one completed file without overwriting another job.

    Args:
        target: The verified partial file and its job-scoped paths.

    Raises:
        PullError: If the final path conflicts or installation fails.
    """
    marked = False
    installed = False
    try:
        # persist ownership before the final name becomes visible
        ownership = _ownership_value(target.part_path)
        with target.marker_path.open("xb") as marker:
            marked = True
            marker.write(ownership)
            marker.flush()
            os.fsync(marker.fileno())
        # create the final name atomically without replacing another job's file
        try:
            if sys.platform == "win32":
                os.rename(target.part_path, target.final_path)
            else:
                os.link(
                    target.part_path,
                    target.final_path,
                    follow_symlinks=False,
                )
        except OSError as exc:
            if exc.errno not in _HARDLINK_UNAVAILABLE:
                raise
            _rename_exclusive(target.part_path, target.final_path)
        installed = True
        target.part_path.unlink(missing_ok=True)
    except FileExistsError as exc:
        if marked and not installed:
            with suppress(OSError):
                target.marker_path.unlink()
        raise LocalFileConflictError from exc
    except OSError as exc:
        if marked and not installed:
            with suppress(OSError):
                target.marker_path.unlink()
        raise PullError(
            OfflineDownloadErrorKind.PULL_FAILED,
            "Local file could not be completed",
        ) from exc


async def _install_local_file(target: LocalFileTarget):
    await asyncio.to_thread(_install_local_file_sync, target)


def transfer_local_file(
    source: Path, destination: Path, job_id: str, *, move: bool = False
):
    """Transfer a completed file without exposing an unfinished library copy.

    Args:
        source: The verified local download file.
        destination: The final media-library file path.
        job_id: The offline job UUID owning any staged copy.
        move: Whether to remove the source after publishing the destination.

    Raises:
        OSError: If copying, moving, or removing a local file fails.
        PullError: If staging or publishing the copy fails.
    """
    if source.resolve() == destination.resolve():
        return
    # distinguish library staging from this job's original download sidecars
    copy_id = hashlib.sha256(f"library:{job_id}".encode()).hexdigest()[:32]
    marker_path = _job_path(destination, copy_id, "done")
    if destination.exists():
        if _owns_local_file(destination, marker_path):
            _remove_installed_part(destination, copy_id)
            # finish a move interrupted between publication and source removal
            if move:
                source.unlink(missing_ok=True)
            marker_path.unlink()
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    if move:
        try:
            _rename_exclusive(source, destination)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
    target = prepare_local_file(destination.parent, destination.name, copy_id)
    shutil.copy2(source, target.part_path)
    _install_local_file_sync(target)
    if move:
        source.unlink()
    target.marker_path.unlink()


async def _complete_local_file(target: LocalFileTarget, entry: RemoteManifestEntry):
    if not await verify_local_file(target.part_path, entry):
        raise PullError(
            OfflineDownloadErrorKind.VERIFY_FAILED,
            "Local file verification failed",
        )
    await _install_local_file(target)


async def _handle_416(target: LocalFileTarget, entry: RemoteManifestEntry) -> int:
    if target.offset and await verify_local_file(target.part_path, entry):
        return entry.size
    try:
        async with aiofiles.open(target.part_path, "wb"):
            pass
    except OSError as exc:
        raise PullError(
            OfflineDownloadErrorKind.PULL_FAILED,
            "Local file could not be reset",
        ) from exc
    raise _RetryPull


def _current_target(target: LocalFileTarget) -> LocalFileTarget:
    try:
        part_stat = target.part_path.lstat()
    except OSError as exc:
        raise PullError(
            OfflineDownloadErrorKind.PULL_FAILED,
            "Local partial file could not be inspected",
        ) from exc
    if not S_ISREG(part_stat.st_mode):
        raise LocalFileConflictError
    return LocalFileTarget(
        target.final_path,
        target.part_path,
        target.marker_path,
        part_stat.st_size,
    )


def _redirect_url(response: httpx.Response) -> httpx.URL:
    location = response.headers.get("Location")
    if not location:
        raise PullFailedError
    try:
        return response.request.url.join(location)
    except (httpx.InvalidURL, TypeError) as exc:
        raise DirectLinkUnavailableError from exc


def _redirect_headers(
    headers: DataHeaders, source: httpx.URL, target: httpx.URL
) -> DataHeaders:
    if _origin(source) == _origin(target):
        return headers
    # never forward provider credentials across origins
    return {
        name: values
        for name, values in headers.items()
        if name.lower() not in _SENSITIVE_HEADERS
    }


async def _download_response(
    response: httpx.Response,
    target: LocalFileTarget,
    entry: RemoteManifestEntry,
    reporter: _ProgressReporter,
) -> int:
    if response.status_code == 416:
        return await _handle_416(target, entry)
    mode = _write_mode(response, target.offset)
    size = target.offset if mode == "ab" else 0
    try:
        async with aiofiles.open(target.part_path, mode) as file:
            async for chunk in response.aiter_raw():
                if size + len(chunk) > entry.size:
                    raise PullError(
                        OfflineDownloadErrorKind.VERIFY_FAILED,
                        "Downloaded file exceeds expected size",
                    )
                await file.write(chunk)
                size += len(chunk)
                await reporter.report(size)
    except OSError as exc:
        raise PullError(
            OfflineDownloadErrorKind.PULL_FAILED,
            "Local file could not be written",
        ) from exc
    return size


async def _pull_once(
    client: "DataClient",
    target: LocalFileTarget,
    link: RemoteLink,
    entry: RemoteManifestEntry,
    reporter: _ProgressReporter,
) -> int:
    url: str | httpx.URL = link.url
    headers: DataHeaders = _pull_headers(link.headers, target.offset)
    seen: set[str] = set()
    for redirect_count in range(_MAX_REDIRECTS + 1):
        async with client.stream(url, headers=headers) as response:
            request_url = response.request.url
            seen.add(str(request_url))
            if response.status_code in _EXPIRED_LINK_STATUSES:
                raise _RefreshLink
            if response.status_code == 429:
                # let the runtime persist the deadline before releasing this pull
                raise OpenListClientError(
                    OpenListErrorKind.RATE_LIMIT,
                    "Direct file request was rate limited",
                    retry_after=response.headers.get("Retry-After"),
                )
            if response.status_code >= 500:
                raise _RetryPull
            if response.status_code not in _REDIRECT_STATUSES:
                size = await _download_response(response, target, entry, reporter)
            else:
                if redirect_count == _MAX_REDIRECTS:
                    raise PullFailedError
                next_url = _redirect_url(response)
                if str(next_url) in seen:
                    raise PullFailedError
                headers = _redirect_headers(headers, request_url, next_url)
                url = next_url
                continue
        await reporter.report(size, final=True)
        if response.status_code == 416:
            await _install_local_file(target)
        else:
            await _complete_local_file(target, entry)
        return size
    raise PullFailedError


async def pull_file(
    data_client: "DataClient",
    link_client: OpenListClient,
    target: LocalFileTarget,
    remote_path: str,
    *,
    entry: RemoteManifestEntry,
    progress: ProgressCallback | None = None,
) -> int:
    """Pull one remote file with resume, link refresh, retry, and verification.

    Args:
        data_client: The isolated client used for file content requests.
        link_client: The OpenList client used to obtain direct links.
        target: The prepared local destination and resume offset.
        remote_path: The absolute OpenList file path.
        entry: The expected remote manifest entry.
        progress: The optional asynchronous progress callback.

    Raises:
        PullError: If the link, response, local path, transfer, or verification fails.
        OpenListClientError: If link acquisition fails or file data is rate limited.

    Returns:
        The verified transferred file size in bytes.
    """
    link = await link_client.link(remote_path)
    current = _current_target(target)
    reporter = _ProgressReporter(progress, entry.size, current.offset)
    attempts = 0
    refreshes = 0
    while True:
        target = _current_target(target)
        try:
            return await _pull_once(data_client, target, link, entry, reporter)
        except _RefreshLink as exc:
            if refreshes >= _MAX_LINK_REFRESHES:
                raise PullError(
                    OfflineDownloadErrorKind.PULL_FAILED,
                    "Direct link refresh limit reached",
                ) from exc
            refreshes += 1
            link = await link_client.link(remote_path)
        except (httpx.RequestError, _RetryPull) as exc:
            attempts += 1
            if attempts >= _MAX_ATTEMPTS:
                raise PullError(
                    OfflineDownloadErrorKind.PULL_FAILED,
                    "File download retry limit reached",
                ) from exc
            await asyncio.sleep(2 ** (attempts - 1))


class DataClient:
    """Fetch direct file data without application proxy or environment settings."""

    def __init__(self, openlist_base_url: str):
        logging.getLogger("httpx").addFilter(_HTTPX_URL_FILTER)
        self._openlist_base_url = httpx.URL(openlist_base_url)
        self._client = httpx.AsyncClient(
            http2=True,
            follow_redirects=False,
            timeout=60,
            trust_env=False,
        )

    async def aclose(self):
        """Close the owned asynchronous HTTP client."""
        await self._client.aclose()

    @asynccontextmanager
    async def stream(
        self,
        url: str | httpx.URL,
        *,
        headers: DataHeaders | None = None,
    ) -> AsyncGenerator[httpx.Response]:
        """Open one validated direct response stream.

        Args:
            url: The direct result URL.
            headers: The provider headers required by the direct link.

        Yields:
            The open streaming HTTP response.
        """
        target = validate_data_url(url, openlist_base_url=self._openlist_base_url)
        request = httpx.Request("GET", target, headers=_header_items(headers))
        response = await self._client.send(request, stream=True, follow_redirects=False)
        try:
            yield response
        finally:
            await response.aclose()
