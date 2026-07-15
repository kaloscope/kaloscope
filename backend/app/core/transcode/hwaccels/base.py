import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from enum import StrEnum
from fractions import Fraction
from pathlib import Path

from sanic.log import logger

from app.core.transcode.capabilities import (
    FFmpegCapabilities,
    probe_hardware_decode,
    probe_hardware_transform,
    require_hardware_encoder,
)
from app.core.transcode.options import EncoderConfig, TranscodeOptions


class HDRType(StrEnum):
    """HDR subtype inferred from explicit media metadata."""

    SDR = "sdr"
    HDR10 = "hdr10"
    HLG = "hlg"
    HDR10_PLUS = "hdr10_plus"
    DOVI_COMPATIBLE = "dovi_compatible"
    DOVI_ONLY = "dovi_only"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MediaChapter:
    """A seekable chapter embedded in a media container."""

    id: str
    title: str
    start: float
    end: float


@dataclass(frozen=True)
class MediaProbe:
    """Selected stream indexes and metadata probed from a media container."""

    video_stream_index: int | None = None
    audio_stream_index: int | None = None
    height: int | None = None
    width: int | None = None
    sample_aspect_ratio: tuple[int, int] | None = None
    rotation: int | None = None
    field_order: str | None = None
    duration: float | None = None
    chapters: tuple[MediaChapter, ...] = ()
    avg_frame_rate: Fraction | None = None
    r_frame_rate: Fraction | None = None
    codec: str | None = None
    profile: str | None = None
    pixel_format: str | None = None
    bit_depth: int | None = None
    color_range: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_space: str | None = None
    hdr10_plus: bool = False
    dovi_profile: int | None = None
    dovi_bl_present: bool | None = None
    dovi_bl_signal_compatibility_id: int | None = None


@dataclass(frozen=True)
class HardwareRuntime:
    """Prepared hardware device and source-specific runtime decisions.

    Attributes:
        device: The selected hardware device identifier.
        can_decode: Whether the source passed hardware decode probing.
        can_filter: Whether the required hardware transforms passed probing.
    """

    device: str | None
    can_decode: bool
    can_filter: bool = False


def _normalized_profile(value: str) -> str:
    """Normalize a codec profile for case-insensitive capability matching."""
    return value.lower().replace(" ", "").replace("_", "").replace("-", "")


def _is_decode_candidate(metadata: MediaProbe) -> bool:
    """Return whether source metadata is safe to verify for hardware decode.

    Args:
        metadata: The selected source stream metadata.

    Returns:
        Whether codec, profile, depth, and pixel format form a supported candidate.
    """
    codec = (metadata.codec or "").lower()
    profile = metadata.profile
    pixel_format = (metadata.pixel_format or "").lower()
    bit_depth = metadata.bit_depth
    if not profile or bit_depth is None or not pixel_format:
        return False
    normalized_profile = _normalized_profile(profile)
    if codec in {"h264", "avc"}:
        return (
            normalized_profile
            in {
                "constrainedbaseline",
                "baseline",
                "extended",
                "main",
                "high",
                "progressivehigh",
                "constrainedhigh",
            }
            and bit_depth == 8
            and pixel_format in {"yuv420p", "yuvj420p", "nv12"}
        )
    if codec in {"hevc", "h265"}:
        if normalized_profile == "main":
            return bit_depth == 8 and pixel_format in {"yuv420p", "nv12"}
        if normalized_profile == "main10":
            return bit_depth == 10 and pixel_format in {"yuv420p10le", "p010le"}
    return False


def classify_hdr(metadata: MediaProbe) -> HDRType:
    """Classify HDR metadata without guessing from incomplete HDR signals.

    Args:
        metadata: The selected source stream metadata.

    Returns:
        The HDR subtype inferred from transfer, color, depth, and Dolby Vision data.
    """
    transfer = (metadata.color_transfer or "").lower()
    is_pq = transfer == "smpte2084"
    is_hlg = transfer == "arib-std-b67"
    primaries = (metadata.color_primaries or "").lower()
    color_space = (metadata.color_space or "").lower()
    has_valid_hdr_color = (
        metadata.bit_depth is not None
        and metadata.bit_depth >= 10
        and primaries == "bt2020"
        and color_space in {"bt2020nc", "bt2020_ncl", "bt2020c", "bt2020_cl"}
    )

    if metadata.dovi_profile is not None:
        # only compatible base layers can be decoded without Dolby Vision processing
        has_compatible_base_layer = metadata.dovi_bl_present is True and (
            metadata.dovi_bl_signal_compatibility_id == 1
            or (
                metadata.dovi_bl_signal_compatibility_id == 6
                and is_pq
                and has_valid_hdr_color
            )
        )
        if has_compatible_base_layer:
            return HDRType.DOVI_COMPATIBLE
        return HDRType.DOVI_ONLY

    has_hdr_signal = is_pq or is_hlg or metadata.hdr10_plus
    if not has_hdr_signal:
        return HDRType.SDR

    # incomplete HDR signaling must not trigger an unsafe tone-map assumption
    if not has_valid_hdr_color:
        return HDRType.UNKNOWN
    if is_pq:
        return HDRType.HDR10_PLUS if metadata.hdr10_plus else HDRType.HDR10
    if is_hlg and not metadata.hdr10_plus:
        return HDRType.HLG
    return HDRType.UNKNOWN


def segment_keyframe_args(context: "TranscodeContext") -> list[str]:
    """Build timestamp-based closed-GOP options for HLS segment boundaries.

    Args:
        context: The runtime transcode context.

    Returns:
        FFmpeg options for forced keyframes and closed GOP signaling.
    """
    segment_length = context.options.segment_length
    return [
        "-force_key_frames:0",
        (f"expr:if(isnan(prev_forced_t),1,gte(t,prev_forced_t+{segment_length}))"),
        "-flags:v:0",
        "+cgop",
    ]


@dataclass
class TranscodeContext:
    """State shared while building a transcode command."""

    options: TranscodeOptions
    media_path: str | None = None
    metadata: MediaProbe = field(default_factory=MediaProbe)
    capabilities: FFmpegCapabilities | None = None
    hardware: HardwareRuntime | None = None

    @property
    def source_framerate(self) -> Fraction | None:
        # positive average source frame rate when available
        value = self.metadata.avg_frame_rate
        return value if value is not None and value > 0 else None

    @property
    def has_stable_framerate(self) -> bool:
        # whether average and base rates describe the same cadence
        average = self.source_framerate
        base = self.metadata.r_frame_rate
        if average is None or base is None or base <= 0:
            return False
        return abs(average - base) / base <= Fraction(1, 1000)

    @property
    def fixed_gop_size(self) -> int | None:
        # segment-sized GOP when the source cadence is stable
        framerate = self.source_framerate
        if not self.has_stable_framerate or framerate is None:
            return None
        return math.ceil(framerate * self.options.segment_length)

    @property
    def source_pixel_format(self) -> str | None:
        return self.metadata.pixel_format

    @property
    def source_height(self) -> int | None:
        return self.metadata.height

    @property
    def source_width(self) -> int | None:
        return self.metadata.width

    @property
    def source_sar(self) -> tuple[int, int]:
        return self.metadata.sample_aspect_ratio or (1, 1)

    @property
    def rotation(self) -> int:
        return self.metadata.rotation or 0

    @property
    def display_width(self) -> Fraction | None:
        # displayed width after sample aspect ratio and rotation
        width = self.source_width
        height = self.source_height
        if width is None or height is None:
            return None
        numerator, denominator = self.source_sar
        display_width = Fraction(width * numerator, denominator)
        # quarter-turn rotation swaps the display axes
        if self.rotation in {90, 270}:
            return Fraction(height)
        return display_width

    @property
    def display_height(self) -> Fraction | None:
        # displayed height after sample aspect ratio and rotation
        width = self.source_width
        height = self.source_height
        if width is None or height is None:
            return None
        numerator, denominator = self.source_sar
        if self.rotation in {90, 270}:
            return Fraction(width * numerator, denominator)
        return Fraction(height)

    @property
    def needs_downscale(self) -> bool:
        # whether displayed height exceeds the requested limit
        max_height = self.options.max_height
        if max_height is None:
            return False
        display_height = self.display_height
        return display_height is None or display_height > max_height

    @property
    def needs_square_pixels(self) -> bool:
        return self.source_sar != (1, 1)

    @property
    def needs_scale(self) -> bool:
        return self.needs_downscale or self.needs_square_pixels

    @property
    def needs_rotation(self) -> bool:
        return self.rotation in {90, 180, 270}

    @property
    def is_interlaced(self) -> bool:
        return self.metadata.field_order in {"tt", "tb", "bb", "bt"}

    @property
    def field_parity(self) -> str | None:
        # ffmpeg deinterlace parity for the source field order
        if self.metadata.field_order in {"tt", "tb"}:
            return "tff"
        if self.metadata.field_order in {"bb", "bt"}:
            return "bff"
        return None

    @property
    def scale_height(self) -> str | None:
        # even output height expression or fixed pixel value
        max_height = self.options.max_height
        if not self.needs_scale:
            return None
        display_height = self.display_height
        if display_height is None:
            if max_height is None:
                return None
            return f"trunc(min({max_height},ih)/2)*2"
        target = display_height
        if max_height is not None:
            target = min(target, Fraction(max_height))
        # chroma-subsampled output requires an even height
        aligned = max(int(target) // 2 * 2, 2)
        return str(aligned)

    @property
    def scale_width(self) -> str | None:
        # output width expression or value aligned to 16 pixels
        height = self.scale_height
        if height is None:
            return None
        display_width = self.display_width
        display_height = self.display_height
        if display_width is None or display_height is None:
            return f"max(trunc(iw*{height}/ih/16)*16,16)"
        target = display_width * int(height) / display_height
        # align output width to a 16-pixel boundary
        aligned = max(int(target) // 16 * 16, 16)
        return str(aligned)

    @property
    def hdr_type(self) -> HDRType:
        return classify_hdr(self.metadata)

    @property
    def is_hdr10(self) -> bool:
        return self.hdr_type in {
            HDRType.HDR10,
            HDRType.HDR10_PLUS,
            HDRType.DOVI_COMPATIBLE,
        }

    @property
    def is_hlg(self) -> bool:
        return self.hdr_type is HDRType.HLG

    @property
    def needs_tonemap(self) -> bool:
        # whether the source must be converted from HDR to SDR
        return self.hdr_type in {
            HDRType.HDR10,
            HDRType.HLG,
            HDRType.HDR10_PLUS,
            HDRType.DOVI_COMPATIBLE,
        }

    @property
    def encoder_config(self) -> EncoderConfig:
        return self.options.encoder_config

    @property
    def device(self) -> str | None:
        return self.hardware.device if self.hardware is not None else None

    def supports_filter(self, name: str) -> bool:
        """Return whether a filter is available or capabilities are unprobed."""
        return self.capabilities is None or self.capabilities.supports_filter(name)

    def supports_hwaccel(self, name: str) -> bool:
        """Return whether hardware decoding is available or unprobed."""
        return self.capabilities is None or self.capabilities.supports_hwaccel(name)

    def supports_encoder_option(self, name: str) -> bool:
        """Return whether an encoder option is available or unprobed."""
        return self.capabilities is None or self.capabilities.supports_encoder_option(
            name
        )

    def require_encoder_option(self, name: str):
        """Reject a selected encoder that lacks a correctness-critical option."""
        if self.capabilities is not None and not self.supports_encoder_option(name):
            raise RuntimeError(
                f"FFmpeg '{self.capabilities.executable}' is missing required "
                f"capabilities: encoder options: {name}"
            )

    @property
    def uses_hw_decode(self) -> bool:
        # whether the selected strategy can request hardware decoding
        if self.hardware is not None:
            return self.hardware.can_decode
        hwaccel = self.encoder_config.hwaccel
        return hwaccel is not None and self.supports_hwaccel(hwaccel)

    @property
    def uses_hw_filters(self) -> bool:
        # whether source-specific hardware transforms passed probing
        return self.hardware is not None and self.hardware.can_filter

    @property
    def needs_cpu_geometry(self) -> bool:
        # whether geometry transforms must run in system memory
        return self.needs_scale or (
            (self.needs_rotation or self.is_interlaced) and not self.uses_hw_filters
        )


def cpu_tonemap_filters(context: TranscodeContext, output_format: str) -> list[str]:
    """Build a standard-FFmpeg CPU HDR-to-SDR filter chain.

    Args:
        context: The runtime transcode context.
        output_format: The final software pixel format.

    Returns:
        Ordered filters for linearization, tone mapping, and BT.709 conversion.
    """
    linear = "zscale=transfer=linear:npl=100"
    width = context.scale_width
    height = context.scale_height
    if width is not None and height is not None:
        linear += f":w='{width}':h='{height}'"
    return [
        linear,
        "format=gbrpf32le",
        "tonemap=hable:desat=0",
        "zscale=primaries=bt709:transfer=bt709:matrix=bt709:range=tv",
        f"format={output_format}",
    ]


def cpu_geometry_filters(
    context: TranscodeContext, *, include_scale: bool = True
) -> list[str]:
    """Build ordered system-memory deinterlace, rotation, and scale filters.

    Args:
        context: The runtime transcode context.
        include_scale: Whether to append scale and square-pixel normalization.

    Returns:
        Ordered software geometry filter expressions.
    """
    filters: list[str] = []
    parity = context.field_parity
    if parity is not None:
        filters.extend(
            [
                f"bwdif=mode=send_frame:parity={parity}:deint=all",
                "setfield=prog",
            ]
        )

    if context.rotation == 90:
        filters.append("transpose=clock")
    elif context.rotation == 180:
        filters.extend(["transpose=clock", "transpose=clock"])
    elif context.rotation == 270:
        filters.append("transpose=cclock")

    if include_scale:
        width = context.scale_width
        height = context.scale_height
        if width is not None and height is not None:
            filters.extend([f"scale={width}:{height}", "setsar=1"])
    return filters


def rotation_direction(rotation: int) -> str | None:
    """Map clockwise rotation to an FFmpeg hardware filter direction.

    Args:
        rotation: The clockwise rotation in degrees.

    Returns:
        The hardware-filter direction, or `None` when no rotation is needed.
    """
    return {90: "clock", 180: "reversal", 270: "cclock"}.get(rotation)


async def resolve_vaapi_device() -> str | None:
    """Get the VAAPI render device path.

    Uses the `vaapi.device` global setting when it contains a path; otherwise,
    checks the standard render node `/dev/dri/renderD128`.

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


class HWAccelStrategy(ABC):
    """Base interface for FFmpeg hardware acceleration strategies."""

    async def resolve_device(self, context: TranscodeContext) -> str | None:
        """Resolve the device used by this hardware strategy."""
        return None

    def encoder_probe_args(self, context: TranscodeContext) -> list[str]:
        """Build a one-frame hardware encoder health probe."""
        return [
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=1",
            "-frames:v",
            "1",
            "-an",
            "-c:v",
            context.options.encoder,
            "-f",
            "null",
            "-",
        ]

    def allows_decode(self, context: TranscodeContext) -> bool:
        """Return whether this strategy may probe hardware source decoding."""
        return _is_decode_candidate(context.metadata)

    def transform_filters(self, context: TranscodeContext) -> list[str]:
        """Build optional source-specific hardware transform filters."""
        return []

    def transform_filter_names(self, context: TranscodeContext) -> set[str]:
        """Return advertised filter names required by hardware transforms."""
        return set()

    def transform_download_format(self, context: TranscodeContext) -> str:
        """Return the software pixel format produced by the transform probe."""
        return "p010le" if context.metadata.bit_depth == 10 else "nv12"

    def keep_decode_on_fallback(self, context: TranscodeContext) -> bool:
        """Return whether CPU filters can consume downloaded hardware decode."""
        return True

    async def prepare_hardware(
        self, context: TranscodeContext
    ) -> HardwareRuntime | None:
        """Validate hardware encoding and choose decoding for one source.

        Args:
            context: The runtime transcode context.

        Returns:
            Source-specific hardware decisions, or `None` for software encoding.

        Raises:
            RuntimeError: If mandatory encoder, device, or FFmpeg support is absent.
        """
        strategy = context.options.hwaccel
        if strategy is None:
            return None
        capabilities = context.capabilities
        encoder = context.options.encoder
        if capabilities is None:
            raise RuntimeError("Hardware preparation requires FFmpeg capabilities")
        if not capabilities.supports_encoder(encoder):
            raise RuntimeError(
                f"FFmpeg '{capabilities.executable}' is missing required "
                f"capabilities: encoders: {encoder}"
            )

        device = await self.resolve_device(context)
        encoder_context = replace(
            context,
            hardware=HardwareRuntime(device, False),
        )
        # encoder probing fails early before source-specific decode decisions
        await require_hardware_encoder(
            capabilities.executable,
            strategy,
            encoder,
            device,
            self.encoder_probe_args(encoder_context),
        )

        hwaccel = context.encoder_config.hwaccel
        if (
            hwaccel is None
            or not context.supports_hwaccel(hwaccel)
            or not self.allows_decode(context)
        ):
            return HardwareRuntime(device, False)

        media_path = context.media_path
        if media_path is None:
            raise RuntimeError("Hardware decode probe requires a media path")
        stream_index = context.metadata.video_stream_index
        assert stream_index is not None
        probe_context = replace(
            context,
            hardware=HardwareRuntime(device, True),
        )
        input_args = await self.input_args(probe_context)
        output_format = context.encoder_config.hwaccel_output_format
        if output_format and "-hwaccel_output_format" not in input_args:
            input_args.extend(["-hwaccel_output_format", output_format])
        decode_format = "p010le" if context.metadata.bit_depth == 10 else "nv12"
        decode_args = [
            *input_args,
            "-noautorotate",
            "-i",
            media_path,
            "-map",
            f"0:{stream_index}",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"hwdownload,format={decode_format}",
            "-f",
            "null",
            "-",
        ]
        can_decode = await probe_hardware_decode(
            capabilities.executable,
            strategy,
            device,
            media_path,
            stream_index,
            decode_args,
        )
        if not can_decode:
            logger.warning(
                "Hardware decode probe failed for %s codec=%s profile=%s "
                "bit_depth=%s; using software decode",
                strategy,
                context.metadata.codec,
                context.metadata.profile,
                context.metadata.bit_depth,
            )
        runtime = HardwareRuntime(device, can_decode)
        if not can_decode:
            return runtime

        transform_context = replace(context, hardware=runtime)
        transform_filters = self.transform_filters(transform_context)
        if not transform_filters:
            return runtime
        required_filters = self.transform_filter_names(transform_context)
        # missing optional hardware filters fall back to the strategy's CPU path
        if any(not context.supports_filter(name) for name in required_filters):
            return HardwareRuntime(
                device,
                self.keep_decode_on_fallback(context),
            )

        filter_runtime = HardwareRuntime(device, True, True)
        filter_context = replace(context, hardware=filter_runtime)
        input_args = await self.input_args(filter_context)
        signature = ",".join(transform_filters)
        transform_format = self.transform_download_format(filter_context)
        transform_args = [
            *input_args,
            "-noautorotate",
            "-i",
            media_path,
            "-map",
            f"0:{stream_index}",
            "-frames:v",
            "1",
            "-an",
            "-sn",
            "-dn",
            "-vf",
            f"{signature},hwdownload,format={transform_format}",
            "-f",
            "null",
            "-",
        ]
        can_filter = await probe_hardware_transform(
            capabilities.executable,
            strategy,
            device,
            media_path,
            stream_index,
            signature,
            transform_args,
        )
        if not can_filter:
            logger.warning(
                "Hardware transform probe failed for %s rotation=%s "
                "field_order=%s; using software filters",
                strategy,
                context.rotation,
                context.metadata.field_order,
            )
        return HardwareRuntime(
            device,
            can_filter or self.keep_decode_on_fallback(context),
            can_filter,
        )

    def keep_hardware_frames(self, context: TranscodeContext) -> bool:
        """Return whether decoding should keep frames in device memory.

        Args:
            context: The runtime transcode context.

        Returns:
            Whether to keep hardware frames.
        """
        if context.uses_hw_filters:
            return True
        return not (
            context.needs_scale or context.needs_rotation or context.is_interlaced
        )

    async def input_args(self, context: TranscodeContext) -> list[str]:
        """Build FFmpeg input options for hardware-accelerated decoding.

        The returned arguments are inserted before the input file. Hardware
        frames remain on-device when the prepared strategy can perform all
        required transforms; otherwise decoded frames remain accessible to
        software filters. This method is asynchronous to allow implementations
        to discover or initialize a hardware device before building the command.

        Args:
            context: The runtime transcode context.

        Returns:
            FFmpeg command-line arguments to place before the input file.
        """
        cmd: list[str] = []
        config = context.encoder_config
        if config.hwaccel and context.uses_hw_decode:
            cmd.extend(["-hwaccel", config.hwaccel])
            if config.hwaccel_output_format and self.keep_hardware_frames(context):
                cmd.extend(["-hwaccel_output_format", config.hwaccel_output_format])
        return cmd

    def video_filters(self, context: TranscodeContext) -> list[str]:
        """Build strategy-specific FFmpeg video filter expressions.

        The returned expressions form the complete ordered value passed to
        FFmpeg's `-vf` option. The default strategy requires no filters.

        Args:
            context: The runtime transcode context.

        Returns:
            Video filter expressions in the order FFmpeg should apply them.
        """
        return []

    @abstractmethod
    def encoder_args(self, context: TranscodeContext) -> list[str]:
        """Build FFmpeg options for the strategy's video encoder.

        The returned arguments are inserted immediately after the video codec
        selection. Implementations use the requested quality and other
        transcode settings to configure encoder-specific rate control and
        output parameters.

        Args:
            context: The runtime transcode context.

        Returns:
            FFmpeg command-line arguments for configuring the video encoder.

        Raises:
            NotImplementedError: If a concrete strategy does not implement the method.
        """
        raise NotImplementedError

    def keyframe_args(self, context: TranscodeContext) -> list[str]:
        """Build FFmpeg options that place keyframes near HLS segment boundaries.

        The default strategy combines timestamp-based forced keyframes with a
        fixed GOP size. For constant-frame-rate input, the GOP length
        approximates one segment duration after rounding up to a whole frame.

        Args:
            context: The runtime transcode context.

        Returns:
            FFmpeg command-line arguments for keyframe and GOP configuration.
        """
        args = segment_keyframe_args(context)
        gop = context.fixed_gop_size
        if gop is not None:
            args.extend(
                [
                    "-g:v:0",
                    str(gop),
                    "-keyint_min:v:0",
                    str(gop),
                ]
            )
        return args
