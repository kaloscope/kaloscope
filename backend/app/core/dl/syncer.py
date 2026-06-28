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
from tortoise.expressions import F, Q, RawSQL

from app.core.dl.adapter import Adapter, load_config
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
                metadata, gid = _followed_by(result)
                if not metadata:
                    item = result
                elif gid:
                    result = await adapter.call("details", {"id": gid})
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
        if files is None and "details" in adapter.methods:
            result = await adapter.call("details", asdict(unique))
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
        the GID of the actual download task if available, or None otherwise.
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
        replaced = []
        for file in files:
            # render the template with the extracted metadata
            if is_template(repl):
                context = {
                    "title": extract_title(file, True),
                    "year": extract_year(file),
                    "season": extract_season(file),
                    "episode": extract_episode(file),
                }
                repl = render(repl, context=context)
            # apply the replacement pattern
            replaced.append(re.sub(task.sub_pattern, repl, file))

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
                magnet.link,
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
        await _record_history(magnet)

    # update the plan's total_count and last_exec
    await DownloadPlan.filter(id=plan.id).update(
        total_count=plan.total_count + task_count, last_exec=timezone.now()
    )
