import hashlib
from enum import StrEnum, auto
from pathlib import Path
from urllib.parse import quote

import aiofiles
from pydantic import BaseModel
from sanic.log import logger

from app.core.constants import ENCODING, URL_PREFIX
from app.models.media import MediaItem


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

    # xgplayer-subtitles@3.0.24 parses VTT and ASS; SSA uses the ASS syntax.
    SUPPORTED_EXTERNAL_FORMATS = {
        "vtt": "text/vtt; charset=utf-8",
        "ass": "text/plain; charset=utf-8",
        "ssa": "text/plain; charset=utf-8",
    }
    LANGUAGE_ALIASES = {"chs", "cht", "cn", "en", "eng", "ja", "jp", "jpn", "zh"}

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

        subtitles = cls.discover_external(Path(media.path))
        logger.debug(
            "Discovered %d external subtitle track(s) for %s.",
            len(subtitles),
            media.path,
        )
        return subtitles

    @classmethod
    def discover_external(cls, video_path: Path | str) -> list[Subtitle]:
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
            if subtitle := cls.resolve_external(path, video):
                subtitles.append(subtitle)
        return subtitles

    @classmethod
    def resolve_external(
        cls, subtitle_path: Path | str, video_path: Path | str | None = None
    ) -> Subtitle | None:
        """Resolve a supported external subtitle file into a player track.

        Args:
            subtitle_path: The external subtitle file path.
            video_path: The related video file path.

        Returns:
            The resolved subtitle track, or None if the file is unsupported.
        """
        path = Path(subtitle_path)
        subtitle_format = cls._external_format(path)
        if not subtitle_format:
            return None

        video = Path(video_path) if video_path else None
        label = cls._subtitle_label(path, video)
        return Subtitle(
            id=cls._subtitle_id(path),
            type=SubtitleType.EXTERNAL,
            label=label,
            url=f"{URL_PREFIX}/subtitle/content?path={quote(str(path), safe='')}",
            format=subtitle_format,
            language=cls._language(label),
        )

    @classmethod
    async def load_external(cls, subtitle_path: str) -> tuple[str, str] | None:
        """Load a supported external subtitle file for the player.

        Args:
            subtitle_path: The external subtitle file path.

        Returns:
            The subtitle content and content type, or None if the file is unsupported.
        """
        path = Path(subtitle_path)
        subtitle_format = cls._external_format(path)
        if not subtitle_format:
            logger.debug("Unsupported or missing subtitle file: %s", path)
            return None

        logger.debug("Loading external subtitle file: %s", path)
        async with aiofiles.open(path, encoding=ENCODING) as f:
            return await f.read(), cls.SUPPORTED_EXTERNAL_FORMATS[subtitle_format]

    @classmethod
    def _external_format(cls, path: Path) -> str | None:
        """Get the supported external subtitle format for a file path.

        Args:
            path: The subtitle file path.

        Returns:
            The subtitle format, or None if the file is unsupported.
        """
        subtitle_format = path.suffix.lstrip(".").lower()
        if path.is_file() and subtitle_format in cls.SUPPORTED_EXTERNAL_FORMATS:
            return subtitle_format
        return None

    @classmethod
    def _subtitle_id(cls, path: Path) -> str:
        """Create a stable short ID for a subtitle file.

        Args:
            path: The subtitle file path.

        Returns:
            The stable subtitle track ID.
        """
        digest = hashlib.md5(str(path).encode(ENCODING)).hexdigest()
        return f"external-{digest[:12]}"

    @classmethod
    def _subtitle_label(cls, subtitle_path: Path, video_path: Path | None) -> str:
        """Get a readable subtitle label from the file name.

        Args:
            subtitle_path: The subtitle file path.
            video_path: The related video file path.

        Returns:
            The readable subtitle label.
        """
        if not video_path:
            return subtitle_path.stem
        return subtitle_path.stem.removeprefix(f"{video_path.stem}.")

    @classmethod
    def _language(cls, label: str) -> str | None:
        """Infer a language marker from a subtitle label when it is obvious.

        Args:
            label: The readable subtitle label.

        Returns:
            The inferred language marker, or None if it cannot be inferred.
        """
        normalized = label.strip()
        if normalized.lower() in cls.LANGUAGE_ALIASES:
            return normalized

        parts = normalized.split("-")
        if len(parts) == 2 and all(len(part) == 2 for part in parts):
            return normalized
        return None
