import asyncio
import errno
import os
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import cached_property
from itertools import groupby
from multiprocessing.synchronize import Event, Lock
from pathlib import Path
from typing import cast

from sanic import Sanic
from sanic.log import logger
from tortoise import timezone
from tortoise.expressions import F, RawSQL

from app.core.dl.adapter import Adapter, load_config
from app.core.flow.engine import FlowEngine
from app.core.notifications import Notifications, NotificationTemplate
from app.models.download import (
    Downloader,
    DownloadPlan,
    DownloadState,
    DownloadTask,
    TransferMethod,
)
from app.models.flow import GraphState
from app.models.media import MediaLib
from app.utils.bittorrent import standardize_magnet


@dataclass(frozen=True)
class Unique:
    """The unique identifier of a download task."""

    id: str | None
    hash: str | None
    hash_v2: str | None

    def __eq__(self, other):
        if not isinstance(other, Unique):
            return False
        if not self.hash_v2 and not self.hash and not self.id:
            return not other.hash_v2 and not other.hash and not other.id
        return bool(
            (self.hash_v2 and self.hash_v2 == other.hash_v2)
            or (self.hash and self.hash == other.hash)
            or (self.id and self.id == other.id)
        )

    def __hash__(self):
        return hash(self.hash_v2 or self.hash or self.id or "")

    @staticmethod
    def from_task(task: DownloadTask) -> "Unique":
        """Create a unique identifier from a download task.

        Args:
            task: The download task.

        Returns:
            A unique identifier.
        """
        return Unique(
            id=task.unique_id,
            hash=task.info_hash,
            hash_v2=task.info_hash_v2,
        )


class DLSyncer:
    """The download task synchronizer."""

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
        return self._app.shared_ctx.dl_syncer_lock

    @cached_property
    def _syncer_flag(self) -> Event:
        return self._app.shared_ctx.dl_syncer_flag

    @cached_property
    def _sync_fast(self) -> Event:
        return self._app.shared_ctx.dl_sync_fast

    def accelerate(self):
        """Accelerate the download synchronizer."""
        self._sync_fast.set()

    def decelerate(self):
        """Decelerate the download synchronizer."""
        self._sync_fast.clear()

    async def start(self):
        """Start the download synchronizer."""
        if self._task:
            self._app.add_task(self.interval(), name=self._task)

    async def shutdown(self):
        """Shutdown the download synchronizer."""
        if self._task:
            self._syncer_flag.clear()
            await self._app.cancel_task(self._task)

    async def interval(self):
        """Synchronize the download tasks."""
        slow_mode = 30
        while True:
            now = datetime.now()
            try:
                seconds = (now - self._last_sync_tasks).total_seconds()
                if not self._sync_fast.is_set() and seconds < 2:
                    # do not synchronize too frequently
                    await asyncio.sleep(slow_mode - 1)
                    continue

                # synchronize the download tasks in batch by downloader
                all = await DownloadTask.filter(
                    state__in=[DownloadState.PAUSED, DownloadState.DOWNLOADING]
                )
                for downloader_id, tasks in groupby(all, key=lambda t: t.downloader_id):
                    downloader = await Downloader.get(id=downloader_id)
                    await sync_tasks(downloader, list(tasks))

                # check the download plans every hour
                hours = (now - self._last_check_plans).total_seconds() / 3600
                if hours >= 1:
                    self._last_check_plans = now
                    self._app.add_task(check_download_plans())

                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.error("Failed to synchronize the download tasks!", exc_info=True)
                await asyncio.sleep(1)
            finally:
                self._last_sync_tasks = now


async def sync_tasks(downloader: Downloader, tasks: list[DownloadTask]):
    """Synchronize the download tasks with the specified downloader.

    Args:
        downloader: The downloader.
        tasks: The download tasks.
    """
    adapter = load_config(downloader.config)
    variables = {
        "ids": [t.unique_id for t in tasks if t.unique_id],
        "hashes": [t.info_hash for t in tasks if t.info_hash],
        "hashes_v2": [t.info_hash_v2 for t in tasks if t.info_hash_v2],
    }
    result = await adapter.call("list", variables)
    if not isinstance(result, list):
        return

    # construct a dict of unique id to item
    matched: dict[Unique, dict] = {}
    for item in cast(list, result):
        if isinstance(item, dict):
            unique = Unique(
                id=str(item.get("unique_id", "")),
                hash=str(item.get("info_hash", "")),
                hash_v2=str(item.get("info_hash_v2", "")),
            )
            matched[unique] = item

    # update the download tasks
    for task in tasks:
        unique = Unique.from_task(task)
        item: dict | None = matched.get(unique)

        # try to get the details if the details method is supported
        if item is None and "details" in adapter.methods:
            result = await adapter.call("details", asdict(unique))
            if isinstance(result, dict):
                item = result

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

        # update the state to `ERROR` if the error message is not empty
        error_msg = str(item.get("error_msg", ""))
        if error_msg:
            state = DownloadState.ERROR
            await Notifications.send(
                NotificationTemplate.DOWNLOAD_FAILED, name=task.name, error=error_msg
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
            await Notifications.send(
                NotificationTemplate.DOWNLOAD_COMPLETED, name=task.name
            )

        # get the files if the details method is supported
        files = item.get("files")
        if files is None and "details" in adapter.methods:
            result = await adapter.call("details", asdict(unique))
            if isinstance(result, dict):
                files = result.get("files")
        if isinstance(files, list):
            files = [str(f).removeprefix(f"{task.dir}/") for f in files if f]

        # update the download task
        await DownloadTask.filter(id=task.id).update(
            name=str(item.get("name") or task.name),
            unique_id=str(item.get("unique_id") or task.unique_id),
            raw_state=str(item.get("raw_state") or task.raw_state),
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


async def transfer_files(task: DownloadTask, files: list[str] | None):
    """Transfer completed download files to the media library directory.

    Args:
        task: The download task.
        files: The relative file paths within the download directory.
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
        replaced = [re.sub(task.sub_pattern, repl, f) for f in files]
        # discard replacement if duplicate file names arise
        if len(set(replaced)) == len(replaced):
            new_files = replaced

    for name, new_name in zip(files, new_files, strict=True):
        src = src_dir / name
        if not src.exists():
            continue
        dst = dst_dir / new_name
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

    # load the adapters of the associated downloaders
    adapters: dict[int, Adapter] = {}
    for downloader_id in {plan.downloader_id for plan in plans}:
        downloader = await Downloader.get(id=downloader_id)
        adapter = load_config(downloader.config)
        if not adapter.methods.get("version") or (await adapter.version()):
            adapters[downloader_id] = adapter

    # execute each eligible plan with the corresponding adapter
    for plan in plans:
        if plan.downloader_id not in adapters:
            continue
        adapter = adapters[plan.downloader_id]
        try:
            await execute_download_plan(plan, adapter)
        except Exception:
            logger.error("Failed to execute download plan: %s", plan.id, exc_info=True)


async def execute_download_plan(plan: DownloadPlan, adapter: Adapter | None = None):
    """Execute a single download plan.

    Args:
        plan: The download plan.
        adapter: The adapter of the associated downloader.
    """
    # load the adapter if not provided
    if adapter is None:
        downloader = await Downloader.get(id=plan.downloader_id)
        adapter = load_config(downloader.config)

    # prepare the boot parameters for graph execution
    engine: FlowEngine = Sanic.get_app().ctx.flow_engine
    bootparams = {
        "$start": "search_start",
        "page_num": 1,
        "page_size": plan.batch_limit,
        "keyword": plan.keyword,
        **(plan.filters or {}),
    }

    # execute the graph workflow with up to 3 retries
    items = []
    for attempt in range(3):
        try:
            result = await engine.execute(graph_id=plan.graph_id, bootparams=bootparams)
            if isinstance(result, dict) and isinstance(result.get("items"), list):
                items = result["items"]
                break
            raise ValueError("invalid graph execution result")
        except Exception:
            if attempt == 2:
                logger.error(
                    "Failed to execute graph for plan %s after 3 attempts!",
                    plan.id,
                    exc_info=True,
                )
                return
            await asyncio.sleep(2**attempt * 5)

    # extract valid magnet links from the graph execution result
    magnet_links = []
    for item in items:
        if isinstance(item, dict) and isinstance((link := item.get("link")), str):
            # check if the link is a valid magnet/hash
            magnet = standardize_magnet(link)
            if magnet is None:
                continue
            # check if the magnet link already exists in the download tasks
            hash = magnet.info_hash
            if hash and await DownloadTask.filter(info_hash=hash).exists():
                continue
            hash_v2 = magnet.info_hash_v2
            if hash_v2 and await DownloadTask.filter(info_hash_v2=hash_v2).exists():
                continue
            magnet_links.append(magnet)

    # add download tasks for each valid magnet link
    task_count = 0
    for magnet in magnet_links:
        # check if the total limit of the plan has been reached
        if plan.total_limit and plan.total_count + task_count >= plan.total_limit:
            break

        # add the download task via adapter
        try:
            result = await adapter.call(
                "add_link",
                {
                    "dir": plan.dir,
                    "link": magnet.link,
                    "pause": False,
                },
            )
        except Exception:
            logger.error(
                "Failed to add download task for plan %s, link: %s",
                plan.id,
                link,
                exc_info=True,
            )
            continue

        unique_id = result.get("unique_id") if isinstance(result, dict) else None
        await DownloadTask.create(
            downloader_id=plan.downloader_id,
            dir=plan.dir,
            name=magnet.info_hash or magnet.info_hash_v2,
            unique_id=unique_id,
            info_hash=magnet.info_hash,
            info_hash_v2=magnet.info_hash_v2,
            magnet_link=magnet.link,
            state=DownloadState.DOWNLOADING,
            transfer_lib_id=plan.transfer_lib_id,
            transfer_method=plan.transfer_method,
            sub_pattern=plan.sub_pattern,
            sub_repl=plan.sub_repl,
        )
        task_count += 1

    # update the plan's total_count and last_exec
    await DownloadPlan.filter(id=plan.id).update(
        total_count=plan.total_count + task_count, last_exec=timezone.now()
    )
