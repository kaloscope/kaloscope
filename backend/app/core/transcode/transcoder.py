import asyncio
import contextlib
import json
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from pathlib import Path

from filelock import FileLock
from sanic.log import logger

from app.core.exceptions import KaloscopeException
from app.core.transcode.capabilities import load_ffmpeg_capabilities
from app.core.transcode.hls import (
    acquire_output_lock,
    cleanup_stale_hls,
    is_complete,
    output_dir,
    wait_segment,
)
from app.core.transcode.hwaccels.base import (
    HDRType,
    MediaChapter,
    MediaProbe,
    TranscodeContext,
    classify_hdr,
)
from app.core.transcode.options import TranscodeOptions
from app.core.transcode.tasks import finish_task, register_task

_PROBE_TIMEOUT = 30.0
_TERMINATE_TIMEOUT = 5.0

_STDERR_CHUNK_SIZE = 8192
_STDERR_TAIL_SIZE = 16 * 1024
_ERROR_DETAIL_SIZE = 8 * 1024
_ERROR_DETAIL_LINES = 24

_MONITOR_TASKS: set[asyncio.Task["FFmpegCompletion"]] = set()

_EIGHT_BIT_PIXEL_FORMATS = {
    "nv12",
    "nv21",
    "yuv420p",
    "yuv422p",
    "yuv444p",
    "yuvj420p",
    "yuvj422p",
    "yuvj444p",
}


class CompletionState(StrEnum):
    """Terminal classification published by an FFmpeg monitor."""

    FINISHED = "finished"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True)
class FFmpegCompletion:
    """Bounded FFmpeg process result shared with startup and task tracking.

    Attributes:
        returncode: The process return code, or `None` when unavailable.
        state: The application-level terminal classification.
        error: A bounded and redacted stderr summary.
    """

    returncode: int | None
    state: CompletionState
    error: str | None = None

    @classmethod
    def from_exit(
        cls, returncode: int | None, error: str | None = None
    ) -> "FFmpegCompletion":
        """Classify a normal process exit code."""
        if returncode == 0:
            state = CompletionState.FINISHED
        elif returncode == 255:
            state = CompletionState.STOPPED
        else:
            state = CompletionState.FAILED
        return cls(returncode, state, error)


def _stderr_detail(data: bytes, redactions: dict[str, str] | None = None) -> str | None:
    """Format a bounded recent stderr detail with sensitive paths redacted."""
    if not data:
        return None
    detail = data.decode(errors="replace")
    if redactions:
        # longer values must be replaced before any overlapping path suffixes
        for value in sorted(redactions, key=len, reverse=True):
            if value:
                detail = detail.replace(value, redactions[value])
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    if not lines:
        return None
    detail = "\n".join(lines[-_ERROR_DETAIL_LINES:])
    encoded = detail.encode()
    if len(encoded) > _ERROR_DETAIL_SIZE:
        detail = encoded[-_ERROR_DETAIL_SIZE:].decode(errors="ignore")
    return detail or None


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
    """Resolve ffprobe next to a configured ffmpeg or use "ffprobe".

    Returns:
        The ffprobe executable name or path.
    """
    ffmpeg = await _ffmpeg()
    if ffmpeg != "ffmpeg":
        path = Path(ffmpeg).with_name("ffprobe")
        if path.is_file():
            return str(path)
    return "ffprobe"


def _optional_int(value: object, *, positive: bool = False) -> int | None:
    """Parse an integer field while rejecting booleans and invalid values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed < 0 or (positive and parsed == 0):
        return None
    return parsed


def _optional_flag(value: object) -> bool | None:
    """Parse an ffprobe 0/1 flag while preserving a missing value."""
    parsed = _optional_int(value)
    if parsed == 0:
        return False
    if parsed == 1:
        return True
    return None


def _pixel_format_bit_depth(pixel_format: str | None) -> int | None:
    """Infer component bit depth from a known FFmpeg pixel-format name."""
    if not pixel_format:
        return None
    name = pixel_format.lower()
    if name in _EIGHT_BIT_PIXEL_FORMATS:
        return 8
    planar_match = re.search(r"(?:p|gray)(9|10|12|14|16)(?:le|be)?$", name)
    if planar_match:
        return int(planar_match.group(1))
    packed_match = re.fullmatch(r"p0(10|12|16)(?:le|be)?", name)
    if packed_match:
        return int(packed_match.group(1))
    return None


def _parse_sample_aspect_ratio(value: object) -> tuple[int, int] | None:
    """Parse and reduce a positive FFprobe sample-aspect-ratio value."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(\d+)\s*[:/]\s*(\d+)\s*", value)
    if not match:
        return None
    numerator, denominator = (int(part) for part in match.groups())
    if numerator <= 0 or denominator <= 0:
        return None
    divisor = math.gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor


def _parse_frame_rate(value: object) -> Fraction | None:
    """Parse a positive FFprobe frame-rate rational without losing precision."""
    if not isinstance(value, str):
        return None
    try:
        rate = Fraction(value.strip())
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _parse_rotation(value: object) -> int | None:
    """Convert FFprobe counter-clockwise rotation to canonical clockwise degrees."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    raw = float(value)
    if not math.isfinite(raw):
        return None
    clockwise = (-raw) % 360
    for supported in (0, 90, 180, 270):
        distance = abs(clockwise - supported)
        if min(distance, 360 - distance) <= 0.1:
            return supported
    return round(clockwise)


async def _probe_hdr10_plus(media_path: str, stream_index: int) -> bool:
    """Detect HDR10+ dynamic metadata from the selected stream's first frame.

    Args:
        media_path: The media file path to probe.
        stream_index: The selected video stream index.

    Returns:
        Whether the first decoded frame carries SMPTE ST 2094-40 metadata.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            await _ffprobe(),
            "-v",
            "quiet",
            "-select_streams",
            str(stream_index),
            "-read_intervals",
            "%+#1",
            "-show_frames",
            "-show_entries",
            "frame=stream_index:frame_side_data=side_data_type",
            "-of",
            "json",
            media_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        return False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT)
    except TimeoutError:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        await proc.communicate()
        return False
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(stdout.decode())
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False

    frames = data.get("frames")
    if not isinstance(frames, list):
        return False
    expected_type = "hdr dynamic metadata smpte2094-40 (hdr10+)"
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        side_data_list = frame.get("side_data_list")
        if not isinstance(side_data_list, list):
            continue
        for side_data in side_data_list:
            if not isinstance(side_data, dict):
                continue
            side_data_type = side_data.get("side_data_type")
            if (
                isinstance(side_data_type, str)
                and side_data_type.lower() == expected_type
            ):
                return True
    return False


async def probe_media(media_path: str) -> MediaProbe:
    """Probe the container and select its primary video and audio streams.

    Args:
        media_path: The media file path to probe.

    Returns:
        Named probe values. Invalid or unavailable metadata uses the
        corresponding `MediaProbe` default.

    Raises:
        OSError: If the ffprobe process cannot be started.
        UnicodeDecodeError: If ffprobe output is not valid UTF-8.
    """
    proc = await asyncio.create_subprocess_exec(
        await _ffprobe(),
        "-v",
        "quiet",
        "-show_entries",
        (
            "format=duration:chapter=id,start_time,end_time:chapter_tags=title:"
            "stream=index,codec_type,codec_name,profile,"
            "bits_per_sample,bits_per_raw_sample,avg_frame_rate,r_frame_rate,"
            "pix_fmt,width,height,"
            "sample_aspect_ratio,field_order,"
            "color_range,color_transfer,color_primaries,color_space:"
            "stream_disposition=attached_pic:stream_side_data=side_data_type,"
            "rotation,dv_profile,bl_present_flag,dv_bl_signal_compatibility_id"
        ),
        "-of",
        "json",
        media_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT)
    except TimeoutError:
        logger.warning(
            "ffprobe timed out after %.1f seconds for '%s'",
            _PROBE_TIMEOUT,
            media_path,
        )
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        await proc.communicate()
        return MediaProbe()
    if proc.returncode != 0:
        return MediaProbe()
    try:
        data = json.loads(stdout.decode())
    except (json.JSONDecodeError, TypeError):
        return MediaProbe()
    if not isinstance(data, dict):
        return MediaProbe()

    duration = None
    format_data = data.get("format")
    if isinstance(format_data, dict):
        raw_duration = format_data.get("duration")
        if isinstance(raw_duration, (str, int, float)):
            with contextlib.suppress(ValueError, TypeError):
                duration = float(raw_duration)

    chapters: list[MediaChapter] = []
    raw_chapters = data.get("chapters")
    if isinstance(raw_chapters, list):
        for index, chapter in enumerate(raw_chapters, start=1):
            if not isinstance(chapter, dict):
                continue
            try:
                start = float(chapter.get("start_time"))
                end = float(chapter.get("end_time"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(start) or not math.isfinite(end) or start < 0:
                continue
            if end <= start:
                continue
            raw_id = chapter.get("id")
            chapter_id = str(raw_id) if isinstance(raw_id, (int, str)) else str(index)
            tags = chapter.get("tags")
            raw_title = tags.get("title") if isinstance(tags, dict) else None
            title = (
                raw_title.strip()
                if isinstance(raw_title, str) and raw_title.strip()
                else f"Chapter {index}"
            )
            chapters.append(MediaChapter(chapter_id, title, start, end))
        chapters.sort(key=lambda chapter: chapter.start)

    avg_frame_rate = None
    r_frame_rate = None
    codec = None
    profile = None
    pixel_format = None
    bit_depth = None
    color_range = None
    source_height = None
    source_width = None
    sample_aspect_ratio = None
    rotation = None
    field_order = None
    color_transfer = None
    color_primaries = None
    color_space = None
    video_index = None
    audio_index = None
    dovi_profile = None
    dovi_bl_present = None
    dovi_compat_id = None
    hdr10_plus = False
    video_stream = None
    streams = data.get("streams")
    if isinstance(streams, list):
        # attached pictures are skipped so playback selects the first real video
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            raw_index = stream.get("index")
            if (
                not isinstance(raw_index, int)
                or isinstance(raw_index, bool)
                or raw_index < 0
            ):
                continue
            codec_type = stream.get("codec_type")
            if codec_type == "audio" and audio_index is None:
                audio_index = raw_index
            elif codec_type == "video" and video_stream is None:
                disposition = stream.get("disposition")
                attached_pic = (
                    disposition.get("attached_pic")
                    if isinstance(disposition, dict)
                    else 0
                )
                if attached_pic not in (1, "1", True):
                    video_index = raw_index
                    video_stream = stream

    if video_stream is not None:
        stream = video_stream
        raw_codec = stream.get("codec_name")
        if isinstance(raw_codec, str) and raw_codec:
            codec = raw_codec

        raw_profile = stream.get("profile")
        if isinstance(raw_profile, str) and raw_profile:
            profile = raw_profile

        raw_height = stream.get("height")
        if isinstance(raw_height, int) and raw_height > 0:
            source_height = raw_height

        raw_width = stream.get("width")
        if isinstance(raw_width, int) and raw_width > 0:
            source_width = raw_width

        sample_aspect_ratio = _parse_sample_aspect_ratio(
            stream.get("sample_aspect_ratio")
        )

        raw_field_order = stream.get("field_order")
        if isinstance(raw_field_order, str):
            normalized_order = raw_field_order.lower()
            if normalized_order in {
                "progressive",
                "tt",
                "bb",
                "tb",
                "bt",
                "unknown",
            }:
                field_order = normalized_order

        raw_pixel_format = stream.get("pix_fmt")
        if isinstance(raw_pixel_format, str) and raw_pixel_format:
            pixel_format = raw_pixel_format

        # explicit stream depth takes precedence over pixel-format inference
        bit_depth = (
            _optional_int(stream.get("bits_per_raw_sample"), positive=True)
            or _optional_int(stream.get("bits_per_sample"), positive=True)
            or _pixel_format_bit_depth(pixel_format)
        )

        raw_color_range = stream.get("color_range")
        if isinstance(raw_color_range, str) and raw_color_range:
            color_range = raw_color_range

        raw_color_transfer = stream.get("color_transfer")
        if isinstance(raw_color_transfer, str) and raw_color_transfer:
            color_transfer = raw_color_transfer

        raw_color_primaries = stream.get("color_primaries")
        if isinstance(raw_color_primaries, str) and raw_color_primaries:
            color_primaries = raw_color_primaries

        raw_color_space = stream.get("color_space")
        if isinstance(raw_color_space, str) and raw_color_space:
            color_space = raw_color_space

        side_data_list = stream.get("side_data_list")
        if isinstance(side_data_list, list):
            for side_data in side_data_list:
                if not isinstance(side_data, dict):
                    continue
                side_data_type = side_data.get("side_data_type")
                if not isinstance(side_data_type, str):
                    continue
                normalized_type = side_data_type.lower()
                if normalized_type == "display matrix" and rotation is None:
                    rotation = _parse_rotation(side_data.get("rotation"))
                elif normalized_type == "dovi configuration record":
                    dovi_profile = _optional_int(
                        side_data.get("dv_profile"), positive=True
                    )
                    dovi_bl_present = _optional_flag(side_data.get("bl_present_flag"))
                    dovi_compat_id = _optional_int(
                        side_data.get("dv_bl_signal_compatibility_id")
                    )

        avg_frame_rate = _parse_frame_rate(stream.get("avg_frame_rate"))
        r_frame_rate = _parse_frame_rate(stream.get("r_frame_rate"))
        # frame probing is reserved for plausible PQ sources to avoid extra work
        if (
            video_index is not None
            and bit_depth is not None
            and bit_depth >= 10
            and color_transfer is not None
            and color_transfer.lower() == "smpte2084"
        ):
            hdr10_plus = await _probe_hdr10_plus(media_path, video_index)
    return MediaProbe(
        video_stream_index=video_index,
        audio_stream_index=audio_index,
        height=source_height,
        width=source_width,
        sample_aspect_ratio=sample_aspect_ratio,
        rotation=rotation,
        field_order=field_order,
        duration=duration,
        chapters=tuple(chapters),
        avg_frame_rate=avg_frame_rate,
        r_frame_rate=r_frame_rate,
        codec=codec,
        profile=profile,
        pixel_format=pixel_format,
        bit_depth=bit_depth,
        color_range=color_range,
        color_transfer=color_transfer,
        color_primaries=color_primaries,
        color_space=color_space,
        hdr10_plus=hdr10_plus,
        dovi_profile=dovi_profile,
        dovi_bl_present=dovi_bl_present,
        dovi_bl_signal_compatibility_id=dovi_compat_id,
    )


def _require_video_stream(metadata: MediaProbe) -> int:
    """Return the selected video index or reject unsupported media input."""
    if metadata.video_stream_index is None:
        raise RuntimeError("Input has no transcodable video stream")
    return metadata.video_stream_index


def _require_supported_hdr(metadata: MediaProbe):
    """Reject HDR formats that cannot be decoded through a compatible base layer."""
    if classify_hdr(metadata) is HDRType.DOVI_ONLY:
        raise RuntimeError("Dolby Vision-only input is not supported")


def _require_supported_geometry(metadata: MediaProbe, options: TranscodeOptions):
    """Reject unsupported rotation or an incomplete required geometry plan."""
    rotation = metadata.rotation or 0
    if rotation not in {0, 90, 180, 270}:
        raise RuntimeError(f"Unsupported video rotation: {rotation} degrees")

    sar = metadata.sample_aspect_ratio or (1, 1)
    requires_geometry = rotation != 0 or sar != (1, 1) or options.max_height is not None
    if requires_geometry and (metadata.width is None or metadata.height is None):
        raise RuntimeError("Video geometry transform requires valid width and height")


async def _prepare_hardware(context: TranscodeContext):
    """Prepare selected hardware before building the real transcode command."""
    from app.core.transcode.hwaccels import get_hwaccel

    strategy = get_hwaccel(context.options.hwaccel)
    context.hardware = await strategy.prepare_hardware(context)


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

    if is_complete(m3u8_path):
        logger.debug("HLS already complete: %s", out_dir)
        return media_hash, profile

    # another worker owns the output lock while transcoding this profile
    lock = _acquire_lock(out_dir)
    if lock is None:
        logger.debug("HLS transcode already in progress: %s", out_dir)
        if not await wait_segment(m3u8_path):
            raise RuntimeError("HLS first segment was not ready in time")
        return media_hash, profile

    # another process may have completed after the initial check but before
    # this process acquired the lock
    if is_complete(m3u8_path):
        logger.debug("HLS completed while acquiring the lock: %s", out_dir)
        _release_lock(lock)
        return media_hash, profile

    lock_handed_off = False
    proc: asyncio.subprocess.Process | None = None
    try:
        cleanup_stale_hls(out_dir)

        metadata = await probe_media(media_path)
        _require_video_stream(metadata)
        _require_supported_hdr(metadata)
        _require_supported_geometry(metadata, options)
        capabilities = await load_ffmpeg_capabilities(await _ffmpeg(), options.encoder)
        context = TranscodeContext(
            options=options,
            media_path=media_path,
            metadata=metadata,
            capabilities=capabilities,
        )

        await _prepare_hardware(context)
        cmd = await _build_hls_cmd(media_path, out_dir, context)
        logger.info("Starting ffmpeg HLS: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        task_id = await register_task(
            media_path,
            media_hash,
            context.options,
            out_dir,
            proc,
            metadata.duration,
        )

        completion = _start_monitor(
            proc,
            lock,
            task_id,
            redactions={
                media_path: "<input>",
                Path(media_path).name: "<input>",
                str(out_dir): "<output>",
            },
        )
        # the monitor owns process cleanup and lock release after this handoff
        lock_handed_off = True

        if not await wait_segment(m3u8_path, proc=proc):
            if proc.returncode is not None:
                result = await asyncio.shield(completion)
                message = f"FFmpeg failed with code {result.returncode}"
                if result.error:
                    message = f"{message}: {result.error}"
                raise KaloscopeException(message)
            raise RuntimeError("HLS first segment was not ready in time")

    except Exception:
        if not lock_handed_off:
            if proc is not None:
                await _terminate_ffmpeg(proc)
            _release_lock(lock)
        raise

    return media_hash, profile


async def _build_hls_cmd(
    input_path: str, out_dir: Path, context: TranscodeContext
) -> list[str]:
    """Build the ffmpeg command line for HLS transcoding.

    Combines strategy-specific decoding, encoding, filtering, and keyframe
    arguments with shared audio, timestamp, and HLS output settings.

    The command structure, argument ordering, and per-encoder parameters are
    referenced from Jellyfin: https://github.com/jellyfin/jellyfin

    Args:
        input_path: The source media file path.
        out_dir: The output directory for M3U8 playlist and TS segments.
        context: The runtime transcode context.

    Returns:
        A list of command-line arguments ready for `asyncio.create_subprocess_exec`.
    """
    metadata = context.metadata
    video_index = _require_video_stream(metadata)
    audio_index = metadata.audio_stream_index

    _require_supported_hdr(metadata)
    _require_supported_geometry(metadata, context.options)

    executable = (
        context.capabilities.executable
        if context.capabilities is not None
        else await _ffmpeg()
    )
    cmd = [executable, "-hide_banner", "-loglevel", "error"]

    options = context.options

    from app.core.transcode.hwaccels import get_hwaccel

    hwaccel = get_hwaccel(options.hwaccel)
    # strategy input args decide whether decoded frames remain on the device
    cmd.extend(await hwaccel.input_args(context))

    cmd.extend(["-display_rotation", "0", "-noautorotate", "-i", input_path])

    # strip metadata and chapters that web playback does not use
    cmd.extend(
        [
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-metadata:s:v:0",
            "rotate=0",
        ]
    )

    cmd.extend(["-map", f"0:{video_index}"])
    if audio_index is not None:
        cmd.extend(["-map", f"0:{audio_index}"])
    else:
        cmd.append("-an")

    vf_parts = hwaccel.video_filters(context)

    _validate_capabilities(context, vf_parts)

    enc = context.encoder_config.encoder
    cmd.extend(["-c:v", enc])
    cmd.extend(hwaccel.encoder_args(context))

    if vf_parts:
        cmd.extend(["-vf", ",".join(vf_parts)])

    if context.is_interlaced:
        cmd.extend(["-field_order", "progressive"])

    if context.needs_tonemap and (
        context.capabilities is None
        or context.capabilities.supports_bsf("h264_metadata")
    ):
        # tone-mapped output must advertise SDR color metadata
        cmd.extend(
            [
                "-bsf:v",
                (
                    "h264_metadata=colour_primaries=1:"
                    "transfer_characteristics=1:matrix_coefficients=1:"
                    "video_full_range_flag=0"
                ),
            ]
        )

    cmd.extend(hwaccel.keyframe_args(context))

    if audio_index is not None:
        # web playback uses one normalized AAC stereo track
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

    cmd.extend(["-copyts", "-avoid_negative_ts", "disabled"])

    m3u8_path = str(out_dir / "index.m3u8")
    segment_pattern = str(out_dir / "segment_%06d.ts")
    # event playlists expose completed segments while encoding continues
    cmd.extend(
        [
            "-f",
            "hls",
            "-hls_time",
            str(context.options.segment_length),
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "event",
            "-hls_segment_type",
            "mpegts",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_filename",
            segment_pattern,
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


def _validate_capabilities(context: TranscodeContext, video_filters: list[str]):
    """Reject capabilities that remain mandatory in the generated command."""
    capabilities = context.capabilities
    if capabilities is None:
        return

    required_encoders = {context.options.encoder}
    if context.metadata.audio_stream_index is not None:
        required_encoders.add("aac")
    required_filters = {
        expression.split("=", 1)[0].strip()
        for expression in video_filters
        if expression.strip()
    }
    required_muxers = {"hls", "mpegts"}

    missing = {
        "encoders": sorted(required_encoders - capabilities.encoders),
        "filters": sorted(required_filters - capabilities.filters),
        "muxers": sorted(required_muxers - capabilities.muxers),
    }
    details = [
        f"{kind}: {', '.join(names)}" for kind, names in missing.items() if names
    ]
    if details:
        raise RuntimeError(
            f"FFmpeg '{capabilities.executable}' is missing required capabilities: "
            + "; ".join(details)
        )


def _acquire_lock(out_dir: Path) -> FileLock | None:
    """Try to acquire an exclusive transcode lock for the given output directory.

    Uses the shared non-blocking output lock also used by deletion operations.

    Args:
        out_dir: The output directory to lock.

    Returns:
        The acquired `FileLock` instance if successful,
        or `None` if another process holds the lock.
    """
    lock = acquire_output_lock(out_dir)
    if lock is None:
        return None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        return lock
    except OSError:
        _release_lock(lock)
        raise


def _release_lock(lock: FileLock):
    """Release the transcode lock, suppressing any exceptions.

    Args:
        lock: The `FileLock` instance to release.
    """
    with contextlib.suppress(Exception):
        lock.release()


async def _terminate_ffmpeg(proc: asyncio.subprocess.Process):
    """Stop an unmonitored ffmpeg process without masking setup errors.

    Sends a graceful termination request first, then kills the process if it
    does not exit before the configured timeout. Cleanup failures are logged
    instead of replacing the original setup exception.

    Args:
        proc: The ffmpeg subprocess that failed before monitor handoff.
    """
    try:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
        try:
            await asyncio.wait_for(proc.communicate(), timeout=_TERMINATE_TIMEOUT)
        except TimeoutError:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.communicate()
    except Exception:
        logger.warning(
            "Failed to stop unmonitored ffmpeg process %s",
            getattr(proc, "pid", None),
            exc_info=True,
        )


async def _monitor_ffmpeg(
    proc: asyncio.subprocess.Process,
    lock: FileLock,
    task_id: str | None = None,
    completion: asyncio.Future[FFmpegCompletion] | None = None,
    redactions: dict[str, str] | None = None,
) -> FFmpegCompletion:
    """Monitor ffmpeg completion, update task state, and release the lock.

    Reads stderr and retains a bounded tail for failed tasks. Cancellation
    stops the subprocess, records the task as stopped, and then propagates
    cancellation.

    Args:
        proc: The ffmpeg subprocess to monitor.
        lock: The `FileLock` instance.
        task_id: The registered task ID, if task tracking is enabled.
        completion: The future that receives the process result, if requested.
        redactions: Sensitive stderr values mapped to safe placeholders.
    """
    try:
        stderr_data = bytearray()
        try:
            if proc.stderr is not None:
                while chunk := await proc.stderr.read(_STDERR_CHUNK_SIZE):
                    stderr_data.extend(chunk)
                    if len(stderr_data) > _STDERR_TAIL_SIZE:
                        del stderr_data[:-_STDERR_TAIL_SIZE]
        except Exception:
            # still reap and classify the process when stderr capture fails
            pass
        await proc.wait()

        error = None
        # treat exit 255 as an application-requested stop
        if proc.returncode not in (0, 255) and stderr_data:
            error = _stderr_detail(bytes(stderr_data), redactions)

        result = FFmpegCompletion.from_exit(proc.returncode, error)
        if completion is not None and not completion.done():
            completion.set_result(result)

        if result.state is CompletionState.FAILED:
            logger.error(
                "ffmpeg HLS exited with code %s for task '%s': %s",
                proc.returncode,
                task_id or proc.pid,
                error or "",
            )

        if task_id:
            await finish_task(task_id, proc.returncode, error)
    except asyncio.CancelledError:
        await _terminate_ffmpeg(proc)
        result = FFmpegCompletion(
            proc.returncode,
            CompletionState.STOPPED,
        )
        if completion is not None and not completion.done():
            completion.set_result(result)
        if task_id:
            try:
                await finish_task(task_id, 255)
            except Exception:
                logger.error(
                    "Failed to stop cancelled transcode task: %s",
                    task_id,
                    exc_info=True,
                )
        raise
    finally:
        _release_lock(lock)
    logger.debug("ffmpeg HLS finished for task '%s'", task_id or proc.pid)
    return result


def _start_monitor(
    proc: asyncio.subprocess.Process,
    lock: FileLock,
    task_id: str | None = None,
    redactions: dict[str, str] | None = None,
) -> asyncio.Future[FFmpegCompletion]:
    """Start and retain an ffmpeg monitor task until it finishes.

    Args:
        proc: The ffmpeg subprocess to monitor.
        lock: The `FileLock` held for the output directory.
        task_id: The registered transcode task ID, if available.
        redactions: Sensitive stderr values mapped to safe placeholders.

    Returns:
        A future resolved with the monitored FFmpeg process result.
    """

    completion: asyncio.Future[FFmpegCompletion] = (
        asyncio.get_running_loop().create_future()
    )

    def _done(task: asyncio.Task[FFmpegCompletion]):
        """Remove a completed monitor and consume its exception."""
        _MONITOR_TASKS.discard(task)
        if task.cancelled():
            if not completion.done():
                completion.set_result(FFmpegCompletion(None, CompletionState.STOPPED))
            return
        try:
            result = task.result()
        except Exception:
            if not completion.done():
                completion.set_result(
                    FFmpegCompletion(
                        getattr(proc, "returncode", None),
                        CompletionState.FAILED,
                    )
                )
            logger.error(
                "Transcode monitor task failed: %s",
                task.get_name(),
                exc_info=True,
            )
        else:
            if not completion.done():
                completion.set_result(result)

    name = f"transcode-monitor:{task_id or proc.pid}"
    task = asyncio.create_task(
        _monitor_ffmpeg(
            proc,
            lock,
            task_id,
            completion=completion,
            redactions=redactions,
        ),
        name=name,
    )
    _MONITOR_TASKS.add(task)
    task.add_done_callback(_done)
    return completion


async def shutdown_monitors():
    """Cancel and wait for all ffmpeg monitor tasks in this worker."""
    monitors = tuple(_MONITOR_TASKS)
    if not monitors:
        return
    for task in monitors:
        task.cancel()
    await asyncio.gather(*monitors, return_exceptions=True)
    _MONITOR_TASKS.difference_update(monitors)
