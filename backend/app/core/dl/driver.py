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
    """Describe the persisted phase of a download task.

    `downloading`: An RPC downloader is downloading the content.
    `submitting`: An OpenList submission is in progress.
    `submit_unknown`: Submission may have succeeded, but its result is unconfirmed;
    do not treat this as a confirmed failure or submit a duplicate task.
    `remote`: The remote offline downloader is processing the task.
    `settling`: Remote files are being prepared or checked for a stable manifest.
    `pulling`: Remote files are being transferred to local storage.
    `verifying`: Local files are being checked against the remote manifest.
    `paused`: Task processing is paused.
    `completed`: Download processing has finished successfully.
    `error`: Task processing failed; inspect the error message and category.

    Progress may describe an intermediate phase. A value of 100 percent alone
    does not imply `completed`.
    """

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
        """Create an identity from a `DownloadTask`.

        Args:
            task: Persisted download task.

        Returns:
            Identity with local and remote task references.
        """
        return cls(
            task_id=task.id,
            remote_id=task.unique_id,
            info_hash=task.info_hash,
            info_hash_v2=task.info_hash_v2,
        )

    @property
    def match_keys(self) -> tuple[tuple[str, str], ...]:
        """Get keys to match the identity against."""
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
        """Get source formats accepted by the driver.

        Returns:
            Supported source formats.
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
            Endpoint version when available, otherwise `None`.
        """
        ...

    async def version(self) -> str | None:
        """Get downloader endpoint version.

        Returns:
            Endpoint version when available, otherwise `None`.
        """
        ...

    def prepare(self, request: DownloadRequest) -> DownloadDraft | None:
        """Prepare data that must be persisted before submission.

        Args:
            request: Normalized download request.

        Returns:
            Persistence draft, or `None` when the task can be created after
            submission.
        """
        ...

    async def add(self, request: DownloadRequest) -> DownloadSnapshot:
        """Submit a download request.

        Args:
            request: Normalized download request.

        Returns:
            Initial task snapshot returned by the driver.
        """
        ...

    async def sync(
        self, identities: Sequence[DownloadIdentity]
    ) -> tuple[DownloadSnapshot, ...]:
        """Synchronize task state with the downloader.

        Args:
            identities: Tasks to synchronize.

        Returns:
            Task snapshots produced during this synchronization cycle.
        """
        ...

    async def capabilities(
        self,
        identity: DownloadIdentity,
        state: DownloadState,
        *,
        retry_target: DownloadState | None = None,
    ) -> frozenset[DownloadAction]:
        """Get actions available for a task in its current state.

        Args:
            identity: Task identity.
            state: Persisted task state.
            retry_target: State that a retry would restore, if known. `None` means
                the retry destination is unknown.

        Returns:
            Actions currently supported for the task.
        """
        ...

    async def pause(self, identity: DownloadIdentity) -> DownloadState | None:
        """Pause a task.

        Args:
            identity: Task identity.

        Returns:
            Resulting state, or `None` when no state update is required.
        """
        ...

    async def resume(self, identity: DownloadIdentity) -> DownloadState | None:
        """Resume a paused task.

        Args:
            identity: Task identity.

        Returns:
            Resulting state, or `None` when no state update is required.
        """
        ...

    async def cancel(self, identity: DownloadIdentity):
        """Cancel a task without deleting its persisted record.

        Args:
            identity: Task identity.
        """
        ...

    async def retry(self, identity: DownloadIdentity) -> DownloadState | None:
        """Retry a failed task.

        Args:
            identity: Task identity.

        Returns:
            Restored state, or `None` when retry is unavailable.
        """
        ...

    async def delete(self, identity: DownloadIdentity, *, local: bool = False):
        """Delete a task and optionally its local files.

        Args:
            identity: Task identity.
            local: Whether to delete downloaded local files. Defaults to `False`.
        """
        ...

    async def close(self):
        """Release resources owned by the driver."""
        ...
