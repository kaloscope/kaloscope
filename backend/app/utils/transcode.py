import asyncio
import contextlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiofiles
from filelock import FileLock, Timeout
from sanic.log import logger

from app.core.config import KaloscopeConfig
from app.core.constants import ENCODING


@dataclass
class EncoderConfig:
    """Configuration for a specific hardware encoder."""

    encoder: str = "libx264"
    hwaccel: str | None = None
    hwaccel_output_format: str | None = None


# hardware acceleration types (mapped to encoder name and ffmpeg flags)
HWAccelType = Literal[
    "amf", "qsv", "nvenc", "v4l2m2m", "vaapi", "videotoolbox", "rkmpp"
]
_ENCODER_CONFIG: dict[str | None, EncoderConfig] = {
    None: EncoderConfig(
        encoder="libx264",
        hwaccel=None,
        hwaccel_output_format=None,
    ),
    "amf": EncoderConfig(
        encoder="h264_amf",
        hwaccel="d3d11va",
        hwaccel_output_format="d3d11va",
    ),
    "qsv": EncoderConfig(
        encoder="h264_qsv",
        hwaccel="qsv",
        hwaccel_output_format="qsv",
    ),
    "nvenc": EncoderConfig(
        encoder="h264_nvenc",
        hwaccel="cuda",
        hwaccel_output_format="cuda",
    ),
    "v4l2m2m": EncoderConfig(
        encoder="h264_v4l2m2m",
        hwaccel=None,
        hwaccel_output_format=None,
    ),
    "vaapi": EncoderConfig(
        encoder="h264_vaapi",
        hwaccel="vaapi",
        hwaccel_output_format="vaapi",
    ),
    "videotoolbox": EncoderConfig(
        encoder="h264_videotoolbox",
        hwaccel="videotoolbox",
        hwaccel_output_format="videotoolbox",
    ),
    "rkmpp": EncoderConfig(
        encoder="h264_rkmpp",
        hwaccel="rkmpp",
        hwaccel_output_format=None,
    ),
}

# transcode quality levels (mapped to CRF values)
QualityLevel = Literal["low", "medium", "high"]
_QUALITY_CRF: dict[QualityLevel, int] = {"low": 28, "medium": 23, "high": 18}

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


async def probe_duration(media_path: str) -> float | None:
    """Probe the media file duration in seconds via ffprobe.

    Args:
        media_path: The media file path to probe.

    Returns:
        Duration in seconds, or `None` if probing failed.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffprobe",
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


async def ensure_transcode(
    media_path: str, media_hash: str, options: TranscodeOptions
) -> tuple[str, str]:
    profile = options.profile
    out_dir = output_dir(media_hash, profile)
    m3u8_path = out_dir / "index.m3u8"

    # Fast path: already complete.
    if _is_complete(m3u8_path):
        logger.debug("HLS already complete: %s", out_dir)
        return media_hash, profile

    # If another ffmpeg is running for this directory, just wait.
    lock = _acquire_lock(out_dir)
    if lock is None:
        logger.debug("HLS transcode already in progress: %s", out_dir)
        await _wait_segment(m3u8_path)
        return media_hash, profile

    # We hold the lock — start ffmpeg.
    try:
        cmd = _build_hls_cmd(media_path, out_dir, options)
        logger.info("Starting ffmpeg HLS: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        # Monitor completion in the background.
        asyncio.ensure_future(_monitor_ffmpeg(proc, lock))

        # Wait for at least one segment so the player can start immediately.
        await _wait_segment(m3u8_path)

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


def _build_hls_cmd(
    input_path: str, out_dir: Path, options: TranscodeOptions
) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

    enc = options.encoder
    hw = options.encoder_config
    seg_len = 6  # HLS segment length in seconds

    # -- Bitrate by quality (hardware encoders) -------------------------------
    # Jellyfin computes bitrate dynamically from source + resolution;
    # we use fixed presets for simplicity.
    hw_bitrate = {"low": "1500k", "medium": "3000k", "high": "6000k"}
    bitrate = hw_bitrate.get(options.quality, "3000k")

    # -- Hardware acceleration init (decode) ----------------------------------
    # Ref: EncodingHelper.GetHardwareVideoDecoder / GetInputVideoHwaccelArgs
    if hw.hwaccel:
        cmd.extend(["-hwaccel", hw.hwaccel])
        if hw.hwaccel_output_format:
            cmd.extend(["-hwaccel_output_format", hw.hwaccel_output_format])

    cmd.extend(["-i", input_path])

    # -- Video filter chain ---------------------------------------------------
    vf_parts: list[str] = []
    if options.max_height is not None:
        # Explicit width computation that rounds down to the nearest multiple
        # of 16, with a floor of 16 px.  This avoids the edge-case behaviour
        # of ffmpeg's `-16` flag (which can round to 0 or above source
        # width, clashing with `force_original_aspect_ratio=decrease`).
        vf_parts.append(
            f"scale='max(trunc(iw*min({options.max_height},ih)/ih/16)*16,16)'"
            f":'min({options.max_height},ih)'"
        )

    cmd.extend(["-c:v", enc])

    if enc == "libx264":
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
            ]
        )

    elif enc == "h264_amf":
        amf_quality = (
            "balanced"
            if options.quality == "medium"
            else ("quality" if options.quality == "high" else "speed")
        )
        cmd.extend(
            [
                "-quality",
                amf_quality,
                "-rc",
                "cbr",
                "-qmin",
                "0",
                "-qmax",
                "32",
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                "-bufsize",
                str(int(bitrate[:-1]) * 2) + "k",
            ]
        )

    elif enc == "h264_qsv":
        bitrate_num = int(bitrate[:-1])
        cmd.extend(
            [
                "-preset",
                "veryfast",
                "-b:v",
                bitrate,
                "-maxrate",
                str(bitrate_num + 1) + "k",
                "-bufsize",
                str(bitrate_num * 2) + "k",
                "-mbbrc",
                "1",
                "-rc_init_occupancy",
                bitrate,
            ]
        )

    elif enc == "h264_nvenc":
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
        vaapi_compression = {"low": 1, "medium": 3, "high": 7}.get(options.quality, 3)
        cmd.extend(
            [
                "-rc_mode",
                "VBR",
                "-compression_level",
                str(vaapi_compression),
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                "-bufsize",
                str(int(bitrate[:-1]) * 2) + "k",
            ]
        )

    elif enc == "h264_videotoolbox":
        vt_prio = "0" if options.quality in ("high", "medium") else "1"
        cmd.extend(
            [
                "-b:v",
                bitrate,
                "-qmin",
                "-1",
                "-qmax",
                "-1",
                "-prio_speed",
                vt_prio,
            ]
        )

    else:
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

    # -- HLS keyframe / GOP ---------------------------------------------------
    # Ref: Jellyfin uses force_key_frames for precise segment boundaries.
    cmd.extend(
        [
            "-force_key_frames:0",
            f"expr:gte(t,n_forced*{seg_len})",
            "-g:v:0",
            "48",
            "-keyint_min:v:0",
            "48",
        ]
    )
    if enc == "libx264":
        # Software encoder: disable scene-change keyframe insertion
        # so segment durations stay uniform.
        cmd.extend(["-sc_threshold", "0"])

    # -- Audio ----------------------------------------------------------------
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

    cmd.extend(["-map", "0:v:0?", "-map", "0:a:0?"])

    # -- HLS output -----------------------------------------------------------
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
            "-hls_segment_type",
            "mpegts",
            "-hls_segment_filename",
            segment_pattern,
            "-hls_flags",
            "append_list",
            "-start_number",
            "0",
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
    m3u8_path: Path, timeout: float = 10.0, interval: float = 0.25
) -> bool:
    """Block until `m3u8_path` exists and contains at least one segment.

    Args:
        m3u8_path: The M3U8 file path.
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
        await asyncio.sleep(interval)
        elapsed += interval
    return False


async def _monitor_ffmpeg(proc: asyncio.subprocess.Process, lock: FileLock):
    """Wait for ffmpeg to finish, log errors, and release the lock.

    Args:
        proc: The ffmpeg subprocess to monitor.
        lock: The `FileLock` instance.
    """
    stderr_data = b""
    try:
        if proc.stderr is not None:
            stderr_data = await proc.stderr.read()
    except Exception:
        pass
    await proc.wait()

    if proc.returncode not in (0, 255):
        tail = ""
        if stderr_data:
            with contextlib.suppress(Exception):
                tail = stderr_data.decode(errors="replace")[-500:]
        logger.error(
            "ffmpeg HLS exited with code %d for '%s': %s",
            proc.returncode,
            Path(lock.lock_file).parent,
            tail,
        )

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
