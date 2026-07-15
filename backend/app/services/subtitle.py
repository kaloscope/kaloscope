import asyncio
import hashlib
import json
from enum import StrEnum, auto
from pathlib import Path
from urllib.parse import quote

import aiofiles
from charset_normalizer import from_bytes
from langcodes import standardize_tag, tag_is_valid
from langcodes.tag_parser import LanguageTagError
from pydantic import BaseModel
from sanic.log import logger

from app.core.constants import ENCODING, URL_PREFIX
from app.models.general import GlobalConfig
from app.models.media import MediaItem
from app.utils.subtitle import ass_to_vtt, srt_to_vtt


class SubtitleType(StrEnum):
    """The type of a subtitle track."""

    EXTERNAL = auto()
    EMBEDDED = auto()


class Subtitle(BaseModel):
    """A subtitle track that can be passed to the frontend video player."""

    id: str
    type: SubtitleType
    label: str
    url: str | None = None
    format: str | None = None
    language: str | None = None


class SubtitleService:
    """The service class for local subtitle discovery and loading."""

    # xgplayer-subtitles expects WebVTT, so other formats are converted on load
    SUPPORTED_EMBEDDED_CODECS = {"ass", "ssa", "subrip", "webvtt", "text", "mov_text"}
    SUPPORTED_EXTERNAL_FORMATS = {"vtt", "ass", "ssa", "srt"}
    EXTERNAL_CONVERTERS = {"ass": ass_to_vtt, "ssa": ass_to_vtt, "srt": srt_to_vtt}
    WEBVTT_CONTENT_TYPE = "text/vtt; charset=utf-8"
    PLAYER_SUBTITLE_FORMAT = "vtt"
    LEGACY_SUBTITLE_LANGUAGE_ALIASES = {
        "chs": "zh-Hans",
        "cht": "zh-Hant",
        "cn": "zh-CN",
        "jp": "ja",
    }

    @classmethod
    async def list_tracks(cls, path: str) -> list[Subtitle]:
        """List local subtitles for a media resource path.

        Args:
            path: The media resource path.

        Returns:
            A list of subtitle tracks found next to the media file.
        """
        media = await MediaItem.filter(path=path).first()
        if not media:
            logger.debug("No media item found for subtitle track listing: %s", path)
            return []

        video_path = Path(media.path)
        subtitles = [
            *cls.discover_external_tracks(video_path),
            *await cls.discover_embedded_tracks(video_path),
        ]
        logger.debug(
            "Discovered %d subtitle track(s) for %s.",
            len(subtitles),
            media.path,
        )
        return subtitles

    @classmethod
    async def load_content(
        cls, subtitle_path: str, stream_index: int | None = None
    ) -> tuple[str, str] | None:
        """Load a supported local subtitle resource for the player.

        Args:
            subtitle_path: The external subtitle file path or source video file path.
            stream_index: The embedded subtitle stream index.

        Returns:
            The subtitle content and content type, or `None` if the resource is
            unsupported.
        """
        path = Path(subtitle_path)
        if stream_index is not None:
            return await cls.load_embedded_content(path, stream_index)
        return await cls.load_external_content(path)

    @classmethod
    def discover_external_tracks(cls, video_path: Path | str) -> list[Subtitle]:
        """Discover supported external subtitles next to a local video file.

        Args:
            video_path: The video file path.

        Returns:
            Subtitle tracks whose file names share the video file stem.
        """
        video = Path(video_path)
        if not video.is_file():
            return []

        stem = video.stem
        prefix = f"{stem}."
        subtitles: list[Subtitle] = []
        for path in sorted(video.parent.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_file():
                continue
            if path.stem != stem and not path.stem.startswith(prefix):
                continue
            if subtitle := cls.resolve_external_track(path, video):
                subtitles.append(subtitle)
        return subtitles

    @classmethod
    def resolve_external_track(
        cls, subtitle_path: Path | str, video_path: Path | str | None = None
    ) -> Subtitle | None:
        """Resolve a supported external subtitle file into a player track.

        Args:
            subtitle_path: The external subtitle file path.
            video_path: The related video file path.

        Returns:
            The resolved subtitle track, or `None` if the file is unsupported.
        """
        path = Path(subtitle_path)
        subtitle_format = cls._external_subtitle_format(path)
        if not subtitle_format:
            return None

        video = Path(video_path) if video_path else None
        label = cls._external_track_label(path, video)
        return Subtitle(
            id=cls._external_track_id(path),
            type=SubtitleType.EXTERNAL,
            label=label,
            url=f"{URL_PREFIX}/subtitle/content?path={quote(str(path), safe='')}",
            format=cls.PLAYER_SUBTITLE_FORMAT,
            language=cls._external_track_language(label),
        )

    @classmethod
    async def load_external_content(
        cls, subtitle_path: Path | str
    ) -> tuple[str, str] | None:
        """Load a supported external subtitle file for the player.

        Args:
            subtitle_path: The external subtitle file path.

        Returns:
            The subtitle content and content type, or `None` if the file is unsupported.
        """
        path = Path(subtitle_path)
        subtitle_format = cls._external_subtitle_format(path)
        if not subtitle_format:
            logger.debug("Unsupported or missing subtitle file: %s", path)
            return None

        logger.debug("Loading external subtitle file: %s", path)
        async with aiofiles.open(path, "rb") as f:
            raw = await f.read()
        matches = from_bytes(raw).best()
        content = str(matches) if matches else raw.decode(ENCODING)

        if converter := cls.EXTERNAL_CONVERTERS.get(subtitle_format):
            content = converter(content)
        return content, cls.WEBVTT_CONTENT_TYPE

    @classmethod
    def _external_subtitle_format(cls, path: Path) -> str | None:
        """Get the supported external subtitle format for a file path.

        Args:
            path: The subtitle file path.

        Returns:
            The subtitle format, or `None` if the file is unsupported.
        """
        subtitle_format = path.suffix.lstrip(".").lower()
        if path.is_file() and subtitle_format in cls.SUPPORTED_EXTERNAL_FORMATS:
            return subtitle_format
        return None

    @classmethod
    def _external_track_id(cls, path: Path) -> str:
        """Create a stable short ID for an external subtitle file.

        Args:
            path: The subtitle file path.

        Returns:
            The stable external subtitle track ID.
        """
        digest = hashlib.md5(str(path).encode(ENCODING)).hexdigest()
        return f"external-{digest[:12]}"

    @classmethod
    def _external_track_label(cls, subtitle_path: Path, video_path: Path | None) -> str:
        """Get a readable label for an external subtitle file.

        Args:
            subtitle_path: The subtitle file path.
            video_path: The related video file path.

        Returns:
            The readable external subtitle label.
        """
        if not video_path:
            return subtitle_path.stem
        return subtitle_path.stem.removeprefix(f"{video_path.stem}.")

    @classmethod
    def _external_track_language(cls, label: str) -> str | None:
        """Infer a language marker from an external subtitle label.

        Args:
            label: The readable subtitle label.

        Returns:
            The inferred language marker, or `None` if it cannot be inferred.
        """
        normalized = label.strip().replace("_", "-")
        if not normalized:
            return None

        candidate = cls.LEGACY_SUBTITLE_LANGUAGE_ALIASES.get(
            normalized.lower(), normalized
        )
        try:
            language = standardize_tag(candidate)
        except LanguageTagError:
            return None
        return language if tag_is_valid(language) else None

    @classmethod
    async def discover_embedded_tracks(cls, video_path: Path | str) -> list[Subtitle]:
        """Discover supported embedded subtitle streams in a local video file.

        Args:
            video_path: The video file path.

        Returns:
            Subtitle tracks for supported embedded text subtitle streams.
        """
        video = Path(video_path)
        if not video.is_file():
            return []

        subtitles: list[Subtitle] = []
        for stream in await cls._probe_embedded_subtitle_streams(video):
            if subtitle := cls.resolve_embedded_track(
                video,
                stream,
                len(subtitles) + 1,
            ):
                subtitles.append(subtitle)
        return subtitles

    @classmethod
    def resolve_embedded_track(
        cls, video_path: Path | str, stream: dict, ordinal: int
    ) -> Subtitle | None:
        """Resolve a supported embedded subtitle stream into a player track.

        Args:
            video_path: The source video file path.
            stream: The ffprobe subtitle stream metadata.
            ordinal: The one-based supported subtitle stream ordinal.

        Returns:
            The resolved subtitle track, or `None` if the stream is unsupported.
        """
        codec = str(stream.get("codec_name") or "").lower()
        if stream.get("codec_type") != "subtitle" or (
            codec not in cls.SUPPORTED_EMBEDDED_CODECS
        ):
            return None

        try:
            stream_index = int(stream["index"])
        except (KeyError, TypeError, ValueError):
            return None

        video = Path(video_path)
        raw_tags = stream.get("tags")
        tags: dict = raw_tags if isinstance(raw_tags, dict) else {}
        label = cls._embedded_track_label(tags, ordinal)
        return Subtitle(
            id=cls._embedded_track_id(video, stream_index),
            type=SubtitleType.EMBEDDED,
            label=label,
            url=(
                f"{URL_PREFIX}/subtitle/content?"
                f"path={quote(str(video), safe='')}&stream={stream_index}"
            ),
            format=cls.PLAYER_SUBTITLE_FORMAT,
            language=cls._embedded_track_language(tags),
        )

    @classmethod
    async def load_embedded_content(
        cls, video_path: Path | str, stream_index: int
    ) -> tuple[str, str] | None:
        """Load an embedded subtitle stream as WebVTT content.

        Args:
            video_path: The source video file path.
            stream_index: The ffmpeg stream index to extract.

        Returns:
            The WebVTT content and content type, or `None` if extraction failed.
        """
        path = Path(video_path)
        if not path.is_file():
            logger.debug("Missing video file for embedded subtitle: %s", path)
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                await cls._ffmpeg_path(),
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                f"0:{stream_index}",
                "-f",
                "webvtt",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.debug("Failed to start ffmpeg for subtitle extraction: %s", exc)
            return None

        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.debug(
                "Failed to extract embedded subtitle stream %s from %s: %s",
                stream_index,
                path,
                stderr.decode(ENCODING, errors="replace").strip(),
            )
            return None
        content = stdout.decode(ENCODING, errors="replace")
        return content if content.strip() else "WEBVTT\n\n", cls.WEBVTT_CONTENT_TYPE

    @classmethod
    async def _probe_embedded_subtitle_streams(cls, video_path: Path) -> list[dict]:
        """Probe embedded subtitle streams with ffprobe.

        Args:
            video_path: The video file path.

        Returns:
            Raw ffprobe stream metadata for subtitle streams.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                await cls._ffprobe_path(),
                "-v",
                "quiet",
                "-select_streams",
                "s",
                "-show_streams",
                "-print_format",
                "json",
                str(video_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            logger.debug("Failed to start ffprobe for subtitle probing: %s", exc)
            return []

        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(stdout.decode(ENCODING))
        except json.JSONDecodeError:
            return []
        streams = data.get("streams", [])
        return [stream for stream in streams if isinstance(stream, dict)]

    @classmethod
    async def _ffmpeg_path(cls) -> str:
        """Get the ffmpeg executable path from global config or the default name.

        Returns:
            The ffmpeg executable name or path.
        """
        path = await GlobalConfig.get_or_none(key="ffmpeg.path")
        if path and isinstance(path.value, str) and Path(path.value).is_file():
            return path.value
        return "ffmpeg"

    @classmethod
    async def _ffprobe_path(cls) -> str:
        """Get the ffprobe executable path from global config or the default name.

        Returns:
            The ffprobe executable name or path.
        """
        ffmpeg = await cls._ffmpeg_path()
        if ffmpeg != "ffmpeg":
            path = Path(ffmpeg).with_name("ffprobe")
            if path.is_file():
                return str(path)
        return "ffprobe"

    @classmethod
    def _embedded_track_id(cls, video_path: Path, stream_index: int) -> str:
        """Create a stable short ID for an embedded subtitle stream.

        Args:
            video_path: The source video file path.
            stream_index: The subtitle stream index.

        Returns:
            The stable embedded subtitle track ID.
        """
        digest = hashlib.md5(str(video_path).encode(ENCODING)).hexdigest()
        return f"embedded-{digest[:12]}-{stream_index}"

    @classmethod
    def _embedded_track_label(cls, tags: dict, ordinal: int) -> str:
        """Get a readable label for an embedded subtitle stream.

        Args:
            tags: The ffprobe stream tags.
            ordinal: The one-based supported subtitle stream ordinal.

        Returns:
            The readable embedded subtitle label.
        """
        title = tags.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        language = cls._embedded_track_language(tags)
        return language or f"Track {ordinal}"

    @classmethod
    def _embedded_track_language(cls, tags: dict) -> str | None:
        """Get a language marker from ffprobe stream tags.

        Args:
            tags: The ffprobe stream tags.

        Returns:
            The language marker, or `None` if it cannot be inferred.
        """
        language = tags.get("language")
        if not isinstance(language, str) or not language.strip():
            return None

        return language.strip()
