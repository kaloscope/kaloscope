"""Media probing and HLS transcoding utilities."""

import asyncio
import contextlib
import math
import os
import re
import shutil
import signal
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiofiles
from filelock import FileLock, Timeout
from sanic import Sanic
from sanic.exceptions import SanicException
from sanic.log import logger

from app.core.config import KaloscopeConfig
from app.core.constants import ENCODING
from app.utils.disk import format_bytes

_SEGMENT_WAIT_TIMEOUT = 30.0
_SEGMENT_WAIT_INTERVAL = 0.25

_TASKS: dict[str, dict[str, Any]] = {}
_TASKS_LOCK = threading.RLock()


@dataclass
class TranscodeStats:
    """Derived statistics for a transcoded HLS output directory."""

    finished: bool = False
    duration: float = 0.0
    segments: int = 0
    size: int = 0
    progress: int | None = None
    updated_at: str | None = None


@dataclass
class EncoderConfig:
    """Configuration for a specific hardware encoder."""

    encoder: str = "libx264"
    hwaccel: str | None = None
    hwaccel_output_format: str | None = None


# hardware acceleration types (mapped to encoder name and ffmpeg flags)
HWAccelType = Literal["qsv", "vaapi", "nvenc", "videotoolbox"]
_ENCODER_CONFIG: dict[str | None, EncoderConfig] = {
    None: EncoderConfig(
        encoder="libx264",
        hwaccel=None,
        hwaccel_output_format=None,
    ),
    "qsv": EncoderConfig(
        encoder="h264_qsv",
        hwaccel="qsv",
        hwaccel_output_format="qsv",
    ),
    "vaapi": EncoderConfig(
        encoder="h264_vaapi",
        hwaccel="vaapi",
        hwaccel_output_format=None,
    ),
    "nvenc": EncoderConfig(
        encoder="h264_nvenc",
        hwaccel="cuda",
        hwaccel_output_format="cuda",
    ),
    "videotoolbox": EncoderConfig(
        encoder="h264_videotoolbox",
        hwaccel="videotoolbox",
        hwaccel_output_format="videotoolbox",
    ),
}

# transcode quality levels (mapped to CRF values and bitrate targets)
QualityLevel = Literal["low", "medium", "high"]
_QUALITY_CRF: dict[QualityLevel, int] = {
    "low": 28,
    "medium": 23,
    "high": 18,
}
_HW_BITRATE: dict[QualityLevel, str] = {
    "low": "1500k",
    "medium": "3000k",
    "high": "6000k",
}

# output resolution limits (mapped to max height in pixels)
ResolutionLimit = Literal["original", "1080p", "720p", "480p"]
_RESOLUTION_MAX_HEIGHT: dict[ResolutionLimit, int | None] = {
    "original": None,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
}


@dataclass
class TranscodeOptions:
    """Transcoding parameters for web playback."""

    hwaccel: HWAccelType | None = None
    quality: QualityLevel = "medium"
    resolution: ResolutionLimit = "original"
    framerate: float = 30.0

    def __post_init__(self):
        if self.hwaccel not in _ENCODER_CONFIG:
            self.hwaccel = None

    @property
    def encoder_config(self) -> EncoderConfig:
        return _ENCODER_CONFIG[self.hwaccel]

    @property
    def encoder(self) -> str:
        return self.encoder_config.encoder

    @property
    def crf(self) -> int:
        return _QUALITY_CRF[self.quality]

    @property
    def max_height(self) -> int | None:
        return _RESOLUTION_MAX_HEIGHT[self.resolution]

    @property
    def profile(self) -> str:
        """Transcode profile identifier (filesystem-safe directory name)."""
        return f"{self.quality}_{self.resolution}_{str(self.hwaccel).lower()}"


def _task_store():
    """Return the cross-worker task store and lock, with a test fallback.

    Returns:
        A tuple containing the task mapping and its synchronization lock.
    """
    try:
        shared = Sanic.get_app().shared_ctx
        return shared.transcode_tasks, shared.transcode_tasks_lock
    except (AttributeError, SanicException):
        return _TASKS, _TASKS_LOCK


def estimate_progress(encoded_duration: float, duration: float | None) -> int | None:
    """Estimate transcode progress from encoded and source durations.

    Args:
        encoded_duration: The duration already encoded, in seconds.
        duration: The total source duration in seconds, if known.

    Returns:
        The estimated percentage capped at 99, or `None` if unavailable.
    """
    if duration and duration > 0 and encoded_duration > 0:
        return min(99, max(0, int(encoded_duration / duration * 100)))
    return None


def parse_profile(profile: str) -> dict[str, str | None]:
    """Parse a transcode profile directory name into UI tags.

    Profiles are generated as `{quality}_{resolution}_{hwaccel}` by
    `TranscodeOptions.profile`. Unknown profile shapes return empty fields so
    callers can still display the raw profile as a fallback tag.

    Args:
        profile: The transcode profile directory name.

    Returns:
        The parsed quality, resolution, and hardware acceleration tags.
    """
    quality, sep1, rest = profile.partition("_")
    resolution, sep2, hwaccel = rest.rpartition("_")
    if not sep1 or not sep2:
        return {"quality": None, "resolution": None, "hwaccel": None}
    if quality not in _QUALITY_CRF or resolution not in _RESOLUTION_MAX_HEIGHT:
        return {"quality": None, "resolution": None, "hwaccel": None}

    hwaccel = hwaccel.lower()
    valid_hwaccels = {*_ENCODER_CONFIG.keys(), "software", "none", "null"}
    if hwaccel not in valid_hwaccels:
        return {"quality": None, "resolution": None, "hwaccel": None}

    return {
        "quality": quality,
        "resolution": resolution,
        "hwaccel": None if hwaccel in ("none", "null", "software") else hwaccel,
    }


_EXTINF_RE = re.compile(r"^#EXTINF:([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
"""Regular expression to extract segment and duration from HLS playlists."""


def output_stats(out_dir: Path | str, duration: float | None = None) -> TranscodeStats:
    """Read HLS output files and derive progress information.

    Args:
        out_dir: The HLS output directory.
        duration: The total source duration in seconds, if known.

    Returns:
        Statistics derived from the output files and playlist.
    """
    out_dir = Path(out_dir)
    stats = TranscodeStats()
    if not out_dir.is_dir():
        return stats

    for path in out_dir.rglob("*"):
        if path.is_file():
            with contextlib.suppress(OSError):
                stats.size += path.stat().st_size

    m3u8_path = out_dir / "index.m3u8"
    if not m3u8_path.is_file():
        return stats

    try:
        content = m3u8_path.read_text(encoding=ENCODING)
        stat = m3u8_path.stat()
    except OSError:
        return stats

    segments = [float(match.group(1)) for match in _EXTINF_RE.finditer(content)]
    stats.finished = "#EXT-X-ENDLIST" in content
    stats.duration = round(sum(segments), 3)
    stats.segments = len(segments)
    stats.updated_at = datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()

    if stats.finished:
        stats.progress = 100
    else:
        stats.progress = estimate_progress(stats.duration, duration)

    return stats


def _remove_endlist(out_dir: Path | str | None) -> None:
    """Remove a completion marker from an interrupted HLS playlist.

    Args:
        out_dir: The HLS output directory, or `None` if unavailable.
    """
    if out_dir is None:
        return
    m3u8_path = Path(out_dir) / "index.m3u8"
    if not m3u8_path.is_file():
        return
    try:
        content = m3u8_path.read_text(encoding=ENCODING)
        original_lines = content.splitlines()
        lines = [line for line in original_lines if line.strip() != "#EXT-X-ENDLIST"]
        if len(lines) == len(original_lines):
            return
        content = "\n".join(lines)
        if content:
            content += "\n"
        m3u8_path.write_text(content, encoding=ENCODING)
    except OSError:
        logger.warning("Failed to remove HLS ENDLIST marker: %s", m3u8_path)


def scan_outputs(root: Path | str | None = None) -> list[dict[str, Any]]:
    """Scan the transcoded workspace for finished and interrupted HLS outputs.

    Args:
        root: The transcode root directory, or `None` to use the workspace.

    Returns:
        Task snapshots derived from HLS output directories.
    """
    root = (
        Path(root)
        if root is not None
        else Path(KaloscopeConfig.get_workspace("transcoded"))
    )
    if not root.is_dir():
        return []

    tasks: list[dict[str, Any]] = []
    for hash_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        media_hash = hash_dir.name
        for profile_dir in sorted(path for path in hash_dir.iterdir() if path.is_dir()):
            profile = profile_dir.name
            stats = output_stats(profile_dir)
            if not stats.finished and stats.segments == 0:
                continue
            profile_tags = parse_profile(profile)
            state = "finished" if stats.finished else "stopped"
            tasks.append(
                {
                    "id": f"{media_hash}:{profile}",
                    "name": f"{media_hash}/{profile}",
                    "path": None,
                    "hash": media_hash,
                    "state": state,
                    "progress": 100 if stats.finished else stats.progress,
                    "duration": stats.duration if stats.finished else None,
                    "encoded_duration": stats.duration,
                    "encoded_segments": stats.segments,
                    "encoded_size": stats.size,
                    "pid": None,
                    "profile": profile,
                    "quality": profile_tags["quality"],
                    "resolution": profile_tags["resolution"],
                    "hwaccel": profile_tags["hwaccel"],
                    "started_at": None,
                    "finished_at": stats.updated_at,
                    "error_msg": None,
                }
            )
    return tasks


def delete_output(
    media_hash: str, profile: str, root: Path | str | None = None
) -> bool:
    """Delete a deterministic transcode output directory.

    Args:
        media_hash: The source media hash directory name.
        profile: The transcode profile directory name.
        root: The transcode root directory, or `None` to use the workspace.

    Returns:
        `True` if the output directory existed and was deleted.
    """
    root = (
        Path(root)
        if root is not None
        else Path(KaloscopeConfig.get_workspace("transcoded"))
    )
    out_dir = root / media_hash / profile
    try:
        root_resolved = root.resolve()
        out_resolved = out_dir.resolve()
    except OSError:
        return False
    if not out_resolved.is_relative_to(root_resolved) or not out_dir.is_dir():
        return False

    shutil.rmtree(out_dir)
    with contextlib.suppress(OSError):
        out_dir.parent.rmdir()
    return True


async def register_task(
    media_path: str,
    media_hash: str,
    options: TranscodeOptions,
    out_dir: Path,
    proc: asyncio.subprocess.Process,
    duration: float | None,
) -> str:
    """Register a newly started ffmpeg process in the shared task store.

    Args:
        media_path: The source media file path.
        media_hash: The source media hash.
        options: The transcode parameters used by the process.
        out_dir: The HLS output directory.
        proc: The running ffmpeg subprocess.
        duration: The total source duration in seconds, if known.

    Returns:
        The registered task ID.
    """
    task_id = f"{media_hash}:{options.profile}"
    tasks, lock = _task_store()
    lock.acquire()
    try:
        tasks[task_id] = {
            "id": task_id,
            "name": Path(media_path).name,
            "path": media_path,
            "hash": media_hash,
            "state": "running",
            "duration": duration,
            "pid": proc.pid,
            "profile": options.profile,
            "quality": options.quality,
            "resolution": options.resolution,
            "hwaccel": options.hwaccel,
            "out_dir": str(out_dir),
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None,
            "error_msg": None,
        }
    finally:
        lock.release()
    return task_id


async def finish_task(
    task_id: str, returncode: int | None, error_msg: str | None = None
) -> None:
    """Mark a registered ffmpeg task as finished.

    Args:
        task_id: The registered task ID.
        returncode: The ffmpeg process exit code.
        error_msg: The captured ffmpeg error message, if available.
    """
    tasks, lock = _task_store()
    lock.acquire()
    try:
        task = tasks.get(task_id)
        if task is None:
            return
        task = dict(task)
        task["finished_at"] = datetime.now(UTC).isoformat()
        if returncode == 0:
            task["state"] = "finished"
        elif task["state"] == "stopping" or returncode == 255:
            task["state"] = "stopped"
            _remove_endlist(task.get("out_dir"))
        else:
            task["state"] = "error"
            task["error_msg"] = error_msg
            _remove_endlist(task.get("out_dir"))
        tasks[task_id] = task
    finally:
        lock.release()


async def list_tasks() -> list[dict[str, Any]]:
    """List runtime and finished filesystem transcode tasks.

    Returns:
        Runtime task snapshots followed by unregistered filesystem outputs.
    """
    store, lock = _task_store()
    lock.acquire()
    try:
        tasks = [_task_snapshot(task) for task in store.values()]
    finally:
        lock.release()

    task_ids = {task["id"] for task in tasks}
    for task in scan_outputs():
        if task["id"] not in task_ids:
            tasks.append(task)
    for task in tasks:
        task["encoded_size_text"] = format_bytes(task["encoded_size"])
    return tasks


def _task_snapshot(task: dict[str, Any]) -> dict[str, Any]:
    """Convert shared task metadata into an API-friendly dictionary.

    Args:
        task: The stored runtime task metadata.

    Returns:
        The task metadata enriched with output statistics.
    """
    snapshot = dict(task)
    stats = output_stats(snapshot.pop("out_dir"), snapshot.get("duration"))
    state = snapshot["state"]
    progress = (
        100
        if state == "finished"
        else estimate_progress(stats.duration, snapshot.get("duration"))
    )
    if state in ("error", "stopped") and progress is None:
        progress = 0

    snapshot.update(
        progress=progress,
        duration=snapshot.get("duration") or stats.duration or None,
        encoded_duration=stats.duration,
        encoded_segments=stats.segments,
        encoded_size=stats.size,
        finished_at=snapshot.get("finished_at") or stats.updated_at,
    )
    return snapshot


async def stop_tasks(ids: list[str]) -> list[str]:
    """Request termination for running shared transcode tasks.

    Args:
        ids: The task IDs to stop.

    Returns:
        The IDs of running tasks handled by the request.
    """
    stopped: list[str] = []
    tasks, lock = _task_store()
    lock.acquire()
    try:
        for task_id in ids:
            task = tasks.get(task_id)
            if task is None:
                continue
            task = dict(task)
            if task["state"] != "running":
                continue
            task["state"] = "stopping"
            try:
                if task.get("pid") is not None:
                    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
                    os.kill(task["pid"], sigkill)
            except ProcessLookupError:
                task["finished_at"] = datetime.now(UTC).isoformat()
                out_dir = task.get("out_dir")
                if out_dir and _is_complete(Path(out_dir) / "index.m3u8"):
                    task["state"] = "finished"
                else:
                    task["state"] = "stopped"
                    _remove_endlist(out_dir)
            tasks[task_id] = task
            stopped.append(task_id)
    finally:
        lock.release()
    return stopped


async def delete_tasks(ids: list[str]) -> list[str]:
    """Delete non-running transcode outputs and remove runtime records.

    Args:
        ids: The task IDs to delete.

    Returns:
        The task IDs removed from the task store or output workspace.
    """
    deleted: list[str] = []
    tasks, lock = _task_store()
    lock.acquire()
    try:
        for task_id in ids:
            task = tasks.get(task_id)
            if task is not None:
                task = dict(task)
            if task is not None and task["state"] in ("running", "stopping"):
                continue
            media_hash, sep, profile = task_id.partition(":")
            if not media_hash or not sep or not profile:
                continue
            root = Path(task["out_dir"]).parents[1] if task is not None else None
            deleted_output = delete_output(media_hash, profile, root=root)
            if deleted_output or task is not None:
                tasks.pop(task_id, None)
                deleted.append(task_id)
    finally:
        lock.release()
    return deleted


async def _ffmpeg() -> str:
    """Get the ffmpeg executable path from global config or default to "ffmpeg".

    Returns:
        The ffmpeg executable name or path.
    """
    from app.models.general import GlobalConfig

    path = await GlobalConfig.get_or_none(key="ffmpeg.path")
    if path and isinstance(path.value, str) and Path(path.value).is_file():
        return path.value
    return "ffmpeg"


async def _ffprobe() -> str:
    """Get the ffprobe executable path from global config or default to "ffprobe".

    Returns:
        The ffprobe executable name or path.
    """
    ffmpeg = await _ffmpeg()
    if ffmpeg != "ffmpeg":
        path = Path(ffmpeg).with_name("ffprobe")
        if path.is_file():
            return str(path)
    return "ffprobe"


async def _vaapi_device() -> str | None:
    """Get the VAAPI render device path.

    Checks the `vaapi.device` global config first, falls back to the
    standard render node `/dev/dri/renderD128`.

    Returns:
        The render device path if it exists, or `None` if not.
    """
    from app.models.general import GlobalConfig

    dev = await GlobalConfig.get_or_none(key="vaapi.device")
    path = (
        dev.value
        if dev and dev.value and isinstance(dev.value, str)
        else "/dev/dri/renderD128"
    )
    return path if Path(path).exists() else None


async def probe_duration(media_path: str) -> float | None:
    """Probe the media file duration in seconds via ffprobe.

    Args:
        media_path: The media file path to probe.

    Returns:
        Duration in seconds, or `None` if probing failed.
    """
    proc = await asyncio.create_subprocess_exec(
        await _ffprobe(),
        "-v",
        "quiet",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        media_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    try:
        return float(stdout.decode().strip())
    except (ValueError, TypeError):
        return None


async def probe_framerate(media_path: str) -> float | None:
    """Probe the average framerate of the media's first video stream via ffprobe.

    The framerate is reported by ffprobe as a rational string (e.g. `"30000/1001"`)
    and is parsed into a float here.  This is used to calculate the GOP size for
    hardware encoders so that segment-boundary keyframes are correctly aligned.

    Args:
        media_path: The media file path to probe.

    Returns:
        Frames per second, or `None` if probing failed or returned an invalid value.
    """
    proc = await asyncio.create_subprocess_exec(
        await _ffprobe(),
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        media_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    raw = stdout.decode().strip()
    try:
        num, _, den = raw.partition("/")
        fps = float(num) / float(den) if den else float(num)
    except (ValueError, TypeError, ZeroDivisionError):
        return None
    # guard against bogus values (e.g. "0/0" -> 0.0)
    return fps if fps > 0 else None


async def ensure_transcode(
    media_path: str, media_hash: str, options: TranscodeOptions
) -> tuple[str, str]:
    """Ensure the media file has been transcoded to HLS for the given profile.

    If the M3U8 playlist already exists and is complete, returns immediately.
    If another process is already transcoding, waits for at least one segment.
    Otherwise acquires the lock and starts an ffmpeg subprocess, waiting for
    the first segment before returning so playback can begin immediately.

    Args:
        media_path: The source media file path.
        media_hash: The media file hash used as part of the output path.
        options: The transcode parameters (encoder, quality, resolution).

    Returns:
        A tuple of `(media_hash, profile)`.
    """
    profile = options.profile
    out_dir = output_dir(media_hash, profile)
    m3u8_path = out_dir / "index.m3u8"

    # return immediately if the M3U8 already exists and is complete
    if _is_complete(m3u8_path):
        logger.debug("HLS already complete: %s", out_dir)
        return media_hash, profile

    # if another ffmpeg is running for this directory, just wait
    lock = _acquire_lock(out_dir)
    if lock is None:
        logger.debug("HLS transcode already in progress: %s", out_dir)
        if not await _wait_segment(m3u8_path):
            raise RuntimeError("HLS first segment was not ready in time")
        return media_hash, profile

    # start the ffmpeg process if we acquired the lock
    try:
        _cleanup_stale_hls(out_dir)

        # probe the real source framerate so GOP-based keyframe placement
        # (used by hardware encoders) aligns with the HLS segment boundaries
        fps = await probe_framerate(media_path)
        if fps is not None:
            options.framerate = fps

        cmd = await _build_hls_cmd(media_path, out_dir, options)
        logger.info("Starting ffmpeg HLS: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # register the task in the memory store
        duration = await probe_duration(media_path)
        task_id = await register_task(
            media_path, media_hash, options, out_dir, proc, duration
        )

        # monitor completion in the background
        asyncio.ensure_future(_monitor_ffmpeg(proc, lock, task_id))

        # wait for at least one segment so the player can start immediately
        if not await _wait_segment(m3u8_path, proc=proc):
            if proc.returncode is not None:
                raise RuntimeError(
                    "ffmpeg exited before generating the first HLS segment"
                )
            raise RuntimeError("HLS first segment was not ready in time")

    except Exception:
        _release_lock(lock)
        raise

    return media_hash, profile


def output_dir(media_hash: str, profile: str) -> Path:
    """Get the deterministic output directory for the transcoded HLS files.

    Args:
        media_hash: The media file hash.
        profile: The transcode profile identifier.

    Returns:
        The output directory path.
    """
    return Path(KaloscopeConfig.get_workspace("transcoded")) / media_hash / profile


def _acquire_lock(out_dir: Path) -> FileLock | None:
    """Try to acquire an exclusive transcode lock for the given output directory.

    Uses a non-blocking `FileLock` on a `.lock` file within the output directory.

    Args:
        out_dir: The output directory to lock.

    Returns:
        The acquired `FileLock` instance if successful,
        or `None` if another process holds the lock.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = FileLock(out_dir / ".lock", blocking=False)
    try:
        lock.acquire()
        return lock
    except Timeout:
        return None


def _release_lock(lock: FileLock):
    """Release the transcode lock, suppressing any exceptions.

    Args:
        lock: The `FileLock` instance to release.
    """
    with contextlib.suppress(Exception):
        lock.release()


def _cleanup_stale_hls(out_dir: Path):
    """Remove stale HLS files before rebuilding an incomplete transcode.

    Args:
        out_dir: The output directory to clean.
    """
    targets = [
        out_dir / "index.m3u8",
        out_dir / "index.m3u8.tmp",
        *out_dir.glob("segment_*.ts"),
        *out_dir.glob("segment_*.ts.tmp"),
    ]
    for path in targets:
        if path.is_file():
            path.unlink()


async def _build_hls_cmd(
    input_path: str, out_dir: Path, options: TranscodeOptions
) -> list[str]:
    """Build the ffmpeg command line for HLS transcoding.

    Constructs a complete ffmpeg command that transcodes a source video into
    an HLS playlist with MPEG-TS segments.  The command configures hardware
    acceleration (if requested), video codec parameters (CRF for libx264,
    bitrate-based for hardware encoders), audio encoding (AAC 128k stereo),
    keyframe alignment for clean segment boundaries, and HLS output settings.

    The command structure, argument ordering, and per-encoder parameters are
    referenced from Jellyfin: https://github.com/jellyfin/jellyfin

    Args:
        input_path: The source media file path.
        out_dir: The output directory for M3U8 playlist and TS segments.
        options: The transcode parameters (encoder, quality, resolution).

    Returns:
        A list of command-line arguments ready for `asyncio.create_subprocess_exec`.
    """
    cmd = [await _ffmpeg(), "-hide_banner", "-loglevel", "error"]

    # HLS segment length in seconds
    seg_len = 6

    # when scaling is requested we use a CPU `scale` filter, which cannot
    # operate on GPU-resident frames; in that case skip hwaccel_output_format
    # so decoded frames stay in system memory (the encoder re-uploads them)
    needs_scale = options.max_height is not None

    # hardware acceleration
    hw = options.encoder_config
    if hw.hwaccel == "qsv":
        qsv_dev = await _vaapi_device()
        if not qsv_dev:
            raise RuntimeError(
                "QSV requires a DRM render device, e.g. /dev/dri/renderD128"
            )
        cmd.extend(
            [
                "-init_hw_device",
                f"qsv=qs:hw,child_device={qsv_dev},child_device_type=vaapi",
                "-filter_hw_device",
                "qs",
            ]
        )
        if not needs_scale:
            cmd.extend(
                [
                    "-hwaccel",
                    "qsv",
                    "-hwaccel_device",
                    "qs",
                    "-hwaccel_output_format",
                    "qsv",
                ]
            )
    elif hw.hwaccel:
        vaapi_dev = hw.hwaccel == "vaapi" and await _vaapi_device()
        if vaapi_dev:
            cmd.extend(["-vaapi_device", vaapi_dev])
        else:
            cmd.extend(["-hwaccel", hw.hwaccel])
            if hw.hwaccel_output_format and not needs_scale:
                cmd.extend(["-hwaccel_output_format", hw.hwaccel_output_format])

    cmd.extend(["-i", input_path])

    # strip metadata and chapters from output (not needed for web playback)
    cmd.extend(["-map_metadata", "-1", "-map_chapters", "-1"])

    # stream mapping placed before codec arguments
    cmd.extend(["-map", "0:v:0?", "-map", "0:a:0?"])

    # video filter chain
    vf_parts: list[str] = []
    if needs_scale:
        # scale filter to limit the output height while preserving aspect ratio,
        # and ensure the dimensions are compatible with H.264 encoders
        target_height = f"trunc(min({options.max_height},ih)/2)*2"
        vf_parts.append(
            f"scale='max(trunc(iw*{target_height}/ih/16)*16,16)':'{target_height}'"
        )

    # QSV: when frames stay on the GPU, use QSV VPP to normalize them to NV12.
    # When CPU scaling is requested, keep frames in system memory and let the
    # QSV encoder upload them after the software scale/format conversion.
    if hw.hwaccel == "qsv":
        vf_parts.append("format=nv12" if needs_scale else "vpp_qsv=format=nv12")

    # VAAPI: ensure NV12 8-bit format and re-upload to GPU for the encoder.
    # HEVC 10-bit decode produces P010 surfaces — format=nv12 converts in
    # software, then hwupload uploads to the device created by -vaapi_device
    # (or the implicit device from -hwaccel vaapi as fallback).
    elif hw.hwaccel == "vaapi":
        vf_parts.append("format=nv12")
        vf_parts.append("hwupload")

    # video encoder and parameters
    enc = options.encoder
    cmd.extend(["-c:v", enc])

    if enc == "libx264":
        bitrate = _HW_BITRATE.get(options.quality, "3000k")
        bitrate_num = int(bitrate[:-1])
        bufsize = str(bitrate_num * 2) + "k"
        cmd.extend(
            [
                "-preset",
                "veryfast",
                "-crf",
                str(options.crf),
                "-profile:v",
                "main",
                "-level",
                "4.0",
                "-pix_fmt",
                "yuv420p",
                # VBV constraints cap peak bitrate during CRF encoding,
                # preventing network-unfriendly bitrate spikes
                "-maxrate",
                bitrate,
                "-bufsize",
                bufsize,
            ]
        )

    elif enc == "h264_qsv":
        bitrate = _HW_BITRATE.get(options.quality, "3000k")
        bitrate_num = int(bitrate[:-1])
        # QSV rate control follows Jellyfin:
        # - maxrate = bitrate + 1 triggers VBR for better bitrate allocation
        # - mbbrc 1 enables MacroBlock-level rate control
        # - bufsize = bitrate * 2 * factor, factor=2 (level ≥ 5.1);
        #   Jellyfin uses factor=1 only for level < 5.1; without codec-level
        #   detection we default to factor=2
        # - rc_init_occupancy = bitrate * 1 * factor (2 s initial buffer fill)
        cmd.extend(
            [
                "-preset",
                "veryfast",
                "-b:v",
                bitrate,
                "-maxrate",
                str(bitrate_num + 1) + "k",
                "-bufsize",
                str(bitrate_num * 2 * 2) + "k",
                "-mbbrc",
                "1",
                "-rc_init_occupancy",
                str(bitrate_num * 2 * 1000),
            ]
        )

    elif enc == "h264_nvenc":
        bitrate = _HW_BITRATE.get(options.quality, "3000k")
        nvenc_preset = (
            "p4"
            if options.quality == "medium"
            else ("p7" if options.quality == "high" else "p1")
        )
        cmd.extend(
            [
                "-preset",
                nvenc_preset,
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                "-bufsize",
                str(int(bitrate[:-1]) * 2) + "k",
            ]
        )

    elif enc == "h264_vaapi":
        # CQP is the safest RC mode — universally supported across Intel iHD
        # and i965 drivers. VBR / CBR may be unavailable on some GPUs.
        # Reuse CRF values as QP targets (lower = higher quality, ~0–51).
        cmd.extend(
            [
                "-rc_mode",
                "CQP",
                "-qp",
                str(options.crf),
            ]
        )

    elif enc == "h264_videotoolbox":
        bitrate = _HW_BITRATE.get(options.quality, "3000k")
        vt_prio = "0" if options.quality in ("high", "medium") else "1"
        cmd.extend(
            [
                "-b:v",
                bitrate,
                # qmin=-1 / qmax=-1 disable quantization constraints,
                # letting the encoder use pure bitrate-based rate control
                "-qmin",
                "-1",
                "-qmax",
                "-1",
                "-prio_speed",
                vt_prio,
            ]
        )

    else:
        bitrate = _HW_BITRATE.get(options.quality, "3000k")
        cmd.extend(
            [
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                "-bufsize",
                str(int(bitrate[:-1]) * 2) + "k",
            ]
        )

    if vf_parts:
        cmd.extend(["-vf", ",".join(vf_parts)])

    # -------------------- Keyframe / GOP --------------------

    _FORCE_KEYFRAMES = ("libx264", "h264_vaapi")
    _GOP_ENCODERS = ("h264_qsv", "h264_nvenc")

    if enc in _FORCE_KEYFRAMES:
        cmd.extend(
            [
                "-force_key_frames:0",
                f"expr:gte(t,n_forced*{seg_len})",
            ]
        )
        if enc == "libx264":
            # prevent libx264 from inserting scene-change keyframes that
            # would break the uniform segment duration
            cmd.extend(["-sc_threshold:v:0", "0"])

    elif enc in _GOP_ENCODERS:
        # GOP size = segment length × framerate, rounded up to ensure each
        # segment contains at least one keyframe
        gop = math.ceil(options.framerate * seg_len)
        cmd.extend(
            [
                "-g:v:0",
                str(gop),
                "-keyint_min:v:0",
                str(gop),
            ]
        )

    else:
        # unknown encoder: apply both strategies for safety
        gop = math.ceil(options.framerate * seg_len)
        cmd.extend(
            [
                "-force_key_frames:0",
                f"expr:gte(t,n_forced*{seg_len})",
                "-g:v:0",
                str(gop),
                "-keyint_min:v:0",
                str(gop),
            ]
        )

    # -------------------- Audio --------------------
    cmd.extend(
        [
            "-c:a",
            "aac",
            "-profile:a",
            "aac_low",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
        ]
    )

    # preserve original timestamps and disable negative timestamp avoidance
    cmd.extend(["-copyts", "-avoid_negative_ts", "disabled"])

    # -------------------- HLS output --------------------
    m3u8_path = str(out_dir / "index.m3u8")
    segment_pattern = str(out_dir / "segment_%06d.ts")
    cmd.extend(
        [
            "-f",
            "hls",
            "-hls_time",
            str(seg_len),
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            segment_pattern,
            "-hls_flags",
            "append_list",
            "-start_number",
            "0",
            "-max_delay",
            "5000000",
            "-max_muxing_queue_size",
            "128",
            m3u8_path,
        ]
    )

    cmd.extend(["-y", "-nostdin"])
    return cmd


def _is_complete(m3u8_path: Path) -> bool:
    """Check whether the M3U8 playlist file exists and contains the endlist tag.

    Args:
        m3u8_path: The M3U8 file path.

    Returns:
        `True` if the file exists and contains `#EXT-X-ENDLIST`,
        `False` otherwise.
    """
    if not m3u8_path.is_file():
        return False
    try:
        return "#EXT-X-ENDLIST" in m3u8_path.read_text()
    except Exception:
        return False


_SEGMENT_LINE_RE = re.compile(r"^(?!\s*#)(.+\.ts)\s*$", re.MULTILINE)
"""Regex to detect if an M3U8 playlist contains at least one segment line."""


async def _wait_segment(
    m3u8_path: Path,
    proc: asyncio.subprocess.Process | None = None,
    timeout: float = _SEGMENT_WAIT_TIMEOUT,
    interval: float = _SEGMENT_WAIT_INTERVAL,
) -> bool:
    """Block until `m3u8_path` exists and contains at least one segment.

    Args:
        m3u8_path: The M3U8 file path.
        proc: The ffmpeg subprocess to watch.
        timeout: The max seconds to wait.
        interval: The polling interval in seconds.

    Returns:
        `True` if a segment was detected within the timeout, `False` otherwise.
    """
    elapsed = 0.0
    while elapsed < timeout:
        if m3u8_path.is_file():
            try:
                async with aiofiles.open(m3u8_path, encoding=ENCODING) as f:
                    content = await f.read()
            except Exception:
                await asyncio.sleep(interval)
                elapsed += interval
                continue
            if _SEGMENT_LINE_RE.search(content.strip()):
                return True

        # no more segments can be produced after ffmpeg exits
        if proc is not None and proc.returncode is not None:
            return False

        await asyncio.sleep(interval)
        elapsed += interval
    return False


async def _monitor_ffmpeg(
    proc: asyncio.subprocess.Process, lock: FileLock, task_id: str | None = None
):
    """Wait for ffmpeg to finish, log errors, and release the lock.

    Args:
        proc: The ffmpeg subprocess to monitor.
        lock: The `FileLock` instance.
        task_id: The registered task ID, if task tracking is enabled.
    """
    stderr_data = b""
    try:
        if proc.stderr is not None:
            stderr_data = await proc.stderr.read()
    except Exception:
        pass
    await proc.wait()

    error_tail = None
    if proc.returncode not in (0, 255):
        if stderr_data:
            with contextlib.suppress(Exception):
                error_tail = stderr_data.decode(errors="replace")[-500:]
        logger.error(
            "ffmpeg HLS exited with code %d for '%s': %s",
            proc.returncode,
            Path(lock.lock_file).parent,
            error_tail or "",
        )

    if task_id:
        await finish_task(task_id, proc.returncode, error_tail)
    _release_lock(lock)
    logger.debug("ffmpeg HLS finished for '%s'", Path(lock.lock_file).parent)


_MINIMAL_M3U8 = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n"
)


async def read_m3u8(m3u8_path: Path) -> str | None:
    """Read an M3U8 playlist file.

    Args:
        m3u8_path: The M3U8 file path.

    Returns:
        The M3U8 text, `None` if the output directory doesn't exist,
        or a minimal playlist if the M3U8 file isn't ready yet.
    """
    if not m3u8_path.is_file():
        return _MINIMAL_M3U8 if m3u8_path.parent.is_dir() else None

    try:
        async with aiofiles.open(m3u8_path, encoding=ENCODING) as f:
            content = await f.read()
    except Exception:
        return _MINIMAL_M3U8

    return content if content.strip() else _MINIMAL_M3U8
