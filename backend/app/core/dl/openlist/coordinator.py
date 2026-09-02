from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from filelock import Timeout
from tortoise.timezone import now
from tortoise.transactions import in_transaction

from app.core.dl.driver import DownloadIdentity, DownloadSnapshot, DownloadState
from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.finalizer import finalize_job, reconcile_local_manifest
from app.core.dl.openlist.manifest import (
    REFRESH_INTERVAL,
    STABILITY_WINDOW,
    RefreshLimiter,
    build_manifest,
    observe_manifest,
    shared_refresh_limiter,
)
from app.core.dl.openlist.models import (
    OpenListConfig,
    OpenListErrorKind,
    RemoteCleanupPolicy,
    RemoteTask,
    RemoteTaskState,
)
from app.core.dl.openlist.puller import PullError
from app.core.dl.openlist.state import (
    poll_delay,
    rate_limit_delay,
    remote_progress,
    state_progress,
    transient_delay,
)
from app.models.download import (
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)

if TYPE_CHECKING:
    from app.core.dl.openlist.runtime import OpenListPullRuntime

_ACTIVE_STATES = {
    RemoteTaskState.PENDING,
    RemoteTaskState.RUNNING,
    RemoteTaskState.RETRYING,
}
_CONTROL_STATES = {
    DownloadState.SUBMITTING,
    DownloadState.SUBMIT_UNKNOWN,
    DownloadState.REMOTE,
    DownloadState.SETTLING,
    DownloadState.VERIFYING,
}
_FAILED_TRANSFER_STATES = {RemoteTaskState.FAILED, RemoteTaskState.CANCELED}

_REMOTE_ERROR = "OpenList remote task failed"
_REMOTE_MISSING_ERROR = "OpenList remote task is missing"
_TRANSFER_ERROR = "OpenList transfer task failed"
_MANIFEST_ERROR = "OpenList remote manifest is invalid"
_SUBMIT_UNKNOWN_ERROR = "OpenList submission result is unknown"

_SUBMISSION_GRACE = timedelta(seconds=30)


@dataclass(slots=True)
class OpenListCoordinator:
    """Coordinate persisted OpenList jobs across remote and local phases."""

    config: OpenListConfig
    client: OpenListClient
    pull_runtime: "OpenListPullRuntime" = field(repr=False)
    refresh_limiter: RefreshLimiter = field(init=False, repr=False)
    _auth_blocked: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        self.refresh_limiter = shared_refresh_limiter(self.config.base_url)

    async def sync(
        self, identities: Sequence[DownloadIdentity]
    ) -> tuple[DownloadSnapshot, ...]:
        """Synchronize due jobs while applying instance-level failure policy.

        Args:
            identities: The local tasks assigned to this OpenList driver.

        Returns:
            The task snapshots produced by this synchronization cycle.
        """
        # a configuration change replaces the driver and clears this auth block
        if self._auth_blocked:
            return ()
        try:
            return await self._sync(identities)
        except OpenListClientError as exc:
            if exc.kind is OpenListErrorKind.AUTH:
                self._auth_blocked = True
            await self._defer_instance(identities, exc)
            return ()

    async def _sync(
        self, identities: Sequence[DownloadIdentity]
    ) -> tuple[DownloadSnapshot, ...]:
        """Execute one synchronization cycle for due persisted jobs.

        Args:
            identities: The local tasks assigned to this OpenList driver.

        Returns:
            The snapshots whose state was resolved during this cycle.
        """
        # import lazily because `syncer` loads the OpenList driver
        from app.core.dl.syncer import submission_lock

        task_ids = tuple(
            {
                identity.task_id
                for identity in identities
                if identity.task_id is not None
            }
        )
        if not task_ids:
            return ()

        current = now()
        jobs = await OfflineDownloadJob.filter(download_id__in=task_ids).select_related(
            "download"
        )
        # select jobs whose next persisted control action is due
        due = tuple(
            job
            for job in jobs
            if job.download.state
            in {
                DownloadState.SUBMITTING,
                DownloadState.SUBMIT_UNKNOWN,
                DownloadState.REMOTE,
                DownloadState.SETTLING,
                DownloadState.PULLING,
                DownloadState.VERIFYING,
            }
            and (job.next_poll_at is None or job.next_poll_at <= current)
            and (
                job.download.state is not DownloadState.SUBMITTING
                or job.download.created_at is None
                or job.download.created_at <= current - _SUBMISSION_GRACE
            )
        )
        ready = []
        for job in due:
            if job.download.state is DownloadState.SUBMITTING:
                try:
                    with submission_lock(job.job_uuid):
                        await job.download.refresh_from_db()
                        if job.download.state is DownloadState.SUBMITTING:
                            job.download.state = DownloadState.SUBMIT_UNKNOWN
                            job.download.error_msg = _SUBMIT_UNKNOWN_ERROR
                            await job.download.save(
                                update_fields=["state", "error_msg"]
                            )
                except Timeout:
                    # the submitting worker still owns this request
                    continue
            ready.append(job)
        due = tuple(ready)
        remote_jobs = tuple(
            job
            for job in due
            if job.download.state == DownloadState.REMOTE and job.download.unique_id
        )
        unknown_jobs = tuple(
            job
            for job in due
            if job.download.state
            in {DownloadState.SUBMITTING, DownloadState.SUBMIT_UNKNOWN}
        )
        settling_jobs = tuple(
            job for job in due if job.download.state == DownloadState.SETTLING
        )
        pulling_jobs = tuple(
            job for job in due if job.download.state == DownloadState.PULLING
        )
        verifying_jobs = tuple(
            job for job in due if job.download.state == DownloadState.VERIFYING
        )
        if (
            not unknown_jobs
            and not remote_jobs
            and not settling_jobs
            and not pulling_jobs
            and not verifying_jobs
        ):
            return ()

        # `start` reuses an existing process-local pull for the same task
        for job in pulling_jobs:
            self.pull_runtime.start(job)

        offline_tasks = (
            await self.client.undone() if unknown_jobs or remote_jobs else ()
        )
        snapshots = []
        done_tasks: tuple[RemoteTask, ...] | None = None
        if unknown_jobs:
            candidates = {task.id: task for task in offline_tasks}
            done_tasks = await self.client.done()
            candidates.update({task.id: task for task in done_tasks})
            snapshots.extend(
                await self._sync_unknown(
                    unknown_jobs, tuple(candidates.values()), current
                )
            )

        remote_tasks = {}
        if remote_jobs:
            remote_tasks = {task.id: task for task in offline_tasks}
            remote_ids = {str(job.download.unique_id) for job in remote_jobs}
            if remote_ids - remote_tasks.keys():
                if done_tasks is None:
                    done_tasks = await self.client.done()
                remote_tasks.update({task.id: task for task in done_tasks})

        for job in remote_jobs:
            task = job.download
            remote = remote_tasks.get(str(task.unique_id))
            if remote is None:
                try:
                    state = (
                        DownloadState.SETTLING
                        if (await self.client.list(job.remote_dir, 1, 1)).content
                        else DownloadState.ERROR
                    )
                except OpenListClientError as exc:
                    if exc.kind is not OpenListErrorKind.API:
                        raise
                    await self._defer_job(job, current)
                    continue
            elif remote.state in _ACTIVE_STATES:
                state = DownloadState.REMOTE
            elif remote.state is RemoteTaskState.SUCCEEDED:
                state = DownloadState.SETTLING
            else:
                state = DownloadState.ERROR

            if remote is not None and state is DownloadState.REMOTE:
                percentage = remote_progress(
                    task.percentage, remote.progress
                ).percentage
            else:
                percentage = state_progress(state, task.percentage).percentage
            error = None
            if state is DownloadState.ERROR:
                if remote is None:
                    error = _REMOTE_MISSING_ERROR
                else:
                    error = remote.error or _REMOTE_ERROR

            raw_state = remote.state.value if remote is not None else None
            total_size = task.total_size
            if remote is not None and remote.total_bytes:
                total_size = remote.total_bytes
            changed = (
                task.state != state
                or task.raw_state != raw_state
                or task.percentage != percentage
                or task.total_size != total_size
            )
            delay, unchanged_count = poll_delay(
                changed,
                job.unchanged_count,
                base_interval=self.config.poll_interval,
                max_interval=self.config.poll_max_interval,
            )
            next_poll_at = (
                None
                if state is DownloadState.ERROR
                else current + timedelta(seconds=delay)
            )
            error_kind = None
            if error is not None:
                error_kind = (
                    OfflineDownloadErrorKind.REMOTE_TASK_MISSING
                    if remote is None
                    else OfflineDownloadErrorKind.REMOTE_FAILED
                )

            task.state = state
            task.raw_state = raw_state
            task.error_msg = error
            task.percentage = percentage
            task.total_size = total_size
            job.next_poll_at = next_poll_at
            job.unchanged_count = unchanged_count
            job.retry_count = 0
            job.last_error_kind = error_kind
            async with in_transaction() as connection:
                await task.save(
                    update_fields=[
                        "state",
                        "raw_state",
                        "error_msg",
                        "percentage",
                        "total_size",
                    ],
                    using_db=connection,
                )
                await job.save(
                    update_fields=[
                        "next_poll_at",
                        "unchanged_count",
                        "retry_count",
                        "last_error_kind",
                    ],
                    using_db=connection,
                )
            snapshots.append(
                DownloadSnapshot(
                    identity=DownloadIdentity.from_task(task),
                    state=state,
                    error=error,
                    percentage=percentage,
                )
            )
        if settling_jobs:
            snapshots.extend(await self._sync_settling(settling_jobs, current))
        for job in verifying_jobs:
            try:
                snapshots.append(
                    await finalize_job(
                        self.client,
                        job,
                        current,
                        cleanup_on_success=(
                            self.config.remote_cleanup
                            is RemoteCleanupPolicy.DELETE_ON_SUCCESS
                        ),
                    )
                )
            except OpenListClientError as exc:
                if exc.kind is not OpenListErrorKind.API:
                    raise
                await self._defer_job(job, current)
        return tuple(snapshots)

    async def _sync_unknown(
        self,
        jobs: Sequence[OfflineDownloadJob],
        candidates: Sequence[RemoteTask],
        now: datetime,
    ) -> tuple[DownloadSnapshot, ...]:
        """Recover submissions whose remote task ID was not confirmed.

        Args:
            jobs: The uncertain persisted submissions.
            candidates: The unfinished and finished OpenList task candidates.
            now: The current synchronization timestamp.

        Returns:
            The recovered or still-uncertain task snapshots.
        """
        snapshots = []
        for job in jobs:
            task = job.download
            # accept a remote ID only when source and destination identify one task
            matches = tuple(
                remote
                for remote in candidates
                if remote.matches_submission(job.remote_dir, job.source_fingerprint)
            )
            remote_id = matches[0].id if len(matches) == 1 else None
            if remote_id is not None:
                state = DownloadState.REMOTE
                error_msg = None
                error_kind = None
            else:
                try:
                    has_result = bool(
                        (await self.client.list(job.remote_dir, 1, 1)).content
                    )
                except OpenListClientError as exc:
                    if exc.kind is not OpenListErrorKind.API:
                        raise
                    await self._defer_job(job, now)
                    continue
                if has_result:
                    state = DownloadState.SETTLING
                    error_msg = None
                    error_kind = None
                else:
                    state = DownloadState.SUBMIT_UNKNOWN
                    error_msg = _SUBMIT_UNKNOWN_ERROR
                    error_kind = OfflineDownloadErrorKind.SUBMIT_UNKNOWN

            changed = task.state is not state or (
                remote_id is not None and task.unique_id != remote_id
            )
            delay, unchanged_count = poll_delay(
                changed,
                job.unchanged_count,
                base_interval=self.config.poll_interval,
                max_interval=self.config.poll_max_interval,
            )
            percentage = state_progress(state, task.percentage).percentage
            task.state = state
            task.unique_id = remote_id or task.unique_id
            task.error_msg = error_msg
            task.percentage = percentage
            job.next_poll_at = now + timedelta(seconds=delay)
            job.unchanged_count = unchanged_count
            job.retry_count = 0
            job.last_error_kind = error_kind
            async with in_transaction() as connection:
                await task.save(
                    update_fields=[
                        "state",
                        "unique_id",
                        "error_msg",
                        "percentage",
                    ],
                    using_db=connection,
                )
                await job.save(
                    update_fields=[
                        "next_poll_at",
                        "unchanged_count",
                        "retry_count",
                        "last_error_kind",
                    ],
                    using_db=connection,
                )
            snapshots.append(
                DownloadSnapshot(
                    identity=DownloadIdentity.from_task(task),
                    state=state,
                    error=error_msg,
                    percentage=percentage,
                )
            )
        return tuple(snapshots)

    async def _sync_settling(
        self, jobs: Sequence[OfflineDownloadJob], now: datetime
    ) -> tuple[DownloadSnapshot, ...]:
        """Wait for transfer completion and a stable remote result manifest.

        Args:
            jobs: The jobs waiting for OpenList result files to settle.
            now: The current synchronization timestamp.

        Returns:
            The settling, pulling, or failed task snapshots.
        """
        # track OpenList transfer tasks for its internal move into mounted storage
        active = await self.client.transfer_undone()
        completed = await self.client.transfer_done()
        failed = tuple(
            remote
            for remote in active + completed
            if remote.state in _FAILED_TRANSFER_STATES
        )
        snapshots = []
        for job in jobs:
            task = job.download
            error = None
            error_kind = None
            failed_task = next(
                (remote for remote in failed if job.job_uuid in remote.name), None
            )
            if failed_task is not None:
                state = DownloadState.ERROR
                error = failed_task.error or _TRANSFER_ERROR
                error_kind = OfflineDownloadErrorKind.TRANSFER_FAILED
                next_poll_at = None
                unchanged_count = job.unchanged_count
            elif any(
                job.job_uuid in remote.name and remote.state in _ACTIVE_STATES
                for remote in active
            ):
                state = DownloadState.SETTLING
                delay, unchanged_count = poll_delay(
                    False,
                    job.unchanged_count,
                    base_interval=self.config.poll_interval,
                    max_interval=self.config.poll_max_interval,
                )
                next_poll_at = now + timedelta(seconds=delay)
            else:
                try:
                    entries = await build_manifest(self.client, job.remote_dir)
                    expected_size = task.total_size or 0
                    manifest_size = sum(
                        entry.size for entry in entries if not entry.is_dir
                    )
                    incomplete = manifest_size < expected_size
                    observation = observe_manifest(
                        entries,
                        previous_fingerprint=job.manifest_fingerprint,
                        changed_at=job.manifest_changed_at,
                        now=now,
                    )
                    # refresh mounted storage only when needed and rate-limit the call
                    if (
                        observation.needs_refresh or incomplete
                    ) and self.refresh_limiter.acquire(job.job_uuid, now):
                        entries = await build_manifest(
                            self.client, job.remote_dir, refresh=True
                        )
                        manifest_size = sum(
                            entry.size for entry in entries if not entry.is_dir
                        )
                        incomplete = manifest_size < expected_size
                        observation = observe_manifest(
                            entries,
                            previous_fingerprint=job.manifest_fingerprint,
                            changed_at=job.manifest_changed_at,
                            now=now,
                        )
                except OpenListClientError as exc:
                    if exc.kind is not OpenListErrorKind.API:
                        raise
                    await self._defer_job(job, now)
                    continue
                except ValueError:
                    state = DownloadState.ERROR
                    error = _MANIFEST_ERROR
                    error_kind = OfflineDownloadErrorKind.MANIFEST_INVALID
                    next_poll_at = None
                    unchanged_count = job.unchanged_count
                else:
                    try:
                        manifest = (
                            reconcile_local_manifest(job, entries)
                            if entries and not incomplete
                            else None
                        )
                    except PullError as exc:
                        state = DownloadState.ERROR
                        error = str(exc)
                        error_kind = exc.kind
                        next_poll_at = None
                        unchanged_count = job.unchanged_count
                    else:
                        # preserve the last usable manifest for incomplete results
                        if manifest is not None:
                            task.total_size = manifest_size
                            if observation.stable:
                                top_level = [
                                    entry.path
                                    for entry in entries
                                    if "/" not in entry.path
                                ]
                                if len(top_level) == 1:
                                    task.name = top_level[0][:255]
                            job.manifest = manifest
                            job.manifest_fingerprint = observation.fingerprint
                            job.manifest_changed_at = observation.changed_at
                        # pull only after one unchanged stability window
                        state = (
                            DownloadState.PULLING
                            if observation.stable and not incomplete
                            else DownloadState.SETTLING
                        )
                        if state is DownloadState.PULLING:
                            next_poll_at = None
                            unchanged_count = 0
                        else:
                            delay, unchanged_count = poll_delay(
                                observation.changed,
                                job.unchanged_count,
                                base_interval=self.config.poll_interval,
                                max_interval=self.config.poll_max_interval,
                            )
                            if observation.needs_refresh or incomplete:
                                delay = max(delay, REFRESH_INTERVAL.total_seconds())
                            elif observation.changed:
                                delay = max(delay, STABILITY_WINDOW.total_seconds())
                            next_poll_at = now + timedelta(seconds=delay)

            percentage = state_progress(state, task.percentage).percentage
            task.state = state
            task.error_msg = error
            task.percentage = percentage
            job.next_poll_at = next_poll_at
            job.unchanged_count = unchanged_count
            job.retry_count = 0
            job.last_error_kind = error_kind
            async with in_transaction() as connection:
                await task.save(
                    update_fields=[
                        "name",
                        "state",
                        "error_msg",
                        "percentage",
                        "total_size",
                    ],
                    using_db=connection,
                )
                await job.save(
                    update_fields=[
                        "manifest",
                        "manifest_fingerprint",
                        "manifest_changed_at",
                        "next_poll_at",
                        "unchanged_count",
                        "retry_count",
                        "last_error_kind",
                    ],
                    using_db=connection,
                )
            snapshots.append(
                DownloadSnapshot(
                    identity=DownloadIdentity.from_task(task),
                    state=state,
                    error=error,
                    percentage=percentage,
                )
            )
            if state is DownloadState.PULLING:
                self.pull_runtime.start(job)
        return tuple(snapshots)

    async def _defer_job(self, job: OfflineDownloadJob, current: datetime):
        """Defer one job after an API error scoped to its remote result.

        Args:
            job: The affected persisted offline download job.
            current: The current synchronization timestamp.
        """
        error_kind = OfflineDownloadErrorKind.INSTANCE_TRANSIENT
        previous = job.retry_count if job.last_error_kind is error_kind else 0
        delay, job.retry_count = transient_delay(previous)
        job.last_error_kind = error_kind
        job.next_poll_at = current + timedelta(seconds=delay)
        await job.save(update_fields=["last_error_kind", "retry_count", "next_poll_at"])

    async def _defer_instance(
        self,
        identities: Sequence[DownloadIdentity],
        error: OpenListClientError,
    ):
        """Persist instance-level authentication, rate-limit, or transient failure.

        Args:
            identities: The tasks affected by the failed OpenList request.
            error: The classified instance request failure.
        """
        task_ids = tuple(
            identity.task_id for identity in identities if identity.task_id is not None
        )
        jobs = await OfflineDownloadJob.filter(download_id__in=task_ids).select_related(
            "download"
        )
        jobs = [job for job in jobs if job.download.state in _CONTROL_STATES]
        if not jobs:
            return

        current = now()
        retry_count = 0
        if error.kind is OpenListErrorKind.AUTH:
            # authentication requires configuration changes rather than timed retries
            error_kind = OfflineDownloadErrorKind.INSTANCE_AUTH
            next_poll_at = None
        else:
            error_kind = (
                OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT
                if error.kind is OpenListErrorKind.RATE_LIMIT
                else OfflineDownloadErrorKind.INSTANCE_TRANSIENT
            )
            previous = max(
                (job.retry_count for job in jobs if job.last_error_kind is error_kind),
                default=0,
            )
            if error.kind is OpenListErrorKind.RATE_LIMIT:
                delay = rate_limit_delay(error.retry_after, current)
                retry_count = previous + 1
            else:
                delay, retry_count = transient_delay(previous)
            next_poll_at = current + timedelta(seconds=delay)

        for job in jobs:
            job.last_error_kind = error_kind
            if error.kind is OpenListErrorKind.AUTH:
                job.retry_count = retry_count
                job.next_poll_at = None
            else:
                job.retry_count = retry_count
                if job.next_poll_at is None or job.next_poll_at < next_poll_at:
                    job.next_poll_at = next_poll_at
        await OfflineDownloadJob.bulk_update(
            jobs, fields=["last_error_kind", "retry_count", "next_poll_at"]
        )
