import asyncio
import errno
import hashlib
import os
import re
import shutil
from datetime import datetime, timedelta
from functools import cached_property
from multiprocessing.managers import DictProxy
from multiprocessing.synchronize import Event, Lock
from pathlib import Path
from typing import cast

from filelock import FileLock
from sanic import Sanic
from sanic.log import logger
from tortoise import timezone
from tortoise.expressions import F, Q, RawSQL
from tortoise.transactions import in_transaction

from app.core.config import KaloscopeConfig
from app.core.dl.config import decrypt_config, load_driver
from app.core.dl.driver import (
    DownloadAction,
    DownloaderDriver,
    DownloadIdentity,
    DownloadRequest,
)
from app.core.dl.openlist import OpenListDriver
from app.core.dl.openlist.puller import transfer_local_file
from app.core.dl.rpc import RpcClient, RpcDriver
from app.core.flow.engine import FlowEngine
from app.core.notifications import Notifications, NotificationTemplate
from app.core.renderer import is_template, render
from app.models.base import TortoiseModel
from app.models.download import (
    Downloader,
    DownloadPlan,
    DownloadPlanHistory,
    DownloadState,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
    TransferMethod,
)
from app.models.flow import GraphState
from app.models.media import MediaLib
from app.utils.bittorrent import MagnetLink, standardize_magnet
from app.utils.extractor import (
    extract_episode,
    extract_season,
    extract_title,
    extract_year,
)

type _DownloadCommand = tuple[DownloadAction, DownloadState, bool]


def submission_lock(job_uuid: str) -> FileLock:
    """Create a native file lock for submission and recovery.

    The operating system releases the lock when the submitting process exits.
    All application workers use the same job-scoped path in `workspace/temp`.

    Args:
        job_uuid: The offline job UUID shared by submission and recovery.

    Returns:
        A nonblocking native file lock for the job.
    """
    name = hashlib.sha256(job_uuid.encode()).hexdigest()
    return FileLock(
        Path(KaloscopeConfig.get_workspace("temp")) / f"openlist_{name}.lock",
        blocking=False,
        fallback_to_soft=False,
    )


class DLSyncer:
    """Synchronize download tasks from the process owning shared state."""

    _DL_SYNCER = "dl_syncer"

    def __init__(self, app: Sanic):
        """Initialize the download task synchronizer.

        Args:
            app: The Sanic application instance.
        """
        self._app = app
        self._task = None
        self._last_sync_tasks = datetime.now()
        self._last_check_plans = datetime.now()
        self._drivers: dict[int, tuple[str, DownloaderDriver]] = {}
        # ensure that only one instance is running
        if self._syncer_lock.acquire(block=False):
            try:
                if not self._syncer_flag.is_set():
                    self._task = self._DL_SYNCER
                    self._syncer_flag.set()
            finally:
                self._syncer_lock.release()

    @cached_property
    def _syncer_lock(self) -> Lock:
        """Get the shared synchronizer ownership lock.

        Returns:
            The process-shared ownership lock.
        """
        return self._app.shared_ctx.dl_syncer_lock

    @cached_property
    def _syncer_flag(self) -> Event:
        """Get the shared synchronizer ownership flag.

        Returns:
            The process-shared ownership flag.
        """
        return self._app.shared_ctx.dl_syncer_flag

    @cached_property
    def _sync_fast(self) -> Event:
        """Get the shared fast-synchronization flag.

        Returns:
            The process-shared fast-mode flag.
        """
        return self._app.shared_ctx.dl_sync_fast

    @cached_property
    def _task_actions(self) -> DictProxy[int, _DownloadCommand]:
        """Get the shared pending task-action mapping.

        Returns:
            The process-shared commands keyed by local task ID.
        """
        return self._app.shared_ctx.dl_task_actions

    def accelerate(self):
        """Accelerate the download synchronizer."""
        self._sync_fast.set()

    def decelerate(self):
        """Decelerate the download synchronizer."""
        self._sync_fast.clear()

    def publish(
        self,
        task_id: int,
        action: DownloadAction,
        state: DownloadState,
        *,
        local: bool = False,
    ):
        """Publish a task action for the synchronizer owner.

        Args:
            task_id: The local download task ID.
            action: The requested driver action.
            state: The task state observed by the publisher.
            local: Whether task deletion includes local files.
        """
        self._task_actions[task_id] = (action, state, local)

    async def start(self):
        """Start the download synchronizer."""
        if self._task:
            self._app.add_task(self.interval(), name=self._task)

    async def shutdown(self):
        """Shutdown the download synchronizer."""
        try:
            if self._task:
                await self._app.cancel_task(self._task)
        finally:
            try:
                await self._close_drivers()
            finally:
                if self._task:
                    self._syncer_flag.clear()

    async def _driver_for(self, downloader: Downloader) -> DownloaderDriver:
        """Get a driver cached for the current encrypted configuration.

        Args:
            downloader: The persisted downloader definition.

        Returns:
            The reusable driver for `downloader`.
        """
        cached = self._drivers.get(downloader.id)
        if cached is not None and cached[0] == downloader.config:
            return cached[1]
        if cached is not None:
            await self._close_drivers({downloader.id})
        driver = load_driver(decrypt_config(downloader.config))
        self._drivers[downloader.id] = (downloader.config, driver)
        return driver

    async def _close_drivers(self, downloader_ids: set[int] | None = None):
        """Close cached drivers that are no longer needed.

        Args:
            downloader_ids: The downloader IDs to close, or `None` for all drivers.
        """
        ids = set(self._drivers) if downloader_ids is None else downloader_ids
        drivers = [
            self._drivers.pop(downloader_id)[1]
            for downloader_id in ids
            if downloader_id in self._drivers
        ]
        results = await asyncio.gather(
            *(driver.close() for driver in drivers), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            logger.warning("Failed to close one or more downloader drivers")

    async def _consume_actions(self) -> bool:
        """Consume task actions published by request-handling processes.

        Returns:
            `True` when at least one valid action was processed.
        """
        processed = False
        commands = {
            task_id: self._task_actions.get(task_id)
            for task_id in list(self._task_actions.keys())
        }
        pending = await OfflineDownloadJob.filter(
            delete_due_at__lte=timezone.now()
        ).select_related("download")
        pending_ids = {job.download_id for job in pending}
        for job in pending:
            commands[job.download_id] = (
                DownloadAction.DELETE,
                job.download.state,
                job.delete_local,
            )
        for task_id, command in commands.items():
            if command is None:
                continue
            published = self._task_actions.get(task_id)
            try:
                action, expected_state, local = command
                task = await (
                    DownloadTask.filter(id=task_id).select_related("downloader").first()
                )
                # reject commands whose publisher observed a stale task state
                if task is None:
                    continue
                if task_id in pending_ids:
                    expected_state = task.state
                if task.state is not expected_state:
                    continue
                driver = await self._driver_for(task.downloader)
                identity = DownloadIdentity.from_task(task)
                state = None
                if action is DownloadAction.PAUSE:
                    state = await driver.pause(identity)
                elif action is DownloadAction.RESUME:
                    state = await driver.resume(identity)
                elif action is DownloadAction.CANCEL:
                    await driver.cancel(identity)
                elif action is DownloadAction.RETRY:
                    state = await driver.retry(identity)
                elif action is DownloadAction.DELETE:
                    if task.state is DownloadState.SUBMITTING:
                        # retain the durable request until submission or recovery ends
                        processed = True
                        continue
                    capabilities = await driver.capabilities(identity, task.state)
                    if DownloadAction.DELETE not in capabilities:
                        continue
                    delete_state = expected_state
                    if expected_state is DownloadState.PULLING:
                        delete_state = DownloadState.PAUSED
                        updated = await DownloadTask.filter(
                            id=task_id, state=expected_state
                        ).update(state=delete_state)
                        if not updated:
                            continue
                    if DownloadAction.CANCEL in capabilities:
                        await driver.cancel(identity)
                    await driver.delete(identity, local=local)
                    await DownloadTask.filter(id=task_id, state=delete_state).delete()
                    processed = True
                    continue
                processed = True
                if state is not None:
                    values: dict[str, object] = {"state": state}
                    if action in {
                        DownloadAction.PAUSE,
                        DownloadAction.RESUME,
                        DownloadAction.RETRY,
                    }:
                        values["dl_speed"] = 0
                    if action is DownloadAction.RETRY:
                        values["error_msg"] = None
                        if state is DownloadState.REMOTE:
                            values["raw_state"] = None
                            values["percentage"] = 0
                        async with in_transaction() as connection:
                            updated = await (
                                DownloadTask.filter(id=task_id, state=expected_state)
                                .using_db(connection)
                                .update(**values)
                            )
                            if updated:
                                await (
                                    OfflineDownloadJob.filter(download_id=task_id)
                                    .using_db(connection)
                                    .update(
                                        next_poll_at=None,
                                        retry_count=0,
                                        last_error_kind=None,
                                    )
                                )
                    else:
                        await DownloadTask.filter(
                            id=task_id, state=expected_state
                        ).update(**values)
            except Exception:
                if task_id in pending_ids:
                    await OfflineDownloadJob.filter(download_id=task_id).update(
                        delete_due_at=timezone.now() + timedelta(seconds=30)
                    )
                logger.error("Failed to process download task action", exc_info=True)
            finally:
                if self._task_actions.get(task_id) == published:
                    self._task_actions.pop(task_id, None)
        return processed

    async def interval(self):
        """Synchronize the download tasks."""
        slow_mode = 30
        while True:
            now = datetime.now()
            try:
                processed_action = await self._consume_actions()
                seconds = (now - self._last_sync_tasks).total_seconds()
                if (
                    not processed_action
                    and not self._sync_fast.is_set()
                    and seconds < slow_mode
                ):
                    await asyncio.sleep(1)
                    continue

                # synchronize the download tasks in batch by downloader
                active_states = [
                    DownloadState.PAUSED,
                    DownloadState.DOWNLOADING,
                    DownloadState.SUBMITTING,
                    DownloadState.SUBMIT_UNKNOWN,
                    DownloadState.REMOTE,
                    DownloadState.SETTLING,
                    DownloadState.PULLING,
                    DownloadState.VERIFYING,
                ]
                cleanup_due = Q(
                    state=DownloadState.COMPLETED,
                    offline_job__next_poll_at__lte=timezone.now(),
                ) | Q(
                    state=DownloadState.COMPLETED,
                    offline_job__last_error_kind=(
                        OfflineDownloadErrorKind.CLEANUP_FAILED
                    ),
                    offline_job__next_poll_at=None,
                )
                all = await DownloadTask.filter(
                    Q(state__in=active_states)
                    | cleanup_due
                    | Q(
                        state=DownloadState.COMPLETED,
                        offline_job__completion_due_at__lte=timezone.now(),
                    )
                )
                grouped: dict[int, list[DownloadTask]] = {}
                for task in all:
                    grouped.setdefault(task.downloader_id, []).append(task)
                for downloader_id, tasks in grouped.items():
                    try:
                        downloader = await Downloader.get(id=downloader_id)
                        driver = await self._driver_for(downloader)
                        await sync_tasks(tasks, driver)
                    except Exception:
                        logger.error(
                            "Failed to synchronize downloader: %s",
                            downloader_id,
                            exc_info=True,
                        )
                await self._close_drivers(set(self._drivers) - set(grouped))

                # check the download plans every hour
                hours = (now - self._last_check_plans).total_seconds() / 3600
                if hours >= 1:
                    self._last_check_plans = now
                    self._app.add_task(check_download_plans())

                self._last_sync_tasks = now
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Failed to synchronize the download tasks!", exc_info=True)
                self._last_sync_tasks = now
                await asyncio.sleep(1)


async def sync_tasks(
    tasks: list[DownloadTask],
    driver: DownloaderDriver,
):
    """Synchronize the download tasks with the specified downloader.

    Args:
        tasks: The download tasks.
        driver: The downloader driver.
    """
    if isinstance(driver, RpcDriver):
        await _sync_rpc_tasks(driver, tasks)
    else:
        identities = tuple(DownloadIdentity.from_task(task) for task in tasks)
        snapshots = await driver.sync(identities)
        indexed = {task.id: task for task in tasks}
        for snapshot in snapshots:
            task = indexed.get(snapshot.identity.task_id)
            if task is None:
                continue
            if snapshot.state is DownloadState.ERROR:
                await Notifications.send(
                    NotificationTemplate.DOWNLOAD_FAILED,
                    name=task.name,
                    error=snapshot.error or task.error_msg or "",
                )
                continue
            if snapshot.state is not DownloadState.COMPLETED:
                continue
            if isinstance(driver, OpenListDriver):
                continue
            await Notifications.send(
                NotificationTemplate.DOWNLOAD_COMPLETED, name=task.name
            )
            files = list(snapshot.files) if snapshot.files is not None else task.files
            try:
                await transfer_files(task, files)
            except Exception:
                logger.error(
                    "Failed to transfer files for task: %s", task.id, exc_info=True
                )
        if isinstance(driver, OpenListDriver):
            await _complete_openlist_tasks([task.id for task in tasks])


async def _complete_openlist_tasks(task_ids: list[int]):
    """Consume durable local completion work independently of remote snapshots.

    Args:
        task_ids: The tasks selected for this synchronization cycle.
    """
    jobs = await OfflineDownloadJob.filter(
        download_id__in=task_ids,
        download__state=DownloadState.COMPLETED,
        completion_due_at__lte=timezone.now(),
        delete_due_at=None,
    ).select_related("download")
    for job in jobs:
        task = job.download
        try:
            await transfer_files(task, task.files, job_id=job.job_uuid)
            # commit the notification and acknowledgement together after file transfer
            async with in_transaction() as connection:
                updated = await (
                    OfflineDownloadJob.filter(
                        id=job.id, completion_due_at=job.completion_due_at
                    )
                    .using_db(connection)
                    .update(completion_due_at=None)
                )
                if updated:
                    await (
                        DownloadTask.filter(id=task.id)
                        .using_db(connection)
                        .update(error_msg=None)
                    )
                    await Notifications.send(
                        NotificationTemplate.DOWNLOAD_COMPLETED, name=task.name
                    )
        except Exception:
            async with in_transaction() as connection:
                await (
                    OfflineDownloadJob.filter(id=job.id)
                    .using_db(connection)
                    .update(completion_due_at=timezone.now() + timedelta(seconds=30))
                )
                await (
                    DownloadTask.filter(id=task.id)
                    .using_db(connection)
                    .update(error_msg="OpenList completion processing failed")
                )
            logger.error("Failed to complete OpenList task: %s", task.id, exc_info=True)


async def _sync_rpc_tasks(driver: RpcDriver, tasks: list[DownloadTask]):
    """Synchronize tasks through a local HTTP/RPC downloader.

    Args:
        driver: The configured RPC downloader driver.
        tasks: The local tasks assigned to `driver`.
    """
    client = driver.client
    variables = {
        "ids": [t.unique_id for t in tasks if t.unique_id],
        "hashes": [t.info_hash for t in tasks if t.info_hash],
        "hashes_v2": [t.info_hash_v2 for t in tasks if t.info_hash_v2],
    }
    result = await client.call("list", variables)
    if not isinstance(result, list):
        return

    # map each remote item by every available identity
    matched: dict[tuple[str, str], dict] = {}
    for item in cast(list, result):
        if isinstance(item, dict):
            identity = DownloadIdentity(
                remote_id=str(item.get("unique_id") or "") or None,
                info_hash=str(item.get("info_hash") or "") or None,
                info_hash_v2=str(item.get("info_hash_v2") or "") or None,
            )
            for key in identity.match_keys:
                matched[key] = item

    # update the download tasks
    for task in tasks:
        identity = DownloadIdentity.from_task(task)
        item = next(
            (matched[key] for key in identity.match_keys if key in matched),
            None,
        )

        # try to get the details if the details method is supported
        if item is None and "details" in driver.config.methods:
            item = await resolve_details(client, identity.rpc_variables)

        # continue the loop if the task is not matched
        if not item:
            await DownloadTask.filter(id=task.id).update(
                up_speed=0 if task.up_speed is not None else None,
                dl_speed=0 if task.dl_speed is not None else None,
            )
            continue

        # update the state to `DOWNLOADING` if the download speed is greater than 0
        state = task.state
        up_speed = int(item.get("up_speed", task.up_speed) or 0)
        dl_speed = int(item.get("dl_speed", task.dl_speed) or 0)
        if state == DownloadState.PAUSED and task.dl_speed == 0 and dl_speed > 0:
            state = DownloadState.DOWNLOADING

        # update the state to `ERROR` if the raw state indicates an error
        name = str(item.get("name") or task.name)
        raw_state = str(item.get("raw_state") or task.raw_state)
        error_msg = str(item.get("error_msg") or "")
        if error_msg or raw_state.lower() == "error":
            state = DownloadState.ERROR
            up_speed = 0
            dl_speed = 0
            await Notifications.send(
                NotificationTemplate.DOWNLOAD_FAILED, name=name, error=error_msg
            )

        # update the state to `COMPLETED` if the percentage has reached 100
        percentage = float(item.get("percentage", 0.0))
        total_size = int(item.get("total_size", task.total_size) or 0)
        completed_size = int(item.get("completed_size", task.completed_size) or 0)
        completed_at = None
        if not percentage:
            percentage = completed_size / total_size * 100 if total_size else 0.0
        if percentage >= 100:
            dl_speed = 0
            completed_at = timezone.now()
            state = DownloadState.COMPLETED
            await Notifications.send(NotificationTemplate.DOWNLOAD_COMPLETED, name=name)

        # get the files if the details method is supported
        files = item.get("files")
        if files is None and "details" in driver.config.methods:
            result = await client.call("details", identity.rpc_variables)
            if isinstance(result, dict):
                files = result.get("files")
        if isinstance(files, list):
            files = [str(f).removeprefix(f"{task.dir}/") for f in files if f]

        # update the download task
        unique_id = str(item.get("unique_id") or task.unique_id or "")
        await DownloadTask.filter(id=task.id).update(
            name=name,
            unique_id=unique_id,
            raw_state=raw_state,
            files=files,
            state=state,
            error_msg=error_msg,
            up_speed=up_speed,
            dl_speed=dl_speed,
            percentage=percentage,
            total_size=total_size,
            completed_size=completed_size,
            completed_at=completed_at,
        )

        # transfer files to media library after completion
        if state == DownloadState.COMPLETED:
            try:
                await transfer_files(
                    task, files if isinstance(files, list) else task.files
                )
            except Exception:
                logger.error(
                    "Failed to transfer files for task: %s",
                    task.id,
                    exc_info=True,
                )


async def resolve_details(client: RpcClient, unique: dict) -> dict | None:
    """Resolve the details of a download task.

    Args:
        client: The downloader RPC client.
        unique: The unique identifier of the download task.

    Returns:
        The resolved task details, or `None` if they are unavailable.
    """
    result = await client.call("details", unique)
    if not isinstance(result, dict):
        return None

    metadata, gid = _followed_by(result)
    if not metadata:
        return result
    if gid:
        result = await client.call("details", {"id": gid})
        if isinstance(result, dict):
            return result
    return None


def _followed_by(result: dict) -> tuple[bool, str | None]:
    """Extract the followed-by GID from an aria2 task result.

    When a magnet link or info-hash is added to aria2, it first creates a
    metadata-fetching task whose `files` contains a single `[METADATA]`
    placeholder entry. Once the metadata is resolved, aria2 creates the actual
    download task and exposes its GID via the `followedBy` field. This
    function detects that metadata state and returns the followed GID so the
    syncer can track the real download task instead of the ephemeral metadata
    task.

    Args:
        result: A single task result object returned by aria2.

    Returns:
        A tuple of (is_metadata, followed_gid). `is_metadata` is True if the
        result is detected as a metadata-fetching task, and `followed_gid` is
        the GID of the actual download task if available, or `None` otherwise.
    """
    metadata = False
    if (
        isinstance(files := result.get("files"), list)
        and len(files) == 1
        and isinstance(files[0], str)
        and files[0] == "[METADATA]"
    ):
        metadata = True

    gid = None
    if metadata and (
        isinstance(ids := result.get("followed_by"), list)
        and len(ids) == 1
        and isinstance(ids[0], str)
    ):
        gid = ids[0]

    logger.debug("Checked for followedBy: metadata=%s, gid=%s", metadata, gid)
    return (metadata, gid)


async def transfer_files(
    task: DownloadTask, files: list[str] | None, *, job_id: str | None = None
):
    """Transfer completed download files to the media library directory.

    Args:
        task: The download task.
        files: The relative file paths within the download directory.
        job_id: The offline job UUID enabling recoverable copy publication.
    """
    if not task.transfer_lib_id or not files:
        return
    lib = await MediaLib.get_or_none(id=task.transfer_lib_id)
    if not lib:
        return

    src_dir = Path(task.dir)
    dst_dir = Path(lib.dir)
    if src_dir == dst_dir and not task.sub_pattern:
        # no need to transfer if the source and destination are the same
        # and no file name substitution is needed
        return

    # apply file name substitution if sub_pattern is specified
    new_files = files
    if task.sub_pattern:
        repl = task.sub_repl or ""
        replaced = []
        for file in files:
            # render the template with the extracted metadata
            if is_template(repl):
                stem = Path(file).stem
                context = {
                    "title": extract_title(stem),
                    "year": extract_year(stem),
                    "season": extract_season(stem),
                    "episode": extract_episode(stem),
                }
                repl = render(repl, context=context)
            # apply the replacement pattern
            replaced.append(re.sub(task.sub_pattern, repl, file))

        # discard replacement if duplicate file names arise
        if len(set(replaced)) == len(replaced):
            new_files = replaced

    for name, new_name in zip(files, new_files, strict=True):
        src = src_dir / name
        dst = dst_dir / new_name
        if job_id is not None and task.transfer_method in {
            TransferMethod.COPY,
            TransferMethod.MOVE,
        }:
            transfer_local_file(
                src, dst, job_id, move=task.transfer_method is TransferMethod.MOVE
            )
            continue
        if not src.exists():
            continue
        if dst.exists():
            continue

        # create parent directory if it doesn't exist
        dst.parent.mkdir(parents=True, exist_ok=True)

        if task.transfer_method == TransferMethod.HARDLINK:
            try:
                os.link(src, dst)
            except OSError as e:
                # fallback to symlink if hard link fails due to cross-device link error
                if e.errno == errno.EXDEV:
                    os.symlink(src, dst)
                else:
                    raise
        elif task.transfer_method == TransferMethod.SYMLINK:
            os.symlink(src, dst)
        elif task.transfer_method == TransferMethod.MOVE:
            shutil.move(src, dst)
        elif task.transfer_method == TransferMethod.COPY:
            shutil.copy2(src, dst)


async def check_download_plans():
    """Check the download plans and add new download tasks if needed."""

    # get all plans whose graph is published and interval has elapsed
    now = f"julianday('{timezone.now().isoformat(sep=' ')}')"
    last = "julianday(IFNULL(last_exec, download_plan.created_at))"
    plans = await DownloadPlan.annotate(
        elapsed_hours=RawSQL(f"({now} - {last}) * 24")
    ).filter(
        elapsed_hours__gte=F("interval_num"),
        graph__state__not=GraphState.DRAFT,
    )
    plans = [p for p in plans if not p.inactive()]
    if not plans:
        return

    # load the drivers of the associated downloaders
    drivers: dict[int, DownloaderDriver] = {}
    for downloader_id in {plan.downloader_id for plan in plans}:
        try:
            downloader = await Downloader.get(id=downloader_id)
            driver = load_driver(decrypt_config(downloader.config))
            if not driver.supports_version or (await driver.version()):
                drivers[downloader_id] = driver
        except Exception:
            logger.error(
                "Failed to probe download plan driver: %s",
                downloader_id,
                exc_info=True,
            )

    # execute each eligible plan with the corresponding driver
    for plan in plans:
        if plan.downloader_id not in drivers:
            continue
        driver = drivers[plan.downloader_id]
        try:
            await execute_download_plan(plan, driver)
        except Exception:
            logger.error("Failed to execute download plan: %s", plan.id, exc_info=True)


async def execute_download_plan(
    plan: DownloadPlan, driver: DownloaderDriver | None = None
):
    """Execute a single download plan.

    Args:
        plan: The download plan.
        driver: The driver of the associated downloader.
    """
    # load the driver if not provided
    if driver is None:
        downloader = await Downloader.get(id=plan.downloader_id)
        driver = load_driver(decrypt_config(downloader.config))

    # import locally because `DownloadPlanService` imports this function
    from app.services.download import DownloadTaskService

    # get the flow engine instance
    engine: FlowEngine = Sanic.get_app().ctx.flow_engine
    page_num = 1
    seen_links: set[str] = set()

    async def _record_history(magnet: MagnetLink):
        """Record the magnet link in the download plan history."""
        filter = {"plan_id": plan.id}
        if magnet.info_hash is not None:
            filter["info_hash"] = magnet.info_hash
        if magnet.info_hash_v2 is not None:
            filter["info_hash_v2"] = magnet.info_hash_v2
        await DownloadPlanHistory.get_or_create(
            defaults={
                "info_hash": magnet.info_hash,
                "info_hash_v2": magnet.info_hash_v2,
            },
            **filter,
        )

    async def _extract_hashes(
        model: type[TortoiseModel],
        magnets: list[MagnetLink],
        *args: Q,
    ) -> tuple[set[str], set[str]]:
        """Query matching records by magnet hashes and return both hash sets."""
        hashes = [m.info_hash for m in magnets if m.info_hash]
        hashes_v2 = [m.info_hash_v2 for m in magnets if m.info_hash_v2]
        if not hashes and not hashes_v2:
            return set(), set()

        filter = Q()
        if hashes:
            filter |= Q(info_hash__in=hashes)
        if hashes_v2:
            filter |= Q(info_hash_v2__in=hashes_v2)

        records = await model.filter(filter, *args).only("info_hash", "info_hash_v2")
        return (
            set(v1 for r in records if (v1 := getattr(r, "info_hash", None))),
            set(v2 for r in records if (v2 := getattr(r, "info_hash_v2", None))),
        )

    async def _magnet_links() -> list[MagnetLink] | None:
        """Get the magnet links from the graph execution result of the download plan."""
        # prepare the boot parameters for graph execution
        bootparams = {
            "$start": "search_start",
            "page_num": page_num,
            "page_size": plan.batch_limit,
            "keyword": plan.keyword,
            **(plan.filters or {}),
        }

        # execute the graph workflow with up to 3 retries
        items = []
        retries = 3
        for attempt in range(retries):
            try:
                result = await engine.execute(
                    graph_id=plan.graph_id, bootparams=bootparams
                )
                if isinstance(result, dict) and isinstance(result.get("items"), list):
                    items = result["items"]
                    if not items:
                        return None
                    break
                raise ValueError("invalid graph execution result")
            except Exception:
                if attempt == retries - 1:
                    logger.error(
                        "Failed to execute graph for plan %s after %d attempts",
                        plan.id,
                        retries,
                        exc_info=True,
                    )
                    return None
                await asyncio.sleep(2**attempt * 5)

        # detect non-paginated API by checking for duplicate links on page > 1
        links = list(
            dict.fromkeys(
                item["link"]
                for item in items
                if isinstance(item, dict) and isinstance(item.get("link"), str)
            )
        )
        if page_num > 1 and set(links).issubset(seen_links):
            return None

        # extract valid magnet links from the graph execution result
        magnets: list[MagnetLink] = []
        for link in links:
            # skip if the link has been seen in previous pages
            if link in seen_links:
                continue
            seen_links.add(link)
            # check if the link is a valid magnet/hash
            magnet = await standardize_magnet(link)
            if magnet is None:
                continue
            magnets.append(magnet)

        if not magnets:
            return []

        # extract the existing hashes in download plan histories
        hist_hashes, hist_hashes_v2 = await _extract_hashes(
            DownloadPlanHistory,
            magnets,
            Q(plan_id=plan.id),
        )

        missing = [
            m
            for m in magnets
            if (
                (not m.info_hash or m.info_hash not in hist_hashes)
                and (not m.info_hash_v2 or m.info_hash_v2 not in hist_hashes_v2)
            )
        ]

        # extract the existing hashes in download tasks
        task_hashes: set[str] = set()
        task_hashes_v2: set[str] = set()
        if missing:
            task_hashes, task_hashes_v2 = await _extract_hashes(DownloadTask, missing)

        # filter out the magnets that already exist in histories or tasks
        result = []
        for magnet in magnets:
            hist_exists = bool(
                magnet.info_hash and magnet.info_hash in hist_hashes
            ) or bool(magnet.info_hash_v2 and magnet.info_hash_v2 in hist_hashes_v2)
            if hist_exists:
                continue

            task_exists = bool(
                magnet.info_hash and magnet.info_hash in task_hashes
            ) or bool(magnet.info_hash_v2 and magnet.info_hash_v2 in task_hashes_v2)
            if task_exists:
                await _record_history(magnet)
                if magnet.info_hash:
                    task_hashes.add(magnet.info_hash)
                if magnet.info_hash_v2:
                    task_hashes_v2.add(magnet.info_hash_v2)
                continue

            result.append(magnet)
            if len(result) >= plan.batch_limit:
                break

        return result

    # get the magnet links in batch until the batch limit is reached
    magnet_links: list[MagnetLink] = []
    while page_num <= 1000:  # prevent infinite loop
        try:
            links = await _magnet_links()
            if links is None:
                break
            magnet_links.extend(links)
            if len(magnet_links) >= plan.batch_limit:
                magnet_links = magnet_links[: plan.batch_limit]
                break
            page_num += 1
        except Exception:
            logger.error(
                "Failed to get magnet links for plan %s on page %d",
                plan.id,
                page_num,
                exc_info=True,
            )
            break

    # add download tasks for each valid magnet link
    task_count = 0
    for magnet in magnet_links:
        # check if the total limit of the plan has been reached
        if plan.total_limit and plan.total_count + task_count >= plan.total_limit:
            break

        # add the download task via driver
        try:
            request = DownloadRequest(
                directory=plan.dir,
                identity=DownloadIdentity(
                    info_hash=magnet.info_hash,
                    info_hash_v2=magnet.info_hash_v2,
                ),
                link=magnet.link,
                transfer_library_id=plan.transfer_lib_id,
                transfer_method=plan.transfer_method,
                sub_pattern=plan.sub_pattern,
                sub_repl=plan.sub_repl,
            )
            await DownloadTaskService.add_request(
                plan.downloader_id,
                driver,
                request,
            )
        except Exception:
            logger.error(
                "Failed to add download task for plan %s, hashes: v1=%s, v2=%s",
                plan.id,
                magnet.info_hash,
                magnet.info_hash_v2,
                exc_info=True,
            )
            continue

        task_count += 1
        await _record_history(magnet)

    # update the plan's total_count and last_exec
    await DownloadPlan.filter(id=plan.id).update(
        total_count=plan.total_count + task_count, last_exec=timezone.now()
    )
