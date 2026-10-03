"""Read bounded ZIP content and validate archive member boundaries."""

import io
import os
import stat
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.core.media.common import ContentError, FileSnapshot, file_state

_MAX_MEMBERS = 20_000
_MAX_EXPANDED_BYTES = 4 * 1024**3
_DIRECTORY_BYTES = 8 * 1024**2
_READ_BYTES = 64 * 1024


class _ArchiveReader(io.BufferedReader):
    """Bound source reads before zipfile allocates its central directory."""

    def read(self, size: int = -1) -> bytes:
        """Reject unbounded or oversized reads requested by the ZIP parser.

        Args:
            size: The byte bound; the unbounded default is rejected.

        Returns:
            At most the requested number of source bytes.

        Raises:
            ContentError: If the read exceeds the allocation limit or source I/O fails.
        """
        if not 0 <= size <= _DIRECTORY_BYTES:
            raise ContentError("media_limit_exceeded")
        try:
            return super().read(size)
        except OSError as error:
            # zipfile otherwise turns some source I/O errors into BadZipFile
            raise ContentError("media_source_unavailable") from error


def normalize_member_path(name: str) -> str:
    """Normalize a relative ZIP member name without allowing parent traversal.

    Args:
        name: The original or decoded member name, including directory entries.

    Returns:
        A normalized POSIX path used for uniqueness and sorting.

    Raises:
        ValueError: If the name is absolute, unsafe, empty or excessively long.
    """
    if (
        not name
        or len(name) > 4096
        or "\0" in name
        or "\\" in name
        or name.startswith("/")
        or PureWindowsPath(name).drive
        or ".." in name.split("/")
    ):
        raise ValueError("invalid archive member path")
    normalized = unicodedata.normalize("NFC", str(PurePosixPath(name)))
    if normalized == ".":
        raise ValueError("invalid archive member path")
    return normalized


def _validate_members(archive: zipfile.ZipFile):
    """Validate member identities, supported methods and declared resource budgets.

    Args:
        archive: The already opened ZIP archive with bounded directory storage.

    Raises:
        ContentError: If the archive contains unsafe, unsupported or excessive entries.
    """
    members = archive.infolist()
    if len(members) > _MAX_MEMBERS:
        raise ContentError("media_limit_exceeded")
    names, expanded = set(), 0
    for info in members:
        try:
            normalize_member_path(info.orig_filename)
            name = normalize_member_path(info.filename)
        except ValueError as error:
            raise ContentError("invalid_archive") from error
        mode = stat.S_IFMT(info.external_attr >> 16)
        if (
            name in names
            or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
            or (mode == stat.S_IFDIR and not info.is_dir())
            or (mode == stat.S_IFREG and info.is_dir())
            or info.volume != 0
        ):
            raise ContentError("invalid_archive")
        names.add(name)
        if info.flag_bits & (1 | 32 | 64) or info.compress_type not in (
            zipfile.ZIP_STORED,
            zipfile.ZIP_DEFLATED,
        ):
            raise ContentError("unsupported_media_format")
        expanded += info.file_size
        if expanded > _MAX_EXPANDED_BYTES:
            raise ContentError("media_limit_exceeded")


@contextmanager
def open_archive(
    path: Path, snapshot: FileSnapshot | None = None
) -> Iterator[tuple[zipfile.ZipFile, FileSnapshot]]:
    """Open a stable ZIP source with bounded metadata and no extraction or writes.

    The caller validates the full library boundary and runs this synchronous work
    in a worker. Source stability is checked even when member parsing fails.

    Args:
        path: The source archive inside a caller-validated library container.
        snapshot: The expected size and mtime, or None during initial indexing.

    Yields:
        The validated archive and its path-independent source snapshot.

    Raises:
        ContentError: If the source changes, becomes unreadable, is unsafe,
            unsupported or malformed, or exceeds the archive limits.
        OSError: If the source cannot be inspected or opened.
    """
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or path.parent.is_symlink():
        raise ContentError("media_source_unavailable")
    current = FileSnapshot(size=before.st_size, mtime_ns=before.st_mtime_ns)
    if snapshot is not None and current != snapshot:
        raise ContentError("content_changed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with _ArchiveReader(io.FileIO(os.open(path, flags), "rb")) as file:
        if file_state(os.fstat(file.fileno())) != file_state(before):
            raise ContentError("content_changed")
        try:
            with zipfile.ZipFile(file, "r") as archive:
                _validate_members(archive)
                yield archive, current
        except (zipfile.BadZipFile, EOFError, UnicodeError, zlib.error) as error:
            raise ContentError("invalid_archive") from error
        except NotImplementedError as error:
            raise ContentError("unsupported_media_format") from error
        finally:
            try:
                after = path.stat(follow_symlinks=False)
            except FileNotFoundError as error:
                raise ContentError("content_changed") from error
            if file_state(after) != file_state(before) or file_state(
                os.fstat(file.fileno())
            ) != file_state(before):
                raise ContentError("content_changed")
            if path.parent.is_symlink():
                raise ContentError("media_source_unavailable")


def read_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    limit: int,
    *,
    prefix_bytes: int | None = None,
) -> bytes:
    """Read one member within a byte budget, consuming it fully for CRC validation.

    Args:
        archive: The archive yielded by open_archive.
        member: The validated member to read.
        limit: The maximum allowed uncompressed byte count.
        prefix_bytes: The number of leading bytes to retain; None retains all bytes.
            The whole member is consumed and checked in either case.

    Returns:
        The full bytes or the requested prefix, after size and CRC checks.

    Raises:
        ContentError: If the member exceeds the limit or has a mismatched size.
        ValueError: If the requested prefix length is negative.
        zipfile.BadZipFile: If member structure or CRC fails; open_archive maps it
            to a stable content error.
        OSError: If the source cannot be read.
    """
    if prefix_bytes is not None and prefix_bytes < 0:
        raise ValueError("prefix length must be nonnegative")
    if member.file_size > limit:
        raise ContentError("media_limit_exceeded")
    data = bytearray()
    total = 0
    with archive.open(member) as file:
        while chunk := file.read(min(_READ_BYTES, limit - total + 1)):
            total += len(chunk)
            if total > limit:
                raise ContentError("media_limit_exceeded")
            data.extend(
                chunk[: max(0, prefix_bytes - len(data))]
                if prefix_bytes is not None
                else chunk
            )
    if total != member.file_size:
        raise ContentError("invalid_archive")
    return bytes(data)
