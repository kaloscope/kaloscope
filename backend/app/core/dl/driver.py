from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from app.models.download import DownloadTask

type TorrentFile = tuple[str | None, bytes, str | None]


class DownloadAction(StrEnum):
    """An operation that a driver can perform for a task."""

    PAUSE = auto()
    RESUME = auto()
    CANCEL = auto()
    RETRY = auto()
    DELETE = auto()


class DownloadSource(StrEnum):
    """A source format accepted by a downloader driver."""

    RAW = auto()
    MAGNET = auto()
    TORRENT = auto()


class DownloadState(StrEnum):
    """A persisted lifecycle state for a download task."""

    DOWNLOADING = auto()
    SUBMITTING = auto()
    SUBMIT_UNKNOWN = auto()
    REMOTE = auto()
    SETTLING = auto()
    PULLING = auto()
    VERIFYING = auto()
    PAUSED = auto()
    COMPLETED = auto()
    ERROR = auto()


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadRequest:
    """A normalized request accepted by every downloader driver."""

    directory: str
    remote_directory: str | None = None
    identity: DownloadIdentity | None = None
    link: str | None = None
    torrent: TorrentFile | None = None
    paused: bool = False
    transfer_library_id: int | None = None
    transfer_method: str | None = None
    sub_pattern: str | None = None
    sub_repl: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadIdentity:
    """Local and remote identifiers for one download task."""

    task_id: int | None = None
    remote_id: str | None = None
    info_hash: str | None = None
    info_hash_v2: str | None = None

    @classmethod
    def from_task(cls, task: DownloadTask) -> Self:
        """Create an identity from a `DownloadTask`."""
        return cls(
            task_id=task.id,
            remote_id=task.unique_id,
            info_hash=task.info_hash,
            info_hash_v2=task.info_hash_v2,
        )

    @property
    def match_keys(self) -> tuple[tuple[str, str], ...]:
        """Get the keys to match the identity against."""
        keys = []
        if self.info_hash_v2:
            keys.append(("info_hash_v2", self.info_hash_v2))
        if self.info_hash:
            keys.append(("info_hash", self.info_hash))
        if self.remote_id:
            keys.append(("remote_id", self.remote_id))
        return tuple(keys)

    @property
    def rpc_variables(self) -> dict[str, object]:
        """Convert the identity to RPC variables."""
        return {
            "id": self.remote_id,
            "hash": self.info_hash,
            "hash_v2": self.info_hash_v2,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class OfflineJobDraft:
    """A draft for persisting an offline download job."""

    job_uuid: str
    source_fingerprint: str
    remote_directory: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadDraft:
    """A driver-provided draft for creating a task before submission."""

    request: DownloadRequest
    name: str
    state: DownloadState
    magnet_link: str | None = None
    percentage: float | None = None
    job: OfflineJobDraft | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DownloadSnapshot:
    """A normalized task snapshot returned by a downloader driver."""

    identity: DownloadIdentity
    state: DownloadState | None = None
    error: str | None = None
    percentage: float | None = None
    files: tuple[str, ...] | None = None


class DownloaderDriver(Protocol):
    """The common interface implemented by every downloader driver."""

    @property
    def source_types(self) -> frozenset[DownloadSource]:
        """Get the source formats accepted by the driver.

        Returns:
            The supported source formats.
        """
        ...

    @property
    def supports_version(self) -> bool:
        """Get whether the driver can report an endpoint version.

        Returns:
            `True` when version reporting is supported, otherwise `False`.
        """
        ...

    async def validate(self) -> str | None:
        """Validate the configured downloader endpoint.

        Returns:
            The endpoint version when available, otherwise `None`.
        """
        ...

    async def version(self) -> str | None:
        """Get the downloader endpoint version.

        Returns:
            The endpoint version when available, otherwise `None`.
        """
        ...

    def prepare(self, request: DownloadRequest) -> DownloadDraft | None:
        """Prepare data that must be persisted before submission.

        Args:
            request: The normalized download request.

        Returns:
            The persistence draft, or `None` when the task can be created after
            submission.
        """
        ...

    async def add(self, request: DownloadRequest) -> DownloadSnapshot:
        """Submit a download request.

        Args:
            request: The normalized download request.

        Returns:
            The initial task snapshot returned by the driver.
        """
        ...

    async def sync(
        self, identities: Sequence[DownloadIdentity]
    ) -> tuple[DownloadSnapshot, ...]:
        """Synchronize task state with the downloader.

        Args:
            identities: The tasks to synchronize.

        Returns:
            The task snapshots produced during this synchronization cycle.
        """
        ...

    async def capabilities(
        self,
        identity: DownloadIdentity,
        state: DownloadState,
        *,
        retry_target: DownloadState | None = None,
    ) -> frozenset[DownloadAction]:
        """Get the actions available for a task in its current state.

        Args:
            identity: The task identity.
            state: The persisted task state.
            retry_target: The state that a retry would restore, if known.

        Returns:
            The actions currently supported for the task.
        """
        ...

    async def pause(self, identity: DownloadIdentity) -> DownloadState | None:
        """Pause a task.

        Args:
            identity: The task identity.

        Returns:
            The resulting state, or `None` when no state update is required.
        """
        ...

    async def resume(self, identity: DownloadIdentity) -> DownloadState | None:
        """Resume a paused task.

        Args:
            identity: The task identity.

        Returns:
            The resulting state, or `None` when no state update is required.
        """
        ...

    async def cancel(self, identity: DownloadIdentity):
        """Cancel a task without deleting its persisted record.

        Args:
            identity: The task identity.
        """
        ...

    async def retry(self, identity: DownloadIdentity) -> DownloadState | None:
        """Retry a failed task.

        Args:
            identity: The task identity.

        Returns:
            The restored state, or `None` when retry is unavailable.
        """
        ...

    async def delete(self, identity: DownloadIdentity, *, local: bool = False):
        """Delete a task and optionally its local files.

        Args:
            identity: The task identity.
            local: Whether to delete downloaded local files.
        """
        ...

    async def close(self):
        """Release resources owned by the driver."""
        ...
