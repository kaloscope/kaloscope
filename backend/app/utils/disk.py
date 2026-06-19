import shutil
from dataclasses import dataclass
from pathlib import Path

from send2trash import send2trash

from app.core.config import KaloscopeConfig


@dataclass
class DiskUsage:
    """The disk usage statistics."""

    total: int | None = None
    used: int | None = None
    free: int | None = None

    def _format(self, size: int | None) -> str:
        """Format the disk usage size.

        Args:
            size: The size in bytes.

        Returns:
            The formatted size.
        """
        return format_bytes(size) if size is not None else ""

    def total_space(self) -> str:
        return self._format(self.total)

    def used_space(self) -> str:
        return self._format(self.used)

    def free_space(self) -> str:
        return self._format(self.free)


def disk_usage(path: Path | str) -> DiskUsage:
    """Get the disk usage of a directory.

    Args:
        path: The path to the directory.

    Returns:
        The disk usage statistics object.
    """
    if not is_directory(path):
        return DiskUsage()

    try:
        total, used, free = shutil.disk_usage(path)
        return DiskUsage(total, used, free)
    except OSError:
        return DiskUsage()


def is_directory(path: Path | str) -> bool:
    """Check if a path is a directory.

    Args:
        path: The path to check.

    Returns:
        True if the path is a directory, False otherwise.
    """
    try:
        if not isinstance(path, Path):
            path = Path(path)
        return path.is_dir()
    except OSError:
        return False


def delete_path(path: Path | str):
    """Delete a filesystem path according to filesystem trash mode.

    When filesystem trash mode is enabled, the path is moved to the OS trash.
    When disabled, files and symlinks are unlinked and directories are removed.

    Args:
        path: The file or directory path to delete.
    """
    if not isinstance(path, Path):
        path = Path(path)
    if not path.exists() and not path.is_symlink():
        return

    if KaloscopeConfig.get().filesystem_trash_mode:
        send2trash(path)
    elif path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def format_bytes(size_bytes: int) -> str:
    """Format the size in bytes to a human-readable format.

    Args:
        size_bytes: The size in bytes.

    Returns:
        The human-readable size.
    """
    if size_bytes == 0:
        return "0 B"
    i = 0
    size = float(size_bytes)
    sizes = ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]
    while size > 1024:
        size /= 1024
        i += 1
    return f"{size:.1f}".rstrip("0").rstrip(".") + " " + sizes[i]
