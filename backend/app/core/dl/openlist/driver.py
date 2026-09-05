import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import PurePosixPath
from typing import NoReturn
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from sanic import Sanic
from sanic.log import logger
from tortoise import timezone
from tortoise.expressions import Q

from app.core.constants import ENCODING
from app.core.dl.driver import (
    DownloadAction,
    DownloadDraft,
    DownloadIdentity,
    DownloadRequest,
    DownloadSnapshot,
    DownloadSource,
    DownloadState,
    OfflineJobDraft,
)
from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.coordinator import OpenListCoordinator
from app.core.dl.openlist.models import (
    OpenListConfig,
    OpenListErrorKind,
    RemoteCleanupPolicy,
    RemoteTaskState,
)
from app.core.dl.openlist.puller import delete_local_directory, delete_local_file
from app.core.dl.openlist.runtime import OpenListPullRuntime
from app.core.dl.openlist.state import (
    rate_limit_delay,
    retry_state,
    state_progress,
    transient_delay,
)
from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.download import OfflineDownloadErrorKind, OfflineDownloadJob


@dataclass(slots=True)
class OpenListDriver:
    """Provide downloads through one OpenList offline download tool."""

    source_types = frozenset(DownloadSource)
    supports_version = True

    config: OpenListConfig
    client: OpenListClient | None = field(default=None, init=False, repr=False)
    coordinator: OpenListCoordinator | None = field(
        default=None, init=False, repr=False
    )

    async def validate(self) -> str:
        """Validate the tool, administrator access, and writable remote root.

        Raises:
            KaloscopeException: If the OpenList configuration or request fails.

        Returns:
            The OpenList endpoint version.
        """
        client = self._control_client()
        try:
            if self.config.tool not in await client.tools(self.config.remote_root):
                raise KaloscopeException(ErrorCode.INVALID_YAML_CONFIG)
            await client.require_admin()
            if not await client.remote_root_writable():
                raise KaloscopeException(ErrorCode.INVALID_YAML_CONFIG)
            return await client.version()
        except OpenListClientError as exc:
            raise KaloscopeException(
                exc.response_message or ErrorCode.HTTP_REQUEST_FAILED
            ) from None

    async def version(self) -> str:
        """Get the OpenList endpoint version.

        Returns:
            The normalized OpenList version.
        """
        return await self._control_client().version()

    def prepare(self, request: DownloadRequest) -> DownloadDraft:
        """Build the task and offline-job draft required before submission.

        Args:
            request: The normalized download request.

        Raises:
            ValueError: If the request has no identity or source.

        Returns:
            The draft containing local task data and an isolated remote directory.
        """
        identity = request.identity
        source = request.link
        if identity is None or not source:
            raise ValueError("OpenList request is missing task metadata")
        name = identity.info_hash or identity.info_hash_v2 or _source_name(source)

        job_uuid = uuid4().hex
        remote_directory = (
            PurePosixPath(self.config.remote_root) / job_uuid
        ).as_posix()
        return DownloadDraft(
            request=replace(request, remote_directory=remote_directory),
            name=name,
            state=DownloadState.SUBMITTING,
            magnet_link=(
                _public_magnet(identity)
                if identity.info_hash or identity.info_hash_v2
                else None
            ),
            percentage=0,
            job=OfflineJobDraft(
                job_uuid=job_uuid,
                source_fingerprint=hashlib.sha256(source.encode(ENCODING)).hexdigest(),
                remote_directory=remote_directory,
            ),
        )

    async def add(self, request: DownloadRequest) -> DownloadSnapshot:
        """Create the remote directory and submit one source to OpenList.

        Args:
            request: The prepared download request.

        Raises:
            ValueError: If required prepared task metadata is missing.

        Returns:
            The initial snapshot, including an uncertain state when submission
            completion cannot be confirmed safely.
        """
        identity = request.identity
        source = request.link
        if identity is None or request.remote_directory is None or not source:
            raise ValueError("OpenList request is missing prepared task metadata")

        client = self._control_client()
        await client.mkdir(request.remote_directory)
        try:
            task_ids = await client.submit(source, request.remote_directory)
        except OpenListClientError as exc:
            # a complete business rejection confirms that this source was not queued
            if exc.response_message is not None or exc.kind not in {
                OpenListErrorKind.TRANSIENT,
                OpenListErrorKind.INVALID_RESPONSE,
            }:
                try:
                    path = PurePosixPath(request.remote_directory)
                    await self._remove_remote_directory(
                        path.name, request.remote_directory
                    )
                except Exception:
                    logger.warning(
                        "Failed to clean up OpenList submission directory",
                        exc_info=True,
                    )
                if exc.response_message is not None:
                    raise KaloscopeException(
                        exc.response_message or ErrorCode.HTTP_REQUEST_FAILED
                    ) from None
                raise
            return _unknown_snapshot(identity)
        # multiple returned IDs cannot be matched safely to one local task
        if len(task_ids) > 1:
            return _unknown_snapshot(identity)
        state = DownloadState.REMOTE if task_ids else DownloadState.SETTLING
        return DownloadSnapshot(
            identity=replace(
                identity,
                remote_id=task_ids[0] if task_ids else None,
            ),
            state=state,
            percentage=state_progress(state, 0).percentage,
        )

    async def sync(
        self, identities: Sequence[DownloadIdentity]
    ) -> tuple[DownloadSnapshot, ...]:
        """Coordinate rate-limited remote polling and local result pulling.

        Args:
            identities: The tasks owned by this OpenList driver.

        Returns:
            The task snapshots produced during this synchronization cycle.
        """
        if self.coordinator is None:
            client = self._control_client()
            runtime = OpenListPullRuntime(self.config, client)
            self.coordinator = OpenListCoordinator(self.config, client, runtime)
        await self._retry_cleanup(identities)
        snapshots = await self.coordinator.sync(identities)
        for snapshot in snapshots:
            if snapshot.state is DownloadState.COMPLETED:
                await self._cleanup_completed(snapshot)
        return snapshots

    async def capabilities(
        self,
        identity: DownloadIdentity,
        state: DownloadState,
        *,
        retry_target: DownloadState | None = None,
    ) -> frozenset[DownloadAction]:
        """Get the actions safe for the current OpenList lifecycle phase.

        Args:
            identity: The local and remote task identity.
            state: The current persisted task state.
            retry_target: The safe recovery state selected from a persisted error.

        Returns:
            The actions supported for this task.
        """
        actions = {DownloadAction.DELETE}
        if state is DownloadState.PULLING:
            actions.add(DownloadAction.PAUSE)
        elif state is DownloadState.PAUSED:
            actions.add(DownloadAction.RESUME)
        elif state is DownloadState.REMOTE and identity.remote_id:
            actions.add(DownloadAction.CANCEL)
        elif (
            state is DownloadState.ERROR
            and retry_target is not None
            and (retry_target is not DownloadState.REMOTE or identity.remote_id)
        ):
            actions.add(DownloadAction.RETRY)
        return frozenset(actions)

    async def pause(self, identity: DownloadIdentity) -> DownloadState:
        """Cancel local pulling and return the paused state.

        Args:
            identity: The local and remote task identity.

        Returns:
            The `PAUSED` state.
        """
        if self.coordinator is not None and identity.task_id is not None:
            await self.coordinator.pull_runtime.cancel(identity.task_id)
        return DownloadState.PAUSED

    async def resume(self, identity: DownloadIdentity) -> DownloadState:
        """Return the local pulling state for a paused task.

        Args:
            identity: The local and remote task identity.

        Returns:
            The `PULLING` state.
        """
        return DownloadState.PULLING

    async def cancel(self, identity: DownloadIdentity):
        """Cancel the remote offline download task when its ID is known.

        Args:
            identity: The local and remote task identity.
        """
        if identity.remote_id:
            await self._control_client().cancel(identity.remote_id)

    async def retry(self, identity: DownloadIdentity) -> DownloadState | None:
        """Retry required remote work and restore the selected recovery phase.

        Args:
            identity: The local and remote task identity.

        Returns:
            The restored state, or `None` when the task cannot be retried.
        """
        target = DownloadState.REMOTE
        if identity.task_id is not None:
            job = await OfflineDownloadJob.get_or_none(download_id=identity.task_id)
            if job is not None and job.last_error_kind is not None:
                target = retry_state(job.last_error_kind)
                if target is None:
                    return None
                if job.last_error_kind is OfflineDownloadErrorKind.TRANSFER_FAILED:
                    client = self._control_client()
                    unfinished = await client.transfer_undone()
                    finished = await client.transfer_done()
                    retried: set[str] = set()
                    for task in unfinished + finished:
                        if (
                            job.job_uuid not in task.name
                            or task.state
                            not in {RemoteTaskState.FAILED, RemoteTaskState.CANCELED}
                            or task.id in retried
                        ):
                            continue
                        await client.retry_transfer(task.id)
                        retried.add(task.id)
                    return target if retried else None
        if target is not DownloadState.REMOTE:
            return target
        if identity.remote_id is None:
            return None
        await self._control_client().retry(identity.remote_id)
        return target

    async def delete(self, identity: DownloadIdentity, *, local: bool = False):
        """Stop active job work and delete job-scoped local paths.

        Args:
            identity: The local and remote task identity.
            local: Whether to also delete locally pulled final files.
        """
        if self.coordinator is not None and identity.task_id is not None:
            await self.coordinator.pull_runtime.cancel(identity.task_id)
        if identity.task_id is None:
            return
        job = await (
            OfflineDownloadJob.filter(download_id=identity.task_id)
            .select_related("download")
            .first()
        )
        if job is None:
            return
        if job.download.state in {
            DownloadState.SUBMITTING,
            DownloadState.SUBMIT_UNKNOWN,
        }:
            client = self._control_client()
            for task in await client.undone():
                if task.matches_submission(job.remote_dir, job.source_fingerprint):
                    await client.cancel(task.id)
        if job.download.state in {
            DownloadState.SUBMITTING,
            DownloadState.SUBMIT_UNKNOWN,
            DownloadState.REMOTE,
            DownloadState.SETTLING,
        } or (
            job.download.state is DownloadState.ERROR
            and job.last_error_kind
            in {
                OfflineDownloadErrorKind.TRANSFER_FAILED,
                OfflineDownloadErrorKind.REMOTE_FAILED,
                OfflineDownloadErrorKind.REMOTE_TASK_MISSING,
            }
        ):
            client = self._control_client()
            for task in await client.transfer_undone():
                if job.job_uuid in task.name:
                    await client.cancel_transfer(task.id)
        manifest = job.manifest or ()
        for item in manifest:
            if not item["is_dir"]:
                delete_local_file(
                    job.download.dir,
                    item["path"],
                    job.job_uuid,
                    keep_final=not local,
                )
        if not local:
            return
        directories = (item["path"] for item in manifest if item["is_dir"])
        for path in sorted(
            directories,
            key=lambda value: len(PurePosixPath(value).parts),
            reverse=True,
        ):
            delete_local_directory(job.download.dir, path)

    async def close(self):
        """Release process-local pull tasks and transfer connections."""
        if self.coordinator is not None:
            await self.coordinator.pull_runtime.close()
        self.coordinator = None

    async def _cleanup_remote(
        self,
        *,
        state: DownloadState,
        job_uuid: str,
        remote_directory: str,
        owner_count: int,
    ):
        """Delete an exclusively owned and safely scoped remote result directory.

        Args:
            state: The final task state.
            job_uuid: The UUID assigned to the offline job.
            remote_directory: The remote result directory owned by the job.
            owner_count: The number of jobs referencing the remote directory.
        """
        if self.config.remote_cleanup is RemoteCleanupPolicy.KEEP:
            return
        if state is not DownloadState.COMPLETED or owner_count != 1:
            _reject_cleanup()
        await self._remove_remote_directory(job_uuid, remote_directory)

    async def _remove_remote_directory(self, job_uuid: str, remote_directory: str):
        """Delete the persisted UUID child owned by one job.

        Args:
            job_uuid: The UUID assigned to the offline job.
            remote_directory: The remote result directory owned by the job.
        """
        try:
            parsed_uuid = UUID(job_uuid)
        except ValueError:
            _reject_cleanup()

        path = PurePosixPath(remote_directory)
        root = path.parent
        # delete only the UUID-named direct child owned by this job
        if (
            parsed_uuid.hex != job_uuid
            or not path.is_absolute()
            or "\0" in remote_directory
            or ".." in path.parts
            or path.as_posix() != remote_directory
            or path == root
            or path.name != job_uuid
        ):
            _reject_cleanup()
        await self._control_client().remove(root.as_posix(), job_uuid)

    def _control_client(self) -> OpenListClient:
        """Get or lazily create the OpenList control client.

        Returns:
            The shared OpenList control client.
        """
        if self.client is None:
            self.client = OpenListClient(self.config, Sanic.get_app().ctx.httpx)
        return self.client

    async def _cleanup_completed(self, snapshot: DownloadSnapshot):
        """Apply configured remote cleanup after local completion.

        Args:
            snapshot: The completed task snapshot.
        """
        state = snapshot.state
        task_id = snapshot.identity.task_id
        if state is not DownloadState.COMPLETED or task_id is None:
            return
        job = await (
            OfflineDownloadJob.filter(download_id=task_id)
            .select_related("download")
            .first()
        )
        if job is None:
            return
        await self._cleanup_job(job)

    async def _retry_cleanup(self, identities: Sequence[DownloadIdentity]):
        """Retry due remote cleanups for completed OpenList tasks.

        Args:
            identities: The tasks selected for this synchronization cycle.
        """
        task_ids = tuple(
            identity.task_id for identity in identities if identity.task_id is not None
        )
        if not task_ids:
            return
        jobs = await OfflineDownloadJob.filter(
            Q(download_id__in=task_ids),
            Q(last_error_kind=OfflineDownloadErrorKind.CLEANUP_FAILED)
            | Q(next_poll_at__not_isnull=True),
        ).select_related("download")
        current = timezone.now()
        for job in jobs:
            pending = (
                job.last_error_kind is OfflineDownloadErrorKind.CLEANUP_FAILED
                or job.next_poll_at is not None
            )
            if (
                job.download.state is DownloadState.COMPLETED
                and pending
                and (job.next_poll_at is None or job.next_poll_at <= current)
            ):
                await self._cleanup_job(job)

    async def _cleanup_job(self, job: OfflineDownloadJob):
        """Apply or retire persisted cleanup work for one completed job.

        Args:
            job: The completed offline job whose remote directory may be removed.
        """
        pending = (
            job.last_error_kind is OfflineDownloadErrorKind.CLEANUP_FAILED
            or job.next_poll_at is not None
            or job.retry_count > 0
        )
        if self.config.remote_cleanup is RemoteCleanupPolicy.KEEP:
            if pending:
                job.last_error_kind = None
                job.next_poll_at = None
                job.retry_count = 0
                await job.save(
                    update_fields=["last_error_kind", "next_poll_at", "retry_count"]
                )
            return

        # share persisted cleanup rate limits with this downloader's other completions
        limited = await (
            OfflineDownloadJob.filter(
                download__downloader_id=job.download.downloader_id,
                download__state=DownloadState.COMPLETED,
                last_error_kind=OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT,
                next_poll_at__gt=timezone.now(),
            )
            .order_by("-next_poll_at")
            .first()
        )
        if limited is not None:
            job.next_poll_at = limited.next_poll_at
            job.last_error_kind = OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT
            await job.save(update_fields=["next_poll_at", "last_error_kind"])
            return

        owner_count = await OfflineDownloadJob.filter(remote_dir=job.remote_dir).count()
        try:
            await self._cleanup_remote(
                state=DownloadState.COMPLETED,
                job_uuid=job.job_uuid,
                remote_directory=job.remote_dir,
                owner_count=owner_count,
            )
        except Exception as exc:
            # preserve local completion when optional remote cleanup fails
            if (
                isinstance(exc, OpenListClientError)
                and exc.kind is OpenListErrorKind.RATE_LIMIT
            ):
                delay = rate_limit_delay(exc.retry_after, timezone.now())
                job.retry_count += 1
                job.last_error_kind = OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT
            else:
                delay, job.retry_count = transient_delay(job.retry_count)
                job.last_error_kind = OfflineDownloadErrorKind.CLEANUP_FAILED
            job.next_poll_at = timezone.now() + timedelta(seconds=delay)
            await job.save(
                update_fields=["last_error_kind", "next_poll_at", "retry_count"]
            )
            logger.warning("Failed OpenList remote cleanup", exc_info=True)
            return
        if pending:
            job.last_error_kind = None
            job.next_poll_at = None
            job.retry_count = 0
            await job.save(
                update_fields=["last_error_kind", "next_poll_at", "retry_count"]
            )


def _reject_cleanup() -> NoReturn:
    """Reject unsafe cleanup without retiring its persisted retry state.

    Raises:
        ValueError: Always, because the directory ownership check failed.
    """
    raise ValueError("Unsafe OpenList remote cleanup")


def _unknown_snapshot(identity: DownloadIdentity) -> DownloadSnapshot:
    """Build a snapshot for an unconfirmed OpenList submission.

    Args:
        identity: The persisted local task identity.

    Returns:
        The `SUBMIT_UNKNOWN` task snapshot.
    """
    return DownloadSnapshot(
        identity=identity,
        state=DownloadState.SUBMIT_UNKNOWN,
        error="OpenList submission result is unknown",
        percentage=0,
    )


def _public_magnet(identity: DownloadIdentity) -> str:
    """Build a public magnet URI from persisted BitTorrent hashes.

    Args:
        identity: The task identity containing BitTorrent hashes.

    Returns:
        The magnet URI without private tracker parameters.
    """
    topics = []
    if identity.info_hash:
        topics.append(f"xt=urn:btih:{identity.info_hash}")
    if identity.info_hash_v2:
        topics.append(f"xt=urn:btmh:{identity.info_hash_v2}")
    return "magnet:?" + "&".join(topics)


def _source_name(source: str) -> str:
    """Derive a bounded display name from a non-magnet source URI.

    Args:
        source: The raw source URI.

    Returns:
        The decoded final path segment, host name, or fallback name.
    """
    try:
        parsed = urlsplit(source)
        path = unquote(parsed.path).rstrip("/")
        name = PurePosixPath(path).name if path else parsed.hostname
    except ValueError:
        name = None
    return (name or "download")[:255]
