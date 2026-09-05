from datetime import datetime
from pathlib import PurePosixPath

from tortoise.transactions import in_transaction

from app.core.dl.driver import DownloadIdentity, DownloadSnapshot, DownloadState
from app.core.dl.openlist.client import OpenListClient
from app.core.dl.openlist.manifest import (
    REFRESH_INTERVAL,
    STABILITY_WINDOW,
    RemoteManifestEntry,
    build_manifest,
    manifest_fingerprint,
    serialize_manifest,
)
from app.core.dl.openlist.puller import (
    PullError,
    delete_local_directory,
    delete_local_file,
    verify_local_manifest,
)
from app.core.dl.openlist.state import state_progress
from app.models.download import OfflineDownloadErrorKind, OfflineDownloadJob


def reconcile_local_manifest(
    job: OfflineDownloadJob, entries: tuple[RemoteManifestEntry, ...]
) -> list[dict[str, object]]:
    """Remove local paths invalidated by a new non-empty remote manifest.

    Args:
        job: The persisted job whose previous manifest owns the local paths.
        entries: The latest non-empty remote manifest entries.

    Returns:
        The serialized latest manifest ready for persistence.

    Raises:
        PullError: If a stale local path cannot be handled safely.
    """
    manifest = serialize_manifest(entries)
    current = {item["path"]: item for item in manifest}
    stale_directories = []
    for item in job.manifest or ():
        current_item = current.get(item["path"])
        if item["is_dir"]:
            if current_item is None or current_item["is_dir"] is not True:
                stale_directories.append(item["path"])
        elif current_item != item:
            # remove only files whose ownership marker still matches
            delete_local_file(job.download.dir, item["path"], job.job_uuid)
    for path in sorted(
        stale_directories,
        key=lambda value: len(PurePosixPath(value).parts),
        reverse=True,
    ):
        delete_local_directory(job.download.dir, path)
    return manifest


async def finalize_job(
    client: OpenListClient,
    job: OfflineDownloadJob,
    now: datetime,
    *,
    cleanup_on_success: bool = False,
) -> DownloadSnapshot:
    """Verify a pulled job and persist its resulting state.

    The remote manifest is rebuilt before checking the local files. A changed
    manifest returns the job to `SETTLING`; otherwise the task advances to
    `COMPLETED` or `ERROR` based on local verification.

    Args:
        client: The OpenList client used to read the remote result.
        job: The persisted offline download job to finalize.
        now: The timestamp used for stability scheduling and completion.
        cleanup_on_success: Whether successful completion requires remote cleanup.

    Returns:
        A snapshot of the persisted download state.
    """
    task = job.download
    files: tuple[str, ...] | None = None
    error = None
    error_kind = None

    # re-read the remote result so files changed during pulling are not accepted
    try:
        entries = await build_manifest(client, job.remote_dir)
    except ValueError:
        state = DownloadState.ERROR
        error = "OpenList remote manifest is invalid"
        error_kind = OfflineDownloadErrorKind.MANIFEST_INVALID
        total = task.total_size
        job.next_poll_at = None
    else:
        fingerprint = manifest_fingerprint(entries)
        expected_size = task.total_size or 0
        manifest_size = sum(entry.size for entry in entries if not entry.is_dir)
        total = manifest_size
        if not entries:
            # retain the pulled manifest when an empty listing is not yet stable
            state = DownloadState.SETTLING
            total = task.total_size
            job.next_poll_at = now + STABILITY_WINDOW
        elif manifest_size < expected_size:
            # retain the pulled manifest while mounted storage is still refreshing
            state = DownloadState.SETTLING
            total = task.total_size
            job.next_poll_at = now + REFRESH_INTERVAL
        elif fingerprint != job.manifest_fingerprint:
            try:
                manifest = reconcile_local_manifest(job, entries)
            except PullError as exc:
                state = DownloadState.ERROR
                error = str(exc)
                error_kind = exc.kind
                job.next_poll_at = None
            else:
                # wait before pulling the changed result
                state = DownloadState.SETTLING
                job.manifest = manifest
                job.manifest_fingerprint = fingerprint
                job.manifest_changed_at = now
                job.next_poll_at = now + STABILITY_WINDOW
        else:
            try:
                files = await verify_local_manifest(task.dir, entries)
            except PullError as exc:
                state = DownloadState.ERROR
                error = str(exc)
                error_kind = exc.kind
            else:
                if files is None:
                    state = DownloadState.ERROR
                    error = "OpenList local verification failed"
                    error_kind = OfflineDownloadErrorKind.VERIFY_FAILED
                else:
                    state = DownloadState.COMPLETED
            job.next_poll_at = None

    percentage = state_progress(state, task.percentage).percentage
    task.state = state
    task.error_msg = error
    task.percentage = percentage
    task.total_size = total
    task.dl_speed = 0
    task.files = list(files) if files is not None else None
    task.completed_at = now if state is DownloadState.COMPLETED else None
    if state is DownloadState.COMPLETED:
        task.completed_size = total
        job.completion_due_at = now
    job.unchanged_count = 0
    job.retry_count = 0
    if state is DownloadState.COMPLETED and cleanup_on_success:
        job.next_poll_at = now
    job.last_error_kind = error_kind

    # keep the task state and its OpenList bookkeeping in one transaction
    async with in_transaction() as connection:
        await task.save(
            update_fields=[
                "state",
                "error_msg",
                "percentage",
                "total_size",
                "completed_size",
                "dl_speed",
                "files",
                "completed_at",
            ],
            using_db=connection,
        )
        await job.save(
            update_fields=[
                "manifest",
                "manifest_fingerprint",
                "manifest_changed_at",
                "next_poll_at",
                "completion_due_at",
                "unchanged_count",
                "retry_count",
                "last_error_kind",
            ],
            using_db=connection,
        )
    return DownloadSnapshot(
        identity=DownloadIdentity.from_task(task),
        state=state,
        error=error,
        percentage=percentage,
        files=files,
    )
