import asyncio
from contextlib import suppress
from datetime import timedelta

from sanic.log import logger
from tortoise import timezone
from tortoise.transactions import in_transaction

from app.core.dl.driver import DownloadState
from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.manifest import RemoteManifestEntry
from app.core.dl.openlist.models import OpenListConfig, OpenListErrorKind
from app.core.dl.openlist.puller import (
    DataClient,
    PullError,
    find_completed_file,
    prepare_local_directory,
    prepare_local_file,
    pull_file,
)
from app.core.dl.openlist.state import pull_progress, rate_limit_delay, transient_delay
from app.core.notifications import Notifications, NotificationTemplate
from app.models.download import (
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)

# map link-client failures to the persisted offline-job error categories
_E = OfflineDownloadErrorKind
_CLIENT_ERROR_KINDS = {
    OpenListErrorKind.AUTH: _E.INSTANCE_AUTH,
    OpenListErrorKind.RATE_LIMIT: _E.INSTANCE_RATE_LIMIT,
    OpenListErrorKind.TRANSIENT: _E.INSTANCE_TRANSIENT,
    OpenListErrorKind.INVALID_RESPONSE: _E.DIRECT_LINK_UNAVAILABLE,
    OpenListErrorKind.API: _E.DIRECT_LINK_UNAVAILABLE,
}


class _DeferredPullError(PullError):
    """Carry a retryable direct-link failure into durable pull scheduling."""

    def __init__(self, error: OpenListClientError):
        super().__init__(_CLIENT_ERROR_KINDS[error.kind], str(error))
        self.retry_after = error.retry_after


def _first_pull_error(group: BaseExceptionGroup[PullError]) -> PullError:
    """Extract the first `PullError` from a nested exception group.

    Args:
        group: The failure group raised by the pull `TaskGroup`.

    Returns:
        The first classified pull failure in the group.
    """
    error = group.exceptions[0]
    if isinstance(error, PullError):
        return error
    return _first_pull_error(error)


class OpenListPullRuntime:
    """Run and own process-local pulls for OpenList jobs.

    The runtime deduplicates pulls by `DownloadTask.id`, lazily owns the data-plane
    client, and advances successful pulls only to `VERIFYING`. The coordinator and
    `finalize_job` remain responsible for durable scheduling and final verification.
    """

    def __init__(
        self,
        config: OpenListConfig,
        link_client: OpenListClient,
    ):
        self._config, self._link_client = config, link_client
        self._data_client: DataClient | None = None
        self._semaphore = asyncio.Semaphore(config.pull_concurrency)
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def _file_client(self) -> DataClient:
        """Get the lazily initialized data-plane client.

        Returns:
            The shared client used for direct file transfers.
        """
        if self._data_client is None:
            self._data_client = DataClient(self._config.base_url)
        return self._data_client

    def start(self, job: OfflineDownloadJob):
        """Start or reuse the local pull for a persisted job.

        Args:
            job: The job containing the stable manifest and local target.
        """
        existing = self._tasks.get(job.download_id)
        if existing is not None and not existing.done():
            return
        handle = asyncio.create_task(
            self._run_job(job), name=f"openlist-pull-{job.download_id}"
        )
        self._tasks[job.download_id] = handle
        handle.add_done_callback(lambda done: self._discard(job.download_id, done))

    def _discard(self, job_id: int, handle: asyncio.Task[None]):
        """Discard a completed handle when it is still the registered task.

        Args:
            job_id: The local `DownloadTask` ID used as the runtime key.
            handle: The completed pull task being removed.
        """
        if self._tasks.get(job_id) is handle:
            self._tasks.pop(job_id)
        if handle.cancelled():
            return
        error = handle.exception()
        if error is not None:
            logger.error(
                "Failed to persist OpenList pull failure: %s",
                job_id,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def cancel(self, task_id: int):
        """Cancel the active pull for a task.

        Args:
            task_id: The local `DownloadTask` ID.
        """
        handle = self._tasks.get(task_id)
        if handle is None:
            return
        handle.cancel()
        with suppress(asyncio.CancelledError):
            await handle

    async def close(self):
        """Cancel active pulls and close the owned data client."""
        await asyncio.gather(
            *(self.cancel(task_id) for task_id in tuple(self._tasks)),
            return_exceptions=True,
        )
        self._tasks.clear()
        if self._data_client is not None:
            await self._data_client.aclose()
            self._data_client = None

    async def _run_job(self, job: OfflineDownloadJob):
        """Run a local pull and persist every terminal failure.

        Args:
            job: The persisted job being pulled.
        """
        try:
            await self._pull_job(job)
        except asyncio.CancelledError:
            raise
        except _DeferredPullError as error:
            await self._defer_job(job, error)
        except PullError as error:
            await self._fail_job(job, error)
        except Exception:
            logger.error(
                "OpenList local pull crashed: %s", job.download_id, exc_info=True
            )
            await self._fail_job(
                job,
                PullError(
                    OfflineDownloadErrorKind.PULL_FAILED,
                    "OpenList local pull failed",
                ),
            )

    @staticmethod
    async def _defer_job(job: OfflineDownloadJob, error: _DeferredPullError):
        """Keep a transient link failure resumable across driver restarts.

        Args:
            job: The persisted job whose direct link could not be obtained.
            error: The retryable failure and optional `Retry-After` value.
        """
        current = timezone.now()
        previous = job.retry_count if job.last_error_kind is error.kind else 0
        if error.kind is OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT:
            delay = rate_limit_delay(error.retry_after, current)
            retries = previous + 1
        else:
            delay, retries = transient_delay(previous)
        async with in_transaction() as connection:
            updated = await (
                DownloadTask.filter(id=job.download_id, state=DownloadState.PULLING)
                .using_db(connection)
                .update(dl_speed=0, error_msg=str(error))
            )
            if updated:
                await (
                    OfflineDownloadJob.filter(id=job.id)
                    .using_db(connection)
                    .update(
                        last_error_kind=error.kind,
                        retry_count=retries,
                        next_poll_at=current + timedelta(seconds=delay),
                    )
                )

    @staticmethod
    async def _fail_job(job: OfflineDownloadJob, error: PullError):
        """Persist and notify a pull failure while the task is still active.

        Args:
            job: The persisted job whose pull failed.
            error: The classified failure exposed to the task.
        """
        task = job.download
        async with in_transaction() as connection:
            updated = await (
                DownloadTask.filter(id=task.id, state=DownloadState.PULLING)
                .using_db(connection)
                .update(
                    state=DownloadState.ERROR,
                    error_msg=str(error),
                    dl_speed=0,
                )
            )
            if updated:
                await (
                    OfflineDownloadJob.filter(id=job.id)
                    .using_db(connection)
                    .update(last_error_kind=error.kind)
                )
        if not updated:
            return
        try:
            await Notifications.send(
                NotificationTemplate.DOWNLOAD_FAILED,
                name=task.name,
                error=str(error),
            )
        except Exception:
            # keep the persisted failure when notification storage is unavailable
            logger.warning(
                "Failed to send OpenList download failure notification",
                exc_info=True,
            )

    async def _pull_job(self, job: OfflineDownloadJob):
        """Pull every persisted manifest entry into local storage.

        Keep transient link failures in `PULLING` with a persisted retry deadline.
        Move terminal failures to `ERROR` and advance successful pulls to
        `VERIFYING`, leaving final checks to `finalize_job`.

        Args:
            job: The job containing the stable manifest and local target directory.
        """
        task = job.download
        # rebuild typed manifest entries from a persisted job
        entries = tuple(
            RemoteManifestEntry(
                path=item["path"],
                is_dir=item["is_dir"],
                size=item["size"],
                hashes=tuple(item.get("hashes", {}).items()),
            )
            for item in job.manifest or ()
        )
        total = sum(entry.size for entry in entries if not entry.is_dir)
        completed = {entry.path: 0 for entry in entries if not entry.is_dir}
        speeds = completed.copy()
        # serialize updates to shared per-entry progress aggregates
        lock = asyncio.Lock()

        async def update(path: str, size: int, speed: int):
            async with lock:
                completed[path] = size
                speeds[path] = speed
                progress = pull_progress(
                    sum(completed.values()),
                    total,
                    sum(speeds.values()),
                )
                task.percentage = progress.percentage
                # preserve user-driven state changes by updating only `PULLING` tasks
                await DownloadTask.filter(
                    id=task.id, state=DownloadState.PULLING
                ).update(
                    percentage=progress.percentage,
                    completed_size=progress.completed_size,
                    total_size=progress.total_size,
                    dl_speed=progress.dl_speed,
                )

        async def pull_entry(entry: RemoteManifestEntry):
            # reuse verified files and resume safe `.part` files after a restart
            if await find_completed_file(task.dir, entry, job.job_uuid) is not None:
                await update(entry.path, entry.size, 0)
                return
            target = prepare_local_file(task.dir, entry.path, job.job_uuid)
            await update(entry.path, min(target.offset, entry.size), 0)

            async def report(size: int, _total: int, speed: int):
                await update(entry.path, size, speed)

            remote_path = f"{job.remote_dir.rstrip('/')}/{entry.path}"
            try:
                await pull_file(
                    self._file_client(),
                    self._link_client,
                    target,
                    remote_path,
                    entry=entry,
                    progress=report,
                )
            except OpenListClientError as exc:
                if exc.kind in {
                    OpenListErrorKind.RATE_LIMIT,
                    OpenListErrorKind.TRANSIENT,
                }:
                    raise _DeferredPullError(exc) from exc
                raise PullError(_CLIENT_ERROR_KINDS[exc.kind], str(exc)) from exc
            await update(entry.path, entry.size, 0)

        for entry in entries:
            if entry.is_dir:
                prepare_local_directory(task.dir, entry.path)
        files = tuple(entry for entry in entries if not entry.is_dir)
        pending = iter(files)

        async def worker():
            for entry in pending:
                async with self._semaphore:
                    await pull_entry(entry)

        failure: PullError | None = None
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(self._config.pull_concurrency, len(files))):
                    group.create_task(worker())
        except* PullError as caught:
            failure = _first_pull_error(caught)

        if failure is not None:
            raise failure
        # advance only an unchanged `PULLING` task
        # leave final manifest verification to the `VERIFYING` phase
        async with in_transaction() as connection:
            updated = await (
                DownloadTask.filter(id=task.id, state=DownloadState.PULLING)
                .using_db(connection)
                .update(
                    state=DownloadState.VERIFYING,
                    error_msg=None,
                    percentage=100,
                    completed_size=total,
                    total_size=total,
                    dl_speed=0,
                )
            )
            if updated:
                await (
                    OfflineDownloadJob.filter(id=job.id)
                    .using_db(connection)
                    .update(last_error_kind=None)
                )
