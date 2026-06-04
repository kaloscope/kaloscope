import asyncio
import contextlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiofiles
from filelock import FileLock, Timeout
from sanic.log import logger

from app.core.config import KaloscopeConfig
from app.core.constants import ENCODING
from app.models.general import GlobalConfig


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


async def _ffmpeg() -> str:
    """Get the ffmpeg executable path from global config or default to "ffmpeg".

    Returns:
        The ffmpeg executable name or path.
    """
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
        await _wait_segment(m3u8_path)
        return media_hash, profile

    # start the ffmpeg process if we acquired the lock
    try:
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

        # monitor completion in the background
        asyncio.ensure_future(_monitor_ffmpeg(proc, lock))

        # wait for at least one segment so the player can start immediately
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
    if hw.hwaccel:
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
        # and ensure the dimensions are divisible by 16 for better encoder compatibility
        vf_parts.append(
            f"scale='max(trunc(iw*min({options.max_height},ih)/ih/16)*16,16)'"
            f":'min({options.max_height},ih)'"
        )

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

    elif enc == "h264_amf":
        bitrate = _HW_BITRATE.get(options.quality, "3000k")
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
        bitrate = _HW_BITRATE.get(options.quality, "3000k")
        cmd.extend(
            [
                "-rc_mode",
                "VBR",
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
                "-bufsize",
                str(int(bitrate[:-1]) * 2) + "k",
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
    _GOP_ENCODERS = ("h264_qsv", "h264_nvenc", "h264_amf", "h264_rkmpp")

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
