import shutil
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import aiofiles
from tortoise import timezone
from tortoise.expressions import Q
from tortoise.functions import Count, Sum
from tortoise.queryset import QuerySet
from tortoise.transactions import atomic, in_transaction

from app.core.constants import ENCODING
from app.core.dl.config import (
    decrypt_config,
    encrypt_config,
    load_driver,
)
from app.core.dl.driver import (
    DownloadAction,
    DownloadDraft,
    DownloaderDriver,
    DownloadIdentity,
    DownloadRequest,
    DownloadSource,
)
from app.core.dl.openlist.state import retry_state
from app.core.dl.rpc import RpcDriver
from app.core.dl.syncer import execute_download_plan, resolve_details, submission_lock
from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.download import (
    DownloadAdd,
    DownloadDir,
    Downloader,
    DownloaderUpsert,
    DownloadPlan,
    DownloadPlanUpsert,
    DownloadState,
    DownloadStats,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)
from app.services.base import BaseService
from app.utils.bittorrent import (
    decode_torrent,
    standardize_magnet,
)


class DownloaderService(BaseService[Downloader], model=Downloader):
    """Manage persisted downloader definitions."""

    PRESETS_PATH = Path(__file__).resolve().parents[2] / "static/downloaders"

    @classmethod
    async def dump(cls, obj: Downloader, **kwargs) -> dict:
        """Serialize a downloader with decrypted driver metadata.

        Args:
            obj: The downloader to serialize.
            kwargs: The serialization options forwarded to `BaseService.dump`.

        Returns:
            The serialized downloader and driver capabilities.
        """
        data = await super().dump(obj, **kwargs)
        if "config" not in data:
            return data
        config = decrypt_config(data["config"])
        driver = load_driver(config)
        source_types = driver.source_types
        data.update(
            config=config,
            driver=driver.config.driver,
            source_types=[
                source_type
                for source_type in DownloadSource
                if source_type in source_types
            ],
            status="unknown",
        )
        return data

    @classmethod
    async def dump_list(cls, objects, **kwargs) -> list[dict]:
        """Serialize downloader objects with driver metadata.

        Args:
            objects: The downloader objects or unresolved query set.
            kwargs: The serialization options forwarded to `BaseService.dump_list`.

        Returns:
            The serialized downloader records.
        """
        if isinstance(objects, QuerySet):
            objects = await objects
        return await super().dump_list(objects, **kwargs)

    @classmethod
    async def get_presets(cls) -> dict[str, str]:
        """Get the downloader presets.

        Returns:
            The downloader presets.
        """
        presets = {}
        for file in cls.PRESETS_PATH.iterdir():
            if file.is_file() and file.suffix == ".yaml":
                async with aiofiles.open(file, encoding=ENCODING) as f:
                    presets[file.name[:-5]] = await f.read()
        return presets

    @classmethod
    @atomic()
    async def update_priorities(cls, ids: list):
        """Update the downloader priorities.

        Args:
            ids: The sorted downloader IDs.
        """
        downloaders = await Downloader.all()
        if set(ids) != set(d.id for d in downloaders):
            raise KaloscopeException(ErrorCode.BAD_REQUEST)
        # avoid duplicate priorities
        priorities = [downloader.priority for downloader in downloaders]
        start_priority = 1 if min(priorities) > len(ids) else max(priorities) + 1
        for downloader in downloaders:
            downloader.priority = start_priority + ids.index(downloader.id)
        await Downloader.bulk_update(downloaders, fields=["priority"])

    @classmethod
    async def upsert(cls, obj: DownloaderUpsert) -> Downloader:
        """Create or update a downloader.

        Args:
            obj: The downloader data.

        Raises:
            KaloscopeException: If the name already exists.

        Returns:
            The downloader instance.
        """
        # load the YAML configuration
        driver = load_driver(obj.config)
        config = driver.config

        # check if the name already exists
        filter = Q(name=config.name)
        if obj.id:
            filter &= ~Q(id=obj.id)
        if await Downloader.filter(filter).count() > 0:
            raise KaloscopeException(ErrorCode.NAME_ALREADY_EXISTS)

        version = await driver.validate()
        stored_config = encrypt_config(obj.config)
        if obj.id:
            # update the downloader
            values = {
                "config": stored_config,
                "name": config.name,
                "host": getattr(config, "host", None),
                "port": getattr(config, "port", None),
            }
            if version is not None:
                values["version"] = version
            await Downloader.filter(id=obj.id).update(**values)
            downloader = await Downloader.get(id=obj.id)
        else:
            # create the downloader
            priorities: list = await Downloader.all().values_list("priority", flat=True)
            downloader = await Downloader.create(
                preset=obj.preset or None,
                config=stored_config,
                name=config.name,
                host=getattr(config, "host", None),
                port=getattr(config, "port", None),
                version=version,
                priority=(max(priorities) + 1 if priorities else 1),
            )

        return downloader


class DownloadDirService(BaseService[DownloadDir], model=DownloadDir):
    """Manage recently used local download directories."""

    @classmethod
    async def upsert(cls, path: str) -> DownloadDir:
        """Create or update a download directory.

        Args:
            path: The download directory path.

        Returns:
            The download directory instance.
        """
        dir = await DownloadDir.get_or_none(path=path)
        if dir is not None:
            dir.last_used = timezone.now()
            await dir.save()
            return dir
        return await DownloadDir.create(path=path, last_used=timezone.now())


class DownloadTaskService(BaseService[DownloadTask], model=DownloadTask):
    """Manage normalized tasks across all downloader drivers."""

    @classmethod
    async def dump(cls, obj: DownloadTask, **kwargs) -> dict:
        """Serialize one task with driver capabilities and offline-job errors.

        Args:
            obj: The download task to serialize.
            kwargs: The serialization options forwarded to `BaseService.dump`.

        Returns:
            The enriched serialized task.
        """
        return (await cls._dump_tasks([obj], **kwargs))[0]

    @classmethod
    async def dump_list(cls, objects, **kwargs) -> list[dict]:
        """Serialize task objects with driver capabilities and job errors.

        Args:
            objects: The download tasks or unresolved query set.
            kwargs: The serialization options forwarded to `BaseService.dump`.

        Returns:
            The enriched serialized tasks.
        """
        if isinstance(objects, QuerySet):
            objects = await objects
        return await cls._dump_tasks(objects, **kwargs)

    @classmethod
    async def _dump_tasks(cls, tasks: list[DownloadTask], **kwargs) -> list[dict]:
        """Serialize a resolved task batch without repeated related queries.

        Args:
            tasks: The resolved download tasks.
            kwargs: The serialization options forwarded to `BaseService.dump`.

        Returns:
            The enriched serialized tasks in input order.
        """
        if not tasks:
            return []
        downloaders = {
            downloader.id: downloader
            for downloader in await Downloader.filter(
                id__in={task.downloader_id for task in tasks}
            )
        }
        jobs = {
            job.download_id: job
            for job in await OfflineDownloadJob.filter(
                download_id__in={task.id for task in tasks}
            )
        }
        drivers = {
            downloader_id: load_driver(decrypt_config(downloader.config))
            for downloader_id, downloader in downloaders.items()
        }
        items = []
        for task in tasks:
            job = jobs.get(task.id)
            target = (
                retry_state(job.last_error_kind)
                if job is not None and job.last_error_kind is not None
                else None
            )
            capabilities = await drivers[task.downloader_id].capabilities(
                DownloadIdentity.from_task(task), task.state, retry_target=target
            )
            data = await super().dump(task, **kwargs)
            data["capabilities"] = [
                action for action in DownloadAction if action in capabilities
            ]
            data["error_kind"] = job.last_error_kind if job is not None else None
            items.append(data)
        return items

    @staticmethod
    def _task_values(
        downloader_id: int,
        request: DownloadRequest,
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Build common persistence values from a normalized request.

        Args:
            downloader_id: The owning downloader ID.
            request: The normalized driver request.
            name: The optional driver-provided task name.

        Raises:
            ValueError: If `request` has no normalized identity.

        Returns:
            The common `DownloadTask` field values.
        """
        identity = request.identity
        if identity is None:
            raise ValueError("Download request is missing its identity")
        return {
            "downloader_id": downloader_id,
            "dir": request.directory,
            "name": name
            if name is not None
            else identity.info_hash or identity.info_hash_v2,
            "info_hash": identity.info_hash,
            "info_hash_v2": identity.info_hash_v2,
            "transfer_lib_id": request.transfer_library_id,
            "transfer_method": request.transfer_method,
            "sub_pattern": request.sub_pattern,
            "sub_repl": request.sub_repl,
        }

    @staticmethod
    async def _normalize_source(
        driver: DownloaderDriver,
        request: DownloadRequest,
    ) -> DownloadRequest | None:
        """Normalize one source according to driver source capabilities.

        Args:
            driver: The target downloader driver.
            request: The request containing a torrent or raw source.

        Returns:
            The request with normalized identity and source fields, or `None` when
            the source is invalid or unsupported.
        """
        source_types = driver.source_types
        if request.torrent is not None:
            if DownloadSource.TORRENT not in source_types:
                return None
            torrent = decode_torrent(request.torrent[1])
            if torrent is None:
                return None
            return replace(
                request,
                identity=DownloadIdentity(info_hash=torrent.info_hash),
                link=torrent.magnet_link,
            )

        link = request.link.strip() if request.link else None
        if not link:
            return None
        probe_magnet = (
            DownloadSource.RAW not in source_types
            or not link.lower().startswith(("http://", "https://"))
        )
        magnet = await standardize_magnet(link) if probe_magnet else None
        if magnet is not None:
            if DownloadSource.MAGNET not in source_types:
                return None
            return replace(
                request,
                identity=DownloadIdentity(
                    info_hash=magnet.info_hash,
                    info_hash_v2=magnet.info_hash_v2,
                ),
                link=magnet.link,
            )
        if DownloadSource.RAW not in source_types:
            return None
        return replace(request, identity=DownloadIdentity(), link=link)

    @classmethod
    async def validate_source(cls, downloader_id: int, source: str) -> bool:
        """Validate a source against one downloader driver.

        Args:
            downloader_id: The downloader ID.
            source: The source string to validate.

        Returns:
            `True` if the driver accepts the source, otherwise `False`.
        """
        downloader = await Downloader.get(id=downloader_id)
        driver = load_driver(decrypt_config(downloader.config))
        request = DownloadRequest(directory="", link=source)
        return await cls._normalize_source(driver, request) is not None

    @classmethod
    async def hash_collision(cls, hash: str | None, hash_v2: str | None = None) -> bool:
        """Check if the info hash already exists.

        Args:
            hash: The info hash.
            hash_v2: The info hash v2.

        Returns:
            `True` if the info hash already exists, otherwise `False`.
        """
        if hash and await DownloadTask.filter(info_hash=hash).exists():
            return True
        return bool(
            hash_v2 and await DownloadTask.filter(info_hash_v2=hash_v2).exists()
        )

    @classmethod
    async def add(cls, add: DownloadAdd) -> DownloadTask:
        """Add a download task.

        Args:
            add: The download task details.

        Returns:
            The added download task.
        """
        downloader = await Downloader.get(id=add.downloader_id)
        driver = load_driver(decrypt_config(downloader.config))

        request = await cls._normalize_source(
            driver,
            DownloadRequest(
                directory=add.dir,
                link=add.link,
                torrent=(
                    (add.torrent.name, add.torrent.body, add.torrent.type)
                    if add.torrent
                    else None
                ),
                paused=add.pause,
                transfer_library_id=add.transfer_lib_id,
                transfer_method=add.transfer_method,
                sub_pattern=add.sub_pattern,
                sub_repl=add.sub_repl,
            ),
        )
        if request is None:
            raise KaloscopeException(ErrorCode.INVALID_DOWNLOAD_SOURCE)
        identity = request.identity
        if identity is None:
            raise ValueError("Normalized download request is missing its identity")
        # preserve RPC deduplication until its remote submission is persisted
        transaction = (
            in_transaction() if isinstance(driver, RpcDriver) else nullcontext()
        )
        async with transaction:
            if await cls.hash_collision(identity.info_hash, identity.info_hash_v2):
                raise KaloscopeException(ErrorCode.INFO_HASH_COLLISION)
            return await cls.add_request(downloader.id, driver, request)

    @classmethod
    async def add_request(
        cls,
        downloader_id: int,
        driver: DownloaderDriver,
        request: DownloadRequest,
    ) -> DownloadTask:
        """Submit a normalized download request and persist its task.

        Args:
            downloader_id: The downloader ID.
            driver: The driver used for submission.
            request: The normalized download request.

        Returns:
            The persisted download task.
        """
        identity = request.identity
        draft = driver.prepare(request)
        if draft is not None:
            lock = (
                submission_lock(draft.job.job_uuid)
                if draft.job is not None
                else nullcontext()
            )
            with lock:
                return await cls._add_draft(downloader_id, driver, draft)

        snapshot = await driver.add(request)

        # save the download directory
        await DownloadDirService.upsert(request.directory)
        # create the download task
        return await DownloadTask.create(
            **cls._task_values(downloader_id, request),
            unique_id=snapshot.identity.remote_id,
            magnet_link=(
                request.link
                if identity and (identity.info_hash or identity.info_hash_v2)
                else None
            ),
            state=(
                DownloadState.PAUSED if request.paused else DownloadState.DOWNLOADING
            ),
        )

    @classmethod
    async def _add_draft(
        cls,
        downloader_id: int,
        driver: DownloaderDriver,
        draft: DownloadDraft,
    ) -> DownloadTask:
        """Persist a driver draft before submitting its remote request.

        Args:
            downloader_id: The owning downloader ID.
            driver: The driver used for remote submission.
            draft: The task and optional offline-job persistence draft.

        Returns:
            The persisted task updated with its submission snapshot.
        """
        request = draft.request
        # persist recovery metadata before the remote submission can become uncertain
        async with in_transaction() as connection:
            task = await DownloadTask.create(
                **cls._task_values(
                    downloader_id,
                    request,
                    name=draft.name,
                ),
                magnet_link=draft.magnet_link,
                state=draft.state,
                percentage=draft.percentage,
                using_db=connection,
            )
            if draft.job is not None:
                job = draft.job
                await OfflineDownloadJob.create(
                    download_id=task.id,
                    job_uuid=job.job_uuid,
                    source_fingerprint=job.source_fingerprint,
                    remote_dir=job.remote_directory,
                    using_db=connection,
                )

        try:
            snapshot = await driver.add(
                replace(
                    request,
                    identity=DownloadIdentity.from_task(task),
                )
            )
        except Exception:
            # remove the pre-submission task when the driver reports a definite failure
            await DownloadTask.filter(id=task.id).delete()
            raise
        if snapshot.state is None:
            raise ValueError("Download result is missing its state")
        task.unique_id = snapshot.identity.remote_id
        task.state = snapshot.state
        task.percentage = snapshot.percentage
        task.error_msg = snapshot.error
        async with in_transaction() as connection:
            await task.save(
                update_fields=["unique_id", "state", "percentage", "error_msg"],
                using_db=connection,
            )
            if snapshot.state is DownloadState.SUBMIT_UNKNOWN:
                await (
                    OfflineDownloadJob.filter(download_id=task.id)
                    .using_db(connection)
                    .update(last_error_kind=OfflineDownloadErrorKind.SUBMIT_UNKNOWN)
                )
        await DownloadDirService.upsert(request.directory)
        return task

    @classmethod
    async def pause(cls, id: int):
        """Pause a download task.

        Args:
            id: The download task ID.
        """
        task = await DownloadTask.get(id=id)
        downloader = await Downloader.get(id=task.downloader_id)
        driver = load_driver(decrypt_config(downloader.config))
        identity = DownloadIdentity.from_task(task)
        if DownloadAction.PAUSE not in await driver.capabilities(identity, task.state):
            return
        if await OfflineDownloadJob.get_or_none(download_id=id) is not None:
            return DownloadAction.PAUSE, task.state
        state = await driver.pause(identity)
        if state is not None:
            await DownloadTask.filter(id=id).update(state=state)

    @classmethod
    async def start(cls, id: int):
        """Start a download task.

        Args:
            id: The download task ID.
        """
        task = await DownloadTask.get(id=id)
        downloader = await Downloader.get(id=task.downloader_id)
        driver = load_driver(decrypt_config(downloader.config))
        identity = DownloadIdentity.from_task(task)
        if DownloadAction.RESUME not in await driver.capabilities(identity, task.state):
            return
        if await OfflineDownloadJob.get_or_none(download_id=id) is not None:
            return DownloadAction.RESUME, task.state
        state = await driver.resume(identity)
        if state is not None:
            await DownloadTask.filter(id=id).update(state=state)

    @classmethod
    async def retry(cls, id: int):
        """Retry a download task.

        Args:
            id: The download task ID.
        """
        task = await DownloadTask.get(id=id)
        downloader = await Downloader.get(id=task.downloader_id)
        driver = load_driver(decrypt_config(downloader.config))
        identity = DownloadIdentity.from_task(task)
        job = await OfflineDownloadJob.get_or_none(download_id=id)
        target = (
            retry_state(job.last_error_kind) if job and job.last_error_kind else None
        )
        capabilities = await driver.capabilities(
            identity, task.state, retry_target=target
        )
        if DownloadAction.RETRY not in capabilities:
            return
        if job is not None:
            return DownloadAction.RETRY, task.state
        state = await driver.retry(identity)
        if state is not None:
            await DownloadTask.filter(id=id).update(state=state)

    @classmethod
    async def delete(cls, id: int, local: bool = False):
        """Delete a download task.

        Args:
            id: The download task ID.
            local: Whether to delete the local files.
        """
        task = await DownloadTask.get(id=id)
        downloader = await Downloader.get(id=task.downloader_id)
        driver = load_driver(decrypt_config(downloader.config))
        identity = DownloadIdentity.from_task(task)
        if DownloadAction.DELETE not in await driver.capabilities(identity, task.state):
            return
        if await OfflineDownloadJob.filter(download_id=id).update(
            delete_due_at=timezone.now(), delete_local=local
        ):
            return DownloadAction.DELETE, task.state
        unique = identity.rpc_variables

        # get the local path when using aria2 with `local=True`
        local_path = None
        local_safe = False
        if local and downloader.preset == "aria2" and isinstance(driver, RpcDriver):
            name = task.name
            result = None
            try:
                result = await resolve_details(driver.client, unique)
            except KaloscopeException as e:
                local_safe = (
                    e.extra is not None
                    and e.extra.get("responded")
                    and str(e).startswith("GID ")
                    and str(e).endswith(" is not found")
                )
            if isinstance(result, dict):
                unique["id"] = str(result.get("unique_id") or unique["id"])
                local_safe = str(result.get("raw_state") or "").lower() in {
                    "complete",
                    "error",
                    "removed",
                }
            if name in {task.info_hash, task.info_hash_v2}:
                name = str(result.get("name") or "") if isinstance(result, dict) else ""
            if name:
                relative = Path(name)
                if name not in {".", ".."} and relative.name == name:
                    local_path = Path(task.dir) / relative

        # call the `delete` method
        try:
            remote_id = str(unique["id"]) if unique["id"] else None
            await driver.delete(replace(identity, remote_id=remote_id), local=local)
        except KaloscopeException as e:
            if not local_safe:
                if e.extra is not None and e.extra.get("responded"):
                    # treat any downloader response as delete-request delivery
                    local_path = None
                else:
                    raise

        # delete local aria2 files when the RPC succeeds or the task is safe
        if local_path is not None:
            if local_path.exists():
                if local_path.is_dir() and not local_path.is_symlink():
                    shutil.rmtree(local_path)
                else:
                    local_path.unlink()

            control_path = Path(f"{local_path}.aria2")
            if control_path.exists():
                control_path.unlink()

        # delete the download task
        await DownloadTask.filter(id=id).delete()

    @classmethod
    async def stats(cls) -> DownloadStats:
        """Get the download statistics.

        Returns:
            The download statistics object.
        """
        count = (
            await DownloadTask.annotate(count=Count("id"))
            .group_by("state")
            .values("state", "count")
        )
        downloading = sum(
            c["count"] for c in count if c["state"] != DownloadState.COMPLETED
        )
        completed = sum(
            c["count"] for c in count if c["state"] == DownloadState.COMPLETED
        )
        up_speed = (
            await DownloadTask.annotate(total_up=Sum("up_speed"))
            .filter(state=DownloadState.DOWNLOADING)
            .values("total_up")
        )[0]["total_up"] or 0
        dl_speed = (
            await DownloadTask.annotate(total_dl=Sum("dl_speed"))
            .filter(state__in=[DownloadState.DOWNLOADING, DownloadState.PULLING])
            .values("total_dl")
        )[0]["total_dl"] or 0
        return DownloadStats(
            downloading=downloading,
            completed=completed,
            up_speed=up_speed,
            dl_speed=dl_speed,
        )


class DownloadPlanService(BaseService[DownloadPlan], model=DownloadPlan):
    """Manage persisted automated download plans."""

    @classmethod
    async def upsert(cls, obj: DownloadPlanUpsert) -> DownloadPlan:
        """Create or update a download plan.

        Args:
            obj: The download plan data.

        Returns:
            The download plan instance.
        """
        data = obj.model_dump(exclude={"id"})
        if obj.id:
            await DownloadPlan.filter(id=obj.id).update(**data)
            plan = await DownloadPlan.get(id=obj.id)
        else:
            plan = await DownloadPlan.create(**data)
            # execute the plan immediately if it's active
            if not plan.inactive():
                await execute_download_plan(plan)
        return plan
