import mimetypes
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from fnmatch import fnmatch
from pathlib import Path

from lxml import etree
from sanic.log import Colors, logger
from watchdog.events import (
    EVENT_TYPE_CREATED,
    EVENT_TYPE_DELETED,
    EVENT_TYPE_MODIFIED,
    EVENT_TYPE_MOVED,
    FileSystemEvent,
)

from app.core.constants import ENCODING, NFO_MIME_TYPE
from app.models.media import Language, LibType, MediaLib, NFOType


@dataclass(kw_only=True)
class MediaPathInfo:
    """The information parsed from the media item path."""

    path: Path = field(kw_only=False)
    nfo_path: Path | None = None
    nfo_type: NFOType | None = None
    nfo_source: str | None = None
    language: Language | None = None
    title: str = field(init=False)
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    series_id: str | None = None

    @property
    def item_dir(self) -> str:
        return str(self.path if self.path.is_dir() else self.path.parent)

    @property
    def item_path(self) -> str:
        return str(self.path)

    @property
    def item_name(self) -> str:
        return self.path.name if self.path.is_dir() else self.path.stem


@dataclass(kw_only=True)
class Actor:
    """The metadata of an actor parsed from an NFO file."""

    name: str | None = None
    role: str | None = None
    thumb: str | None = None


@dataclass(kw_only=True)
class MediaMeta:
    """The metadata of a media item parsed from an NFO file."""

    nfo_path: str = field(init=False)
    nfo_source: str | None = None
    unique_id: str | None = None
    title: str | None = None
    originaltitle: str | None = None
    tagline: str | None = None
    plot: str | None = None
    rating: Decimal | None = None
    year: int | None = None
    aired: str | None = None
    season: int | None = None
    episode: int | None = None
    premiered: str | None = None
    country: str | None = None
    mpaa: str | None = None
    tags: list[str] | None = None
    genres: list[str] | None = None
    studios: list[str] | None = None
    directors: list[str] | None = None
    writers: list[str] | None = None
    credits: list[str] | None = None
    actors: list[Actor] | None = None
    poster: str | None = None
    backdrop: str | None = None


class MediaHandler(ABC):
    """The base class for media item handlers."""

    @abstractmethod
    def accept(self) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def hierarchies(self) -> list[int]:
        raise NotImplementedError

    @abstractmethod
    def extract_meta(self, data: etree._ElementTree) -> MediaMeta:
        raise NotImplementedError

    @abstractmethod
    async def gen_items(self, lib: MediaLib, path: Path) -> list[MediaPathInfo]:
        raise NotImplementedError

    def filter_event(
        self, event: FileSystemEvent, *, base_path: str
    ) -> FileSystemEvent | None:
        """Filter the event based on the library type and hierarchy.

        Args:
            event: The file system event to filter.
            base_path: The base path of the media library.

        Returns:
            The filtered event or None if the event is not accepted.
        """
        logger.debug(f"Filtering: {Colors.CYAN}%s{Colors.END}", event)

        result: FileSystemEvent | None = None
        if event.event_type == EVENT_TYPE_MODIFIED:
            result = self._filter_modified(event, base_path=base_path)
        elif event.event_type == EVENT_TYPE_DELETED:
            result = self._filter_deleted(event, base_path=base_path)
        elif event.event_type == EVENT_TYPE_MOVED:
            result = self._filter_moved(event, base_path=base_path)
        elif event.event_type == EVENT_TYPE_CREATED:
            result = self._filter_created(event, base_path=base_path)

        if result is not None:
            logger.debug(f"Accepted: {Colors.GREEN}%s{Colors.END}", result)
        return result

    def is_target(
        self,
        base_path: str,
        path: bytes | str,
        *,
        check_dir: bool = False,
        check_exists: bool = True,
    ) -> bool:
        """Check if the path is a target for the media item handler.

        Args:
            base_path: The base path.
            path: The path to check for.
            check_dir: Whether to check directories.
            check_exists: Whether to check if the path exists.

        Returns:
            True if the path is a target, False otherwise.
        """
        _path = Path(self._decode_path(path))

        # check if the path exists
        if check_exists and not _path.exists():
            logger.debug(f"Path does not exist: {Colors.RED}%s{Colors.END}", _path)
            return False

        if not check_dir:
            # check if the path is a file
            if check_exists and not _path.is_file():
                logger.debug(f"Path is not a file: {Colors.RED}%s{Colors.END}", _path)
                return False

            # check if the mime type is accepted
            mime_type, _ = mimetypes.guess_file_type(_path)
            if mime_type is None:
                logger.debug(
                    f"Mime type is None for path: {Colors.RED}%s{Colors.END}", _path
                )
                return False
            accept = [*self.accept(), NFO_MIME_TYPE]
            if not any(fnmatch(mime_type, pat) for pat in accept):
                logger.debug(
                    f"Mime type not accepted: {Colors.RED}%s{Colors.END}", mime_type
                )
                return False

        # check if the path is in the supported hierarchies
        relative_path = _path.relative_to(Path(base_path))
        hierarchy = len(relative_path.parts) + (1 if check_dir else 0)
        if hierarchy not in self.hierarchies():
            logger.debug(
                f"Hierarchy not supported: {Colors.RED}%d{Colors.END}", hierarchy
            )
            return False

        return True

    def _decode_path(self, path: bytes | str) -> str:
        """Convert the path to a string.

        Args:
            path: The path to convert.

        Returns:
            The converted path as a string.
        """
        if isinstance(path, bytes | bytearray):
            path = path.decode(ENCODING)
        elif isinstance(path, memoryview):
            path = path.tobytes().decode(ENCODING)
        return path

    def _filter_modified(
        self, event: FileSystemEvent, *, base_path: str
    ) -> FileSystemEvent | None:
        """Filter the modification event.

        Args:
            event: The file system event to filter.
            base_path: The base path of the media library.

        Returns:
            The filtered event or None if the event is not accepted.
        """
        if event.is_directory:
            return None
        if self.is_target(base_path, event.src_path, check_exists=True):
            return event
        # check if the path is a target but does not exist
        if not Path(self._decode_path(event.src_path)).exists() and self.is_target(
            base_path, event.src_path, check_exists=False
        ):
            event.event_type = EVENT_TYPE_DELETED
            event.dest_path = ""
            return event
        return None

    def _filter_deleted(
        self, event: FileSystemEvent, *, base_path: str
    ) -> FileSystemEvent | None:
        """Filter the deletion event.

        Args:
            event: The file system event to filter.
            base_path: The base path of the media library.

        Returns:
            The filtered event or None if the event is not accepted.
        """
        if self.is_target(
            base_path, event.src_path, check_dir=event.is_directory, check_exists=False
        ):
            return event
        return None

    def _filter_moved(
        self, event: FileSystemEvent, *, base_path: str
    ) -> FileSystemEvent | None:
        """Filter the movement event.

        Args:
            event: The file system event to filter.
            base_path: The base path of the media library.

        Returns:
            The filtered event or None if the event is not accepted.
        """
        if event.is_directory:
            return None

        src = self.is_target(base_path, event.src_path, check_exists=False)
        dest = self.is_target(base_path, event.dest_path)

        if not src and not dest:
            return None
        elif src and dest:
            return event
        elif src:
            # src is a target, dest is not
            # convert the event to a delete event
            event.event_type = EVENT_TYPE_DELETED
            event.dest_path = ""
        else:
            # dest is a target, src is not
            # convert the event to a create event
            event.event_type = EVENT_TYPE_CREATED
            event.src_path = event.dest_path
            event.dest_path = ""

        return event

    def _filter_created(
        self, event: FileSystemEvent, *, base_path: str
    ) -> FileSystemEvent | None:
        """Filter the creation event.

        Args:
            event: The file system event to filter.
            base_path: The base path of the media library.

        Returns:
            The filtered event or None if the event is not accepted.
        """
        if event.is_directory:
            return None
        if self.is_target(base_path, event.src_path):
            return event
        return None


_HANDLERS: dict[LibType, MediaHandler] = {}


def get_handler(lib_type: LibType) -> MediaHandler:
    """Get the registered handler for the given type.

    Args:
        lib_type: The media library type.

    Raises:
        ValueError: If the type is not supported.

    Returns:
        The registered handler for the type.
    """
    handler = _HANDLERS.get(lib_type)
    if handler is None:
        raise ValueError(f"unsupported media library type: {lib_type}")
    return handler
