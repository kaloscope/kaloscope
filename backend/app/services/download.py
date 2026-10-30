import asyncio
import hashlib
import mimetypes
import os
import re
import shutil
from collections.abc import Collection
from contextlib import nullcontext, suppress
from dataclasses import dataclass, replace
from datetime import UTC, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

import aiofiles
import httpx
import orjson
from filelock import Timeout as LockTimeout
from jinja2 import TemplateSyntaxError
from jinja2.sandbox import SecurityError
from pydantic import ValidationError
from sanic import Sanic
from sanic.log import logger
from tortoise import timezone
from tortoise.exceptions import IntegrityError
from tortoise.expressions import Q
from tortoise.functions import Count, Sum
from tortoise.queryset import QuerySet
from tortoise.transactions import atomic, in_transaction

from app.core.config import KaloscopeConfig
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
from app.core.dl.rpc.models import Method
from app.core.dl.syncer import (
    TV_TRANSFER_DEPTH,
    execute_download_plan,
    notify_download_changes,
    resolve_details,
    submission_lock,
    transfer_issues,
    transfer_names,
    transfer_target,
)
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.flow.nodes.base import start_config
from app.core.flow.outputs import check_output, download_link_kind
from app.core.renderer import SANDBOX_ENV, is_template
from app.models.assistant import DOWNLOAD_PATHS
from app.models.download import (
    PLAN_SETTINGS,
    DownloadAdd,
    DownloadDir,
    Downloader,
    DownloaderUpsert,
    DownloadPlan,
    DownloadPlanApply,
    DownloadPlanChange,
    DownloadPlanCheck,
    DownloadPlanCreate,
    DownloadPlanQuery,
    DownloadPlanReceipt,
    DownloadPlanSettings,
    DownloadPlanUpsert,
    DownloadQuery,
    DownloadState,
    DownloadStats,
    DownloadTask,
    DownloadTaskAction,
    DownloadTaskActionApply,
    DownloadTaskCheck,
    DownloadTaskCreate,
    DownloadTaskReceipt,
    DownloadTaskSettings,
    IndexerSearch,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
    PlanAction,
    TaskAction,
)
from app.models.flow import FlowGraph, GraphCategory, GraphState
from app.models.media import LibType, MediaLib
from app.models.user import UserInfo, UserRole
from app.services.base import BaseService
from app.services.flow import FlowGraphService
from app.services.media import MediaLibService
from app.utils.bittorrent import (
    decode_torrent,
    standardize_magnet,
)
from app.utils.crypto import fernet_decrypt, fernet_encrypt
from app.utils.json import dumps, loads


def default_download_directory() -> Path:
    """Resolve the editor's default directory without creating it.

    Returns:
        A readable conventional directory or the workspace download path.
    """
    for path in (Path("/downloads"), Path.home() / "Downloads"):
        try:
            if path.is_dir() and os.access(path, os.R_OK):
                return path.resolve()
        except OSError:
            continue
    return Path(KaloscopeConfig.get()._workspace) / "downloads"


async def seal_source(
    workflow_id: int, resource_id: str, title: str | None, link: str
) -> str:
    """Seal a search result's download link for one later selection.

    Args:
        workflow_id: Indexer that returned the result.
        resource_id: Result ID shown to the user.
        title: Observed title shown to the user.
        link: Exact link from the search output.

    Returns:
        Fernet token binding the link to this indexer and result; the link itself
        never appears in assistant or MCP results.
    """
    payload = dumps({"w": workflow_id, "i": resource_id, "t": title, "l": link})
    return await asyncio.to_thread(
        fernet_encrypt,
        payload.decode(),
        KaloscopeConfig.get().secret_key_path,
        create_key=True,
    )


async def open_source(selection: Any) -> dict[str, Any] | None:
    """Unseal a selected result's source and check that it belongs to it.

    Args:
        selection: Reviewed indexer, result ID and sealed source.

    Returns:
        Observed title and exact link, or `None` for a foreign, damaged or
        unreadable source.
    """
    try:
        payload = loads(
            await asyncio.to_thread(
                fernet_decrypt, selection.source, KaloscopeConfig.get().secret_key_path
            )
        )
    except (RuntimeError, OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("w") != selection.workflow_id
        or payload.get("i") != selection.resource_id
        or not isinstance(payload.get("l"), str)
    ):
        return None
    return {"title": payload.get("t"), "link": payload["l"]}


# keep transfer previews compact in assistant and MCP results
TRANSFER_PREVIEW_ITEMS = 6
TRANSFER_PREVIEW_PROBLEMS = 4
TRANSFER_PATH_CHARS = 160
TV_TRANSFER_HINT = (
    "sub_pattern is a regex replaced in each relative path by sub_repl, a template "
    "with title/year/season/episode read from the file name, e.g. ^(?:.*/)? -> "
    "Show/Season {{ season }}/. Use the user's or evidenced series name and ask "
    "only for missing ones; search titles are not file names."
)


def _shown(path: str) -> str:
    """Shorten a long path for a transfer preview, keeping both ends.

    Args:
        path: Source or destination path.

    Returns:
        The path, or its start and end joined by an ellipsis when it is longer
        than `TRANSFER_PATH_CHARS`.
    """
    if len(path) <= TRANSFER_PATH_CHARS:
        return path
    return f"{path[:60]}…{path[-(TRANSFER_PATH_CHARS - 61) :]}"


def _rule_failure(
    pattern: str, repl: str | None, names: list[str]
) -> tuple[str, Exception] | None:
    """Find the first file whose destination name cannot be produced.

    Args:
        pattern: Transfer `sub_pattern`.
        repl: Transfer `sub_repl`.
        names: Relative source paths.

    Returns:
        The failing path and its error, or `None` when every name renders.
    """
    for name in names:
        try:
            transfer_names(pattern, repl, [name], sandboxed=True)
        except Exception as error:
            return name, error
    return None


async def transfer_preview(
    lib: MediaLib,
    pattern: str | None,
    repl: str | None,
    *,
    files: list[str] | None = None,
    samples: list[str] | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any] | None]:
    """Preview where file transfer puts files and how the library reads them.

    Uses the transfer's own naming with sandboxed templates. Nothing is created,
    moved or recorded; destinations are only checked for existing files.

    Args:
        lib: Accessible transfer library.
        pattern: `sub_pattern` applied to each relative download path.
        repl: `sub_repl` replacement template.
        files: Relative paths known from the download's torrent metadata.
        samples: Illustrative file names when the actual files are unknown.

    Returns:
        Located issues and a bounded preview: library, rule outcome, basis, the
        first source-to-destination items (problems first) with the season and
        episode a TV library reads, the omitted count and notes. The preview is
        `None` when a movie or reading library has no rule and no names.
    """
    from app.core.media.handlers.base import get_handler
    from app.core.media.handlers.tvshow import episode_location

    names = files or samples or []
    if not (pattern or names or lib.lib_type == LibType.TV_SHOW):
        return [], None
    field = "sub_repl" if pattern else "sub_pattern"
    issues: list[dict[str, str]] = []
    notes: list[str] = []
    preview: dict[str, Any] = {
        "library": lib.name,
        "lib_type": lib.lib_type,
        "basis": "known_files" if files else "samples" if samples else None,
        "rule": "unchecked" if pattern else "none",
    }
    if pattern:
        try:
            regex = re.compile(pattern)
        except re.error as error:
            issues.append({"path": "sub_pattern", "message": f"Invalid regex: {error}"})
            return issues, preview
        try:
            SANDBOX_ENV.parse(repl or "")
        except TemplateSyntaxError as error:
            issues.append({"path": "sub_repl", "message": f"Invalid template: {error}"})
            return issues, preview
        if not is_template(repl or ""):
            try:
                regex.sub(repl or "", "")
            except re.error as error:
                issues.append(
                    {"path": "sub_repl", "message": f"Invalid replacement: {error}"}
                )
                return issues, preview
    if not names:
        if lib.lib_type == LibType.TV_SHOW:
            preview["notes"] = [
                f"No file names to check. {TV_TRANSFER_DEPTH} Supply transfer_samples."
            ]
            preview["hint"] = TV_TRANSFER_HINT
        return issues, preview
    if pattern and (failure := _rule_failure(pattern, repl, names)):
        name, error = failure
        name = _shown(name)
        message = (
            "Uses blocked Python internals"
            if isinstance(error, SecurityError)
            else str(error)
        )
        issues.append(
            {
                "path": "sub_repl",
                "message": f"{name}: {message}; transfer would fail for every file.",
            }
        )
        return issues, preview
    destinations, applied = transfer_names(pattern, repl, names, sandboxed=True)
    if pattern:
        preview["rule"] = "applied" if applied else "conflict"
        if not applied:
            issues.append(
                {
                    "path": "sub_repl",
                    "message": "Several files get one destination; "
                    "new transfers are blocked.",
                }
            )
            return issues, preview

    root = Path(os.path.normpath(Path(lib.dir).absolute()))
    handler = get_handler(lib.lib_type)
    targets = [transfer_target(root, name) for name in destinations]
    path_issues = transfer_issues(lib, names, targets)
    items = []
    video_count = 0
    series = set()
    skipped = 0
    unnumbered = []
    for index, (source, name) in enumerate(zip(names, destinations, strict=True)):
        target = targets[index]
        shown = _shown(str(target.relative_to(root)) if target else name)
        item: dict[str, Any] = {"from": _shown(source), "to": shown}
        video = (mimetypes.guess_file_type(name)[0] or "").startswith("video/")
        problem = f"{shown}: {path_issues[index]}" if index in path_issues else None
        if (
            target is not None
            and not problem
            and (target.exists() or target.is_symlink())
        ):
            problem = f"{shown} already exists; transfer will not overwrite it."
        elif (
            target is not None
            and video
            and not handler.is_target(str(root), str(target), check_exists=False)
        ):
            if lib.lib_type == LibType.TV_SHOW and target.parent == root:
                problem = f"{shown}: {TV_TRANSFER_DEPTH}"
            else:
                # deeper extras or samples stay unindexed without blocking the rest
                item["indexed"] = False
                skipped += 1
        elif target is not None and video and lib.lib_type == LibType.TV_SHOW:
            parts = target.relative_to(root).parts
            season, episode = episode_location(parts) or (None, None)
            item.update(season=season, episode=episode)
            series.add(parts[0])
            if episode is None:
                unnumbered.append(shown)
        if video:
            video_count += 1
        items.append((problem, item))

    problems = [entry[0] for entry in items if entry[0]]
    issues.extend(
        {"path": field, "message": problem}
        for problem in problems[:TRANSFER_PREVIEW_PROBLEMS]
    )
    if len(problems) > TRANSFER_PREVIEW_PROBLEMS:
        issues.append(
            {
                "path": field,
                "message": f"{len(problems) - TRANSFER_PREVIEW_PROBLEMS} more files "
                "have problems like these.",
            }
        )
    if skipped and skipped == video_count and lib.lib_type != LibType.TV_SHOW:
        issues.append(
            {
                "path": field,
                "message": "No video reaches a folder depth the library indexes.",
            }
        )
    elif 0 < skipped < video_count:
        notes.append(
            f"{skipped} video(s) marked indexed=false are below the indexed folder "
            "depth, e.g. extras; they stay unindexed."
        )
    if unnumbered:
        more = f" and {len(unnumbered) - 1} more" if len(unnumbered) > 1 else ""
        notes.append(
            f"{unnumbered[0]}{more}: no episode number; ingestion gets episode=null."
        )
    if len(series) > 1:
        folders = ", ".join(_shown(folder) for folder in sorted(series)[:3])
        notes.append(f"Files reach {len(series)} series folders: {folders}.")
    if samples and not files:
        notes.insert(
            0, "Samples only illustrate the rule; actual names are known on completion."
        )
    ordered = [entry[1] for entry in items if entry[0]] + [
        entry[1] for entry in items if not entry[0]
    ]
    preview["items"] = ordered[:TRANSFER_PREVIEW_ITEMS]
    if len(ordered) > TRANSFER_PREVIEW_ITEMS:
        preview["omitted"] = len(ordered) - TRANSFER_PREVIEW_ITEMS
    if notes:
        preview["notes"] = notes[:3]
    if issues and lib.lib_type == LibType.TV_SHOW:
        preview["hint"] = TV_TRANSFER_HINT
    return issues, preview


# avoid contending for the shared submission lock within this worker
_REQUEST_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_PLAN_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_TASK_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
# RPC methods behind immediate task actions, for delivery checks after errors
_TASK_METHODS: dict[TaskAction, Method] = {
    TaskAction.PAUSE: "pause",
    TaskAction.RESUME: "start",
    TaskAction.DELETE: "delete",
}
TASK_UNCERTAIN = (
    "The downloader may have received it; read the task with get_download_task "
    "instead of repeating the request."
)


def _maybe_sent(error: Exception, driver: DownloaderDriver, method: Method) -> bool:
    """Check whether a failed RPC request may have reached the downloader.

    Args:
        error: Exception raised by the driver.
        driver: Driver whose configured method may contain multiple requests.
        method: Configured RPC method that failed, such as `add_link` or `pause`.

    Returns:
        Whether the request may have succeeded. Single-request connection failures
        and HTTP client rejections are definite. Multi-step failures retain
        uncertainty because an earlier step may already have reached it.
    """
    if isinstance(driver, RpcDriver):
        api = driver.config.methods.get(method)
        if api and api.steps:
            return True
    cause = error.__context__ if isinstance(error, KaloscopeException) else error
    if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return False
    extra = getattr(error, "extra", None) or {}
    status = extra.get("status_code", 0)
    return not (extra.get("responded") and 400 <= status < 500 and status != 408)


@dataclass(frozen=True, slots=True)
class _TaskControl:
    """Describe what one task's downloader allows now and how it applies it."""

    downloader: Downloader
    driver: DownloaderDriver
    job: OfflineDownloadJob | None
    actions: list[TaskAction]
    retry_from: DownloadState | None
    revision: str


class DownloaderService(BaseService[Downloader], model=Downloader):
    """Manage persisted downloader definitions."""

    PRESETS_PATH = Path(__file__).resolve().parents[2] / "static/downloaders"

    @classmethod
    async def dump(cls, obj: Downloader, **kwargs) -> dict:
        """Serialize a downloader with decrypted driver metadata.

        Args:
            obj: Downloader to serialize.
            **kwargs: Serialization options forwarded to `BaseService.dump`.

        Returns:
            Serialized downloader and driver capabilities.
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
            objects: Downloader objects or unresolved query set.
            **kwargs: Serialization options forwarded to `BaseService.dump_list`.

        Returns:
            Serialized downloader records.
        """
        if isinstance(objects, QuerySet):
            objects = await objects
        return await super().dump_list(objects, **kwargs)

    @classmethod
    async def get_presets(cls) -> dict[str, str]:
        """Get downloader presets.

        Returns:
            Downloader presets.
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
        """Update downloader priorities.

        Args:
            ids: Sorted downloader IDs.
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
            obj: Downloader data.

        Raises:
            KaloscopeException: If the name already exists.

        Returns:
            Downloader instance.
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
            path: Download directory path.

        Returns:
            Download directory instance.
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
    async def list_tasks(
        cls, query: DownloadQuery, *, include: tuple[str, ...] = ()
    ) -> dict[str, Any]:
        """List persisted download states without polling or controlling a driver.

        Args:
            query: Pagination, name, state, and downloader filters.
            include: Serialized fields to include; an empty tuple keeps all fields.

        Returns:
            A task page using the application's shared download visibility.
        """
        queries = []
        if query.name:
            queries.append(Q(name__icontains=query.name))
        if query.state:
            queries.append(Q(state=query.state))
        if query.states:
            queries.append(Q(state__in=query.states))
        if query.downloader_id:
            queries.append(Q(downloader_id=query.downloader_id))
        page = await DownloadTask.page(*queries, **query.page_params)
        return await cls.dump_page(page, include=include)

    @classmethod
    async def dump(cls, obj: DownloadTask, **kwargs) -> dict:
        """Serialize one task with driver capabilities and offline-job errors.

        Args:
            obj: Download task to serialize.
            **kwargs: Serialization options forwarded to `BaseService.dump`.

        Returns:
            Enriched serialized task.
        """
        return (await cls._dump_tasks([obj], **kwargs))[0]

    @classmethod
    async def dump_list(cls, objects, **kwargs) -> list[dict]:
        """Serialize task objects with driver capabilities and job errors.

        Args:
            objects: Download tasks or unresolved query set.
            **kwargs: Serialization options forwarded to `BaseService.dump`.

        Returns:
            Enriched serialized tasks.
        """
        if isinstance(objects, QuerySet):
            objects = await objects
        return await cls._dump_tasks(objects, **kwargs)

    @classmethod
    async def _dump_tasks(cls, tasks: list[DownloadTask], **kwargs) -> list[dict]:
        """Serialize a resolved task batch without repeated related queries.

        Args:
            tasks: Resolved download tasks.
            **kwargs: Serialization options forwarded to `BaseService.dump`.

        Returns:
            Enriched serialized tasks in input order.
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
            downloader_id: Owning downloader ID.
            request: Normalized driver request.
            name: Optional driver-provided task name. `None` uses the normalized
                source hash.

        Raises:
            ValueError: If `request` has no normalized identity.

        Returns:
            Common `DownloadTask` field values.
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
            driver: Target downloader driver.
            request: Request containing a torrent or raw source.

        Returns:
            Request with normalized identity and source fields, or `None` when
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
            torrent = magnet.torrent if DownloadSource.TORRENT in source_types else None
            if torrent is None and DownloadSource.MAGNET not in source_types:
                return None
            return replace(
                request,
                identity=DownloadIdentity(
                    info_hash=magnet.info_hash,
                    info_hash_v2=magnet.info_hash_v2,
                ),
                link=magnet.link,
                torrent=torrent,
            )
        if DownloadSource.RAW not in source_types:
            return None
        return replace(request, identity=DownloadIdentity(), link=link)

    @classmethod
    async def validate_source(cls, downloader_id: int, source: str) -> bool:
        """Validate a source against one downloader driver.

        Args:
            downloader_id: Downloader ID.
            source: Source string to validate.

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
            hash: Info hash.
            hash_v2: Info hash v2. `None` checks only the version 1 hash.

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
        """Add a download task and wake synchronization after committing.

        Args:
            add: Download task details.

        Returns:
            Added download task.
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
            task = await cls.add_request(downloader.id, driver, request)
        notify_download_changes()
        return task

    @classmethod
    async def _review_request(
        cls,
        settings: DownloadTaskSettings,
        ingest_workflow_id: int | None,
        user,
        samples: list[str] | None = None,
    ) -> tuple[list[dict[str, str]], dict[str, Any], DownloadRequest | None, Any]:
        """Check a selected result and settings exactly as submission will use them.

        Args:
            settings: Reviewed result, downloader, directory and transfer.
            ingest_workflow_id: Optional ingestion workflow to bind.
            user: Current administrator; permissions are checked again.
            samples: Illustrative file names for the transfer preview when the
                torrent's files are unknown.

        Returns:
            Located issues, the review shown to the user including its transfer
            rule and preview, the normalized request and the loaded driver
            (callers close it).

        Raises:
            ForbiddenException: If the account cannot submit downloads or read the
                indexer, library or workflow.
            NotFoundException: If the indexer, library or workflow is missing.
        """
        FlowGraphService.require_editor(user)
        selection = settings.resource
        graph = await FlowGraphService.get_accessible(selection.workflow_id, user)
        issues = []
        review: dict[str, Any] = {
            "workflow_id": graph.id,
            "workflow_name": graph.name,
            "resource_id": selection.resource_id,
            "transfer_lib_id": settings.transfer_lib_id,
        }
        if graph.category != GraphCategory.INDEXER or graph.state == GraphState.DRAFT:
            issues.append(
                {"path": "resource.workflow_id", "message": "Use a published indexer."}
            )
        source = await open_source(selection)
        if source is None:
            issues.append(
                {
                    "path": "resource.source",
                    "message": "Use this result's source from search_indexer with "
                    "sources=true; search again if it changed.",
                }
            )
        else:
            review["title"] = source["title"]
        downloader = await Downloader.get_or_none(id=settings.downloader_id)
        driver = None
        if downloader is None:
            issues.append(
                {"path": "downloader_id", "message": "Select an existing downloader."}
            )
        else:
            review["downloader_name"] = downloader.name
        lib = None
        review.update(
            (field, value)
            for field in ("sub_pattern", "sub_repl")
            if (value := getattr(settings, field)) is not None
        )
        if settings.transfer_lib_id:
            lib = await MediaLibService.get_accessible(settings.transfer_lib_id, user)
            if settings.transfer_method is None:
                issues.append(
                    {
                        "path": "transfer_method",
                        "message": "Choose a transfer method for the library.",
                    }
                )
        if ingest_workflow_id is not None:
            if settings.transfer_lib_id:
                found, review["ingestion"] = await MediaLibService.ingestion_check(
                    settings.transfer_lib_id, ingest_workflow_id, user
                )
                issues.extend(found)
            else:
                issues.append(
                    {
                        "path": "transfer_lib_id",
                        "message": "Choose the library that ingests it.",
                    }
                )
        if settings.sub_pattern and not settings.transfer_lib_id:
            issues.append(
                {
                    "path": "transfer_lib_id",
                    "message": "Choose the library that receives renamed files.",
                }
            )
        if issues:
            if lib is not None:
                found, preview = await transfer_preview(
                    lib, settings.sub_pattern, settings.sub_repl, samples=samples
                )
                issues.extend(found)
                if preview:
                    review["transfer"] = preview
            return issues, review, None, None
        driver = load_driver(decrypt_config(downloader.config))
        request = None
        known = None
        try:
            # the existing normalizer requires torrent content or a hashed magnet
            magnet = await standardize_magnet(source["link"])
            if magnet is None:
                issues.append(
                    {
                        "path": "resource.source",
                        "message": "The result's link is not a magnet, BT hash or "
                        "torrent file; detail and player pages cannot be "
                        "downloaded.",
                    }
                )
            else:
                # torrent content falls back to its magnet like other submissions
                torrent = (
                    magnet.torrent
                    if DownloadSource.TORRENT in driver.source_types
                    else None
                )
                request = await cls._normalize_source(
                    driver,
                    DownloadRequest(
                        directory=settings.dir,
                        link=magnet.link,
                        torrent=torrent,
                        transfer_library_id=settings.transfer_lib_id,
                        transfer_method=settings.transfer_method,
                        sub_pattern=settings.sub_pattern,
                        sub_repl=settings.sub_repl,
                    ),
                )
                if magnet.torrent and (content := decode_torrent(magnet.torrent[1])):
                    # torrent metadata names the files the downloader will write
                    known = [file.name for file in content.files]
                review["source_kind"] = "torrent" if magnet.torrent else "magnet"
                review["info_hash"] = magnet.info_hash or magnet.info_hash_v2
                if request is None:
                    issues.append(
                        {
                            "path": "downloader_id",
                            "message": "This downloader cannot accept the result's "
                            "magnet or torrent.",
                        }
                    )
            if lib is not None:
                found, preview = await transfer_preview(
                    lib,
                    settings.sub_pattern,
                    settings.sub_repl,
                    files=known,
                    samples=samples,
                )
                issues.extend(found)
                if preview:
                    review["transfer"] = preview
        except BaseException:
            await driver.close()
            raise
        return issues, review, request, driver

    @classmethod
    async def validate_request(cls, body: DownloadTaskCheck, user) -> dict[str, Any]:
        """Validate one selected result for a one-time download without submitting.

        Args:
            body: Reviewed result, settings and optional ingestion workflow.
            user: Current administrator.

        Returns:
            Located issues, the reviewed result and settings, an existing task with
            the same content if any, and a new request ID for confirmation. Only
            reads happen; a torrent URL may be fetched to identify its content.
        """
        issues, review, request, driver = await cls._review_request(
            body, body.ingest_workflow_id, user, body.transfer_samples
        )
        if driver is not None:
            await driver.close()
        result = {
            "valid": False,
            "request_id": str(uuid4()),
            "download": {
                **review,
                **body.model_dump(
                    mode="json", include={"downloader_id", "dir", "transfer_lib_id"}
                ),
                "transfer_method": body.transfer_method,
            },
        }
        if (
            request is not None
            and request.identity is not None
            and (existing := await cls._same_content(request.identity))
        ):
            result["existing_download"] = existing
            issues.append(
                {
                    "path": "resource",
                    "message": f"Task {existing['task_id']} already has this "
                    "content; open it instead of downloading again.",
                }
            )
        if ingestion := review.pop("ingestion", None):
            result["ingestion"] = ingestion
            result["download"].pop("ingestion", None)
        result.update(
            valid=not issues,
            issues=issues[:12],
            first_execution="Submitted after confirmation; downloading, transfer and "
            "ingestion then continue in the background.",
        )
        return result

    @classmethod
    async def _same_content(cls, identity: DownloadIdentity) -> dict | None:
        """Find an existing task holding the same torrent content.

        Args:
            identity: Normalized content hashes.

        Returns:
            Compact task facts, or `None` when no task has these hashes.
        """
        queries = [
            Q(**{field: value})
            for field, value in (
                ("info_hash", identity.info_hash),
                ("info_hash_v2", identity.info_hash_v2),
            )
            if value
        ]
        task = (
            await DownloadTask.filter(Q(*queries, join_type="OR")).first()
            if queries
            else None
        )
        if task is None:
            return None
        return {"task_id": task.id, "name": task.name, "state": task.state}

    @classmethod
    async def _task_result(
        cls, task: DownloadTask, user: UserInfo, *, replayed: bool = False
    ) -> dict[str, Any]:
        """Describe a submitted one-time task without claiming later progress.

        Args:
            task: Task created or replayed for the request.
            user: Current account whose source permissions are rechecked.
            replayed: Whether the same request already created it.

        Returns:
            Task identity, current stored state and where to follow it.
        """
        receipt = task.creation_receipt or {}
        download = receipt["download"]
        await FlowGraphService.get_accessible(download["workflow_id"], user)
        library_id = download.get("transfer_lib_id")
        if library_id:
            await MediaLibService.get_accessible(library_id, user)
        result = {
            "created": True,
            "replayed": replayed,
            "task_id": task.id,
            "name": task.name,
            "state": task.state,
            "uncertain": task.state == DownloadState.SUBMIT_UNKNOWN,
            "path": "/downloads/downloading",
            "download": download,
        }
        if library_id and (workflow_id := receipt.get("ingest_workflow_id")):
            with suppress(NotFoundException):
                _, review = await MediaLibService.ingestion_check(
                    library_id, workflow_id, user
                )
                result["ingestion"] = {
                    **review,
                    "binding": "configured"
                    if review["binding"] == "reuse"
                    else "missing",
                }
        return result

    @classmethod
    async def _replay_request(
        cls, key: str, digest: str, user: UserInfo
    ) -> dict[str, Any] | None:
        """Recover one persisted request or report a settings conflict.

        Args:
            key: Account-scoped request identity.
            digest: Fingerprint of the confirmed source and settings.
            user: Current account used to recheck source permissions.

        Returns:
            Replayed task, a conflict, or `None` before a task is persisted.
        """
        task = await DownloadTask.get_or_none(creation_key=key)
        if task is None:
            return None
        if (task.creation_receipt or {}).get("digest") != digest:
            return {
                "created": False,
                "conflict": True,
                "message": "This request ID already submitted different settings; "
                "validate again for a new request.",
            }
        return await cls._task_result(task, user, replayed=True)

    @classmethod
    async def create_once(cls, body: DownloadTaskCreate, user) -> dict[str, Any]:
        """Submit one reviewed result once and return without waiting for it.

        Args:
            body: Stable request ID, reviewed result and settings including the
                transfer rule, optional reviewed ingestion binding and explicit
                confirmation.
            user: Current administrator; everything is rechecked before writing.

        Returns:
            Validation issues, an unconfirmed preview, a conflict for a reused
            request ID, an existing task with the same content, or the submitted
            task (the same one on every retry). A reviewed binding is applied first;
            if submission then fails, the kept binding is reported and a retry with
            the same request ID reuses it. An unconfirmed downloader response is
            recorded as `submit_unknown`, never resubmitted.

        Raises:
            ForbiddenException: If the account cannot submit downloads.
        """
        FlowGraphService.require_editor(user)
        key = hashlib.sha256(f"{user.id}:{body.request_id}".encode()).hexdigest()
        source = await open_source(body.resource)
        # requests without a transfer rule keep their original receipt digest
        unset = {
            field for field in ("sub_pattern", "sub_repl") if not getattr(body, field)
        }
        identity = {
            **body.model_dump(mode="json", exclude={"request_id", "confirmed", *unset}),
            "resource": {
                "workflow_id": body.resource.workflow_id,
                "resource_id": body.resource.resource_id,
                "link": hashlib.sha256(
                    (source or {}).get("link", body.resource.source).encode()
                ).hexdigest(),
            },
            "ingestion": body.ingestion.workflow_id if body.ingestion else None,
        }
        digest = hashlib.sha256(
            dumps(identity, option=orjson.OPT_SORT_KEYS)
        ).hexdigest()
        lock = _REQUEST_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                with submission_lock(f"one-time:{key}"):
                    return await cls._create_locked(body, user, key, digest)
            except LockTimeout:
                if replay := await cls._replay_request(key, digest, user):
                    return replay
                return {
                    "created": False,
                    "pending": True,
                    "request_id": str(body.request_id),
                    "message": "This submission is still in progress; recover it "
                    "later using the same request_id.",
                }

    @classmethod
    async def _create_locked(
        cls, body: DownloadTaskCreate, user, key: str, digest: str
    ) -> dict[str, Any]:
        """Replay or submit one request while holding its shared submission lock.

        Args:
            body: Reviewed request.
            user: Current administrator.
            key: Account-scoped request identity.
            digest: Fingerprint of the reviewed result and settings.

        Returns:
            Shared creation result described by `create_once`.
        """
        if replay := await cls._replay_request(key, digest, user):
            return replay
        workflow_id = body.ingestion.workflow_id if body.ingestion else None
        issues, review, request, driver = await cls._review_request(
            body, workflow_id, user
        )
        # the binding outcome below replaces the validation-time ingestion review
        review.pop("ingestion", None)
        try:
            result: dict[str, Any] = {
                "request_id": str(body.request_id),
                "download": review,
            }
            if request is not None and review.get("info_hash") != body.info_hash:
                issues.append(
                    {
                        "path": "info_hash",
                        "message": "The download content changed; validate it again "
                        "and confirm the new result.",
                    }
                )
            if (
                request is not None
                and request.identity is not None
                and (same := await cls._same_content(request.identity))
            ):
                result["existing_download"] = same
                issues.append(
                    {"path": "resource", "message": "Another task has this content."}
                )
            if issues or request is None or driver is None:
                return {"created": False, "valid": False, "issues": issues, **result}
            if not body.confirmed:
                return {"created": False, "confirmation_required": True, **result}
            binding = None
            if body.ingestion is not None and body.transfer_lib_id:
                # prepare the library before the download can complete
                binding = await MediaLibService.ensure_ingestion(
                    body.transfer_lib_id,
                    body.ingestion.workflow_id,
                    body.ingestion.revision,
                    user,
                )
                result["ingestion"] = binding
                if binding["binding"] not in {"added", "reused"}:
                    return {"created": False, "valid": False, **result}
            creation = {
                "creation_key": key,
                "creation_receipt": {
                    "digest": digest,
                    "download": review,
                    "ingest_workflow_id": workflow_id,
                },
            }
            try:
                task = await cls.add_request(
                    body.downloader_id, driver, request, creation=creation
                )
            except IntegrityError:
                # a concurrent retry or an independent task stored the same content
                if replay := await cls._replay_request(key, digest, user):
                    return {**result, **replay}
                same = await cls._same_content(request.identity)
                return {"created": False, "existing_download": same, **result}
            except Exception:
                if task := await DownloadTask.get_or_none(creation_key=key):
                    # the durable claim survives ambiguous responses and save errors
                    notify_download_changes()
                elif binding is not None:
                    logger.exception("Download submission failed after its binding")
                    return {
                        "created": False,
                        **result,
                        "submit_error": "The library binding was kept but the "
                        "download was not submitted; retry with the same request_id.",
                    }
                else:
                    raise
            result.update(await cls._task_result(task, user))
            if binding is not None:
                result["ingestion"] = binding
            return result
        finally:
            if driver is not None:
                await driver.close()

    @classmethod
    async def add_request(
        cls,
        downloader_id: int,
        driver: DownloaderDriver,
        request: DownloadRequest,
        *,
        creation: dict[str, Any] | None = None,
    ) -> DownloadTask:
        """Submit a normalized download request and notify synchronization.

        Callers wrapping this method in a transaction must also notify after their
        outer transaction commits.

        Args:
            downloader_id: Downloader ID.
            driver: Driver used for submission.
            request: Normalized download request.
            creation: Request identity fields stored with the task; `None` for
                editor and plan submissions.

        Returns:
            Persisted download task.
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
                return await cls._add_draft(
                    downloader_id, driver, draft, creation=creation
                )

        task = None
        if creation:
            # commit the request claim before RPC can have any remote effect
            task = await DownloadTask.create(
                **cls._task_values(downloader_id, request),
                magnet_link=request.link,
                state=DownloadState.SUBMIT_UNKNOWN,
                **creation,
            )
        try:
            snapshot = await driver.add(request)
        except Exception as error:
            method = "add_torrent" if request.torrent else "add_link"
            if task is not None and not _maybe_sent(error, driver, method):
                await task.delete()
            raise

        # save the download directory
        await DownloadDirService.upsert(request.directory)
        # create the download task
        state = DownloadState.PAUSED if request.paused else DownloadState.DOWNLOADING
        if task is not None:
            task.unique_id = snapshot.identity.remote_id
            task.state = state
            await task.save(update_fields=["unique_id", "state"])
        else:
            task = await DownloadTask.create(
                **cls._task_values(downloader_id, request),
                unique_id=snapshot.identity.remote_id,
                magnet_link=(
                    request.link
                    if identity and (identity.info_hash or identity.info_hash_v2)
                    else None
                ),
                state=state,
            )
        notify_download_changes()
        return task

    @classmethod
    async def _add_draft(
        cls,
        downloader_id: int,
        driver: DownloaderDriver,
        draft: DownloadDraft,
        *,
        creation: dict[str, Any] | None = None,
    ) -> DownloadTask:
        """Persist a driver draft and notify recovery before remote submission.

        Args:
            downloader_id: Owning downloader ID.
            driver: Driver used for remote submission.
            draft: Task and optional offline-job persistence draft.
            creation: Request identity fields stored with the task before the
                remote submission can become uncertain.

        Returns:
            Persisted task updated with its submission snapshot.
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
                **(creation or {}),
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
        notify_download_changes()

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
            notify_download_changes()
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
        notify_download_changes()
        await DownloadDirService.upsert(request.directory)
        return task

    @classmethod
    async def pause(cls, id: int):
        """Pause a download task and notify synchronization after local changes.

        Args:
            id: Download task ID.

        Returns:
            A deferred command for offline jobs, or `None` for immediate actions.
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
            notify_download_changes()

    @classmethod
    async def start(cls, id: int):
        """Start a download task and notify synchronization after local changes.

        Args:
            id: Download task ID.

        Returns:
            A deferred command for offline jobs, or `None` for immediate actions.
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
            notify_download_changes()

    @classmethod
    async def retry(cls, id: int):
        """Retry a download task and notify synchronization after local changes.

        Args:
            id: Download task ID.

        Returns:
            A deferred command for offline jobs, or `None` for immediate actions.
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
            notify_download_changes()

    @classmethod
    async def delete(cls, id: int, local: bool = False):
        """Delete a download task and notify synchronization after committing.

        Args:
            id: Download task ID.
            local: Whether to delete the local files. Defaults to `False`.

        Returns:
            A deferred command for offline jobs, or `None` for immediate actions.
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
            notify_download_changes()
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
        notify_download_changes()

    @staticmethod
    def permitted(capabilities: Collection[str], user: UserInfo) -> list[TaskAction]:
        """Select the task actions an account may request through review.

        Args:
            capabilities: Driver actions for the task's current state.
            user: Current account; only administrators delete tasks.

        Returns:
            Pause, resume, retry and delete actions in that order.
        """
        return [
            action
            for action in TaskAction
            if DownloadAction(action) in capabilities
            and (action != TaskAction.DELETE or user.role == UserRole.ADMIN)
        ]

    @classmethod
    async def control(cls, task: DownloadTask, user: UserInfo) -> _TaskControl:
        """Read the actions a task allows the current account now.

        Args:
            task: Stored task.
            user: Current account; only administrators delete tasks.

        Returns:
            Permitted actions, the owning downloader and driver, an offline job
            that defers actions to synchronization, the stage a retry restores,
            and a `revision` of the task's identity, state, pending transfer and
            driver actions. Progress, speeds and messages leave it unchanged.
        """
        downloader = await Downloader.get(id=task.downloader_id)
        driver = load_driver(decrypt_config(downloader.config))
        job = await OfflineDownloadJob.get_or_none(download_id=task.id)
        target = (
            retry_state(job.last_error_kind) if job and job.last_error_kind else None
        )
        capabilities = await driver.capabilities(
            DownloadIdentity.from_task(task), task.state, retry_target=target
        )
        facts = {
            "task_id": task.id,
            "downloader_id": task.downloader_id,
            "info_hash": task.info_hash,
            "info_hash_v2": task.info_hash_v2,
            "state": task.state,
            "deferred": job is not None,
            "retry_from": target,
            "transfer_lib_id": task.transfer_lib_id,
            "transfer_pending": task.transfer_pending,
            "capabilities": sorted(capabilities),
        }
        return _TaskControl(
            downloader=downloader,
            driver=driver,
            job=job,
            actions=cls.permitted(capabilities, user),
            retry_from=target,
            revision=hashlib.sha256(
                dumps(facts, option=orjson.OPT_SORT_KEYS)
            ).hexdigest()[:16],
        )

    @classmethod
    def action_request(
        cls,
        user_id: int,
        change: DownloadTaskAction,
        revision: str,
        nonce: bytes | None = None,
    ) -> UUID:
        """Issue a request ID that identifies one reviewed action and revision.

        Args:
            user_id: Reviewing account.
            change: Reviewed task, action and file option.
            revision: Reviewed task revision.
            nonce: Random half of an existing ID being checked; `None` issues one.

        Returns:
            A new ID, or the only ID this nonce can have for the same review.
        """
        nonce = nonce or os.urandom(8)
        digest = hashlib.sha256(
            dumps(change.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
        ).hexdigest()
        tag = hashlib.sha256(nonce + f"{user_id}:{digest}:{revision}".encode())
        return UUID(bytes=nonce + tag.digest()[:8])

    @staticmethod
    def _action_notes(
        change: DownloadTaskAction, task: DownloadTask, control: _TaskControl
    ) -> list[str]:
        """Explain what applying an available action does.

        Args:
            change: Reviewed action.
            task: Stored task.
            control: The task's current downloader actions.

        Returns:
            Consequences supported by the service and driver, in English.
        """
        deferred = control.job is not None
        if change.action == TaskAction.PAUSE:
            notes = [
                "Stops pulling files locally; the remote copy is kept."
                if deferred
                else "Asks the downloader to pause it; it stays paused until resumed."
            ]
        elif change.action == TaskAction.RESUME:
            notes = [
                "Continues pulling files locally."
                if deferred
                else "Asks the downloader to resume it."
            ]
        elif change.action == TaskAction.RETRY:
            notes = [
                f"Retries this task from its {control.retry_from or 'failed'} stage; "
                "nothing new is submitted."
            ]
        else:
            notes = ["Removes the task from Kaloscope and its downloader."]
            if task.state == DownloadState.SUBMITTING:
                notes.append("Deletion waits until the submission in progress ends.")
            if change.delete_files:
                notes.append(
                    "Also deletes the files this task pulled locally."
                    if deferred
                    else "Also deletes its downloaded files once the downloader "
                    "confirms the deletion."
                )
                if task.transfer_lib_id:
                    notes.append(
                        "Library files placed by hardlink, copy or move stay; "
                        "symlinks to deleted files break."
                    )
            else:
                notes.append(
                    "Downloaded files stay"
                    + ("; unfinished partial files are removed." if deferred else ".")
                )
            if task.transfer_pending:
                notes.append("Its pending library transfer will not run.")
            notes.append("Plans, plan history and library records are unchanged.")
        if deferred:
            notes.append(
                "Applying queues it for download synchronization, which skips it "
                "if the task state changes first."
            )
        return notes

    @classmethod
    async def _review_action(
        cls, change: DownloadTaskAction, user: UserInfo
    ) -> tuple[dict[str, Any], DownloadTask, _TaskControl]:
        """Check one task action against current permissions and capabilities.

        Args:
            change: Requested action.
            user: Current account.

        Returns:
            The review, the stored task and its current downloader actions.

        Raises:
            ForbiddenException: If a non-administrator asks to delete a task.
            NotFoundException: If the task does not exist.
        """
        if change.action == TaskAction.DELETE and user.role != UserRole.ADMIN:
            raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
        task = await DownloadTask.get_or_none(id=change.task_id)
        if task is None:
            raise NotFoundException
        control = await cls.control(task, user)
        issues = []
        if change.action not in control.actions:
            issues.append(
                {
                    "path": "action",
                    "message": f"{change.action} is unavailable while the task is "
                    f"{task.state}; use one of actions.",
                }
            )
        result: dict[str, Any] = {
            "valid": not issues,
            "issues": issues,
            "action": change.action,
            "delete_files": change.delete_files,
            "task_id": task.id,
            "name": task.name,
            "downloader_id": task.downloader_id,
            "downloader_name": control.downloader.name,
            "state": task.state,
            "percentage": task.percentage,
            "actions": control.actions,
            "dispatch": "deferred" if control.job is not None else "immediate",
            "revision": control.revision,
            "notes": [] if issues else cls._action_notes(change, task, control),
        }
        if change.action == TaskAction.RETRY and control.retry_from:
            result["retry_from"] = control.retry_from
        if task.transfer_lib_id:
            result["transfer_lib_id"] = task.transfer_lib_id
            # the name is shown only to accounts that can read the library
            with suppress(ForbiddenException, NotFoundException):
                library = await MediaLibService.get_accessible(
                    task.transfer_lib_id, user
                )
                result["transfer_lib_name"] = library.name
        if not issues:
            result["request_id"] = str(
                cls.action_request(user.id, change, control.revision)
            )
        return loads(dumps(result)), task, control

    @classmethod
    async def review_action(cls, change: DownloadTaskAction, user: UserInfo) -> dict:
        """Review one pause, resume, retry or deletion without performing it.

        Args:
            change: Target task, action and whether deletion removes files.
            user: Current account; deletion requires an administrator.

        Returns:
            The task's identity, stored state, permitted `actions`, whether the
            action is `immediate` or `deferred` to synchronization, its
            consequences, located issues, the task `revision` and, when valid, a
            `request_id` for this exact review.

        Raises:
            ForbiddenException: If a non-administrator asks to delete a task.
            NotFoundException: If the task does not exist.
        """
        result, _, _ = await cls._review_action(change, user)
        return result

    @classmethod
    async def apply_action(
        cls, body: DownloadTaskActionApply, user: UserInfo
    ) -> dict[str, Any]:
        """Dispatch a confirmed task action at most once and return at once.

        A receipt is claimed before dispatch, so retries read the recorded outcome
        instead of sending the action again, including after deletion.

        Args:
            body: Request ID, the exact reviewed action and revision, and the
                user's confirmation.
            user: Current account; permissions and capabilities are checked again.

        Returns:
            A `changed` or `deleted` stored outcome, a `queued` deferred action
            that synchronization applies later, `replayed` outcomes, or
            `conflict`, `pending`, `stale`, `confirmation_required`, located
            issues, an `absent` or `unavailable` task, a `failed` action that sent
            nothing, or an `uncertain` outcome to inspect rather than repeat.

        Raises:
            ForbiddenException: If a non-administrator asks to delete a task.
        """
        if body.change.action == TaskAction.DELETE and user.role != UserRole.ADMIN:
            raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
        nonce = body.request_id.bytes[:8]
        if (
            cls.action_request(user.id, body.change, body.revision, nonce)
            != body.request_id
        ):
            return {
                "applied": False,
                "conflict": True,
                "message": "This request_id was reviewed for another task, action "
                "or revision; review the action again.",
            }
        key = hashlib.sha256(f"{user.id}:{body.request_id}".encode()).hexdigest()
        lock = _TASK_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                with submission_lock(f"task-action:{key}"):
                    task = asyncio.ensure_future(cls._apply_locked(body, key, user))
                    try:
                        return await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # a cancelled caller never leaves a dispatch unrecorded
                        await task
                        raise
            except LockTimeout:
                receipt = await DownloadTaskReceipt.get_or_none(key=key)
                if receipt is not None and receipt.result is not None:
                    return await cls._replay_action(receipt, user)
                return {
                    "applied": False,
                    "pending": True,
                    "message": "Another call is applying this request; repeat the "
                    "same request later to read its outcome.",
                }

    @classmethod
    async def _task_state(cls, task: DownloadTask, user: UserInfo) -> dict[str, Any]:
        """Describe a task's current stored state and permitted actions.

        Args:
            task: Stored task.
            user: Current account.

        Returns:
            Stored state, progress, permitted actions and its download page.
        """
        control = await cls.control(task, user)
        return {
            "state": task.state,
            "percentage": task.percentage,
            "actions": control.actions,
            "path": DOWNLOAD_PATHS[
                "completed_downloads"
                if task.state == DownloadState.COMPLETED
                else "active_downloads"
            ],
        }

    @classmethod
    async def _replay_action(
        cls, receipt: DownloadTaskReceipt, user: UserInfo
    ) -> dict[str, Any]:
        """Read a recorded outcome under current permissions without dispatching.

        Args:
            receipt: Claimed request.
            user: Current account.

        Returns:
            The recorded outcome, or `uncertain` for a claim without one, with
            the task as it is now, or `None` once deleted.

        Raises:
            ForbiddenException: If a non-administrator replays a deletion.
        """
        if receipt.action == TaskAction.DELETE and user.role != UserRole.ADMIN:
            raise ForbiddenException(ErrorCode.PERMISSION_DENIED)
        task = await DownloadTask.get_or_none(id=receipt.task_id)
        return loads(
            dumps(
                {
                    **(
                        receipt.result
                        or {
                            "applied": None,
                            "outcome": "uncertain",
                            "action": receipt.action,
                            "task_id": receipt.task_id,
                            "message": TASK_UNCERTAIN,
                        }
                    ),
                    "replayed": True,
                    "task": await cls._task_state(task, user) if task else None,
                }
            )
        )

    @classmethod
    async def _apply_locked(
        cls, body: DownloadTaskActionApply, key: str, user: UserInfo
    ) -> dict[str, Any]:
        """Recheck, claim and dispatch one request while holding its lock.

        Args:
            body: Confirmed request.
            key: Account-scoped request identity.
            user: Current account.

        Returns:
            Shared outcome described by `apply_action`.
        """
        if receipt := await DownloadTaskReceipt.get_or_none(key=key):
            return await cls._replay_action(receipt, user)
        change = body.change
        base: dict[str, Any] = {"action": change.action, "task_id": change.task_id}
        try:
            review, task, control = await cls._review_action(change, user)
        except NotFoundException:
            if not await DownloadTask.exists(id=change.task_id):
                return {
                    "applied": False,
                    "outcome": "absent",
                    **base,
                    "message": "The task no longer exists; this request did nothing.",
                }
            raise
        base["name"] = task.name
        if review["revision"] != body.revision:
            return {
                "applied": False,
                **base,
                "stale": True,
                "message": "The task's state or available actions changed after the "
                "review; review it again.",
                "current": {name: review[name] for name in ("state", "actions")},
            }
        if not review["valid"]:
            return {
                "applied": False,
                **base,
                "valid": False,
                "issues": review["issues"],
            }
        if not body.confirmed:
            return {
                **review,
                "request_id": str(body.request_id),
                "applied": False,
                "confirmation_required": True,
            }
        failed = {
            "applied": False,
            **base,
            "failed": True,
            "message": "Nothing was sent; the same request may be repeated.",
        }
        try:
            # the claim commits before the action can have any remote effect
            await DownloadTaskReceipt.create(
                key=key, action=change.action, task_id=task.id
            )
        except Exception:
            logger.exception("Failed to claim a download task action")
            return failed
        try:
            result = await cls._dispatch(change, task, base, user)
        except Exception as error:
            logger.exception("Failed to apply a download task action")
            method = _TASK_METHODS.get(change.action)
            if control.job is not None or (
                method and not _maybe_sent(error, control.driver, method)
            ):
                # deferred actions and refused requests reached no downloader
                try:
                    await DownloadTaskReceipt.filter(key=key).delete()
                    return failed
                except Exception:
                    # a kept claim replays this failure instead of dispatching
                    result = {**failed, "message": "Nothing was sent; review again."}
            else:
                result = {
                    "applied": None,
                    "outcome": "uncertain",
                    **base,
                    "message": TASK_UNCERTAIN,
                }
        try:
            await DownloadTaskReceipt.filter(key=key).update(result=result)
        except Exception:
            # an unrecorded claim replays as uncertain, never as a new dispatch
            logger.exception("Failed to record a download task action")
        return {**result, "replayed": False}

    @classmethod
    async def _dispatch(
        cls,
        change: DownloadTaskAction,
        task: DownloadTask,
        base: dict[str, Any],
        user: UserInfo,
    ) -> dict[str, Any]:
        """Send one checked action through the shared task controls.

        Args:
            change: Confirmed action.
            task: Task as reviewed.
            base: Action, task ID and name shared by every outcome.
            user: Current account, for the actions a changed task now allows.

        Returns:
            The stored outcome of an immediate action, or a deferred action
            accepted for synchronization without an observed state change.
        """
        if change.action == TaskAction.PAUSE:
            command = await cls.pause(task.id)
        elif change.action == TaskAction.RESUME:
            command = await cls.start(task.id)
        elif change.action == TaskAction.RETRY:
            command = await cls.retry(task.id)
        else:
            command = await cls.delete(task.id, change.delete_files)
        path = DOWNLOAD_PATHS[
            "completed_downloads"
            if task.state == DownloadState.COMPLETED
            else "active_downloads"
        ]
        if command is not None:
            action, state = command
            try:
                Sanic.get_app().ctx.dl_syncer.publish(
                    task.id, action, state, local=change.delete_files
                )
            except Exception:
                if change.action != TaskAction.DELETE:
                    raise
                # the durable deletion deadline still reaches synchronization
                logger.warning("Deferred task deletion waits for the next sync")
            return {
                "applied": True,
                "outcome": "queued",
                **base,
                "state": state,
                "path": path,
                "message": "Queued for download synchronization while the task is "
                f"{state}; this is not completion. Read the task later.",
            }
        current = await DownloadTask.get_or_none(id=task.id)
        if change.action == TaskAction.DELETE:
            changed = current is None
        else:
            changed = current is not None and current.state != task.state
        if not changed:
            return {
                "applied": False,
                "outcome": "unavailable",
                **base,
                "state": current.state if current else None,
                "message": "The task changed before the action ran, so nothing was "
                "sent; read the task and review again.",
            }
        if current is None:
            return {
                "applied": True,
                "outcome": "deleted",
                **base,
                "files": "deleted" if change.delete_files else "kept",
                "path": path,
            }
        return {
            "applied": True,
            "outcome": "changed",
            **base,
            **await cls._task_state(current, user),
            "message": "Stored state after the downloader accepted it; "
            "synchronization keeps it current.",
        }

    @classmethod
    async def stats(cls) -> DownloadStats:
        """Get download statistics.

        Returns:
            Download statistics object.
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
    async def list_plans(cls, query: DownloadPlanQuery, user: UserInfo) -> dict:
        """Page plans within the account's workflow and transfer-library permissions.

        Args:
            query: Indexer and keyword filters, ordering and pagination from the plan
                page. A zero or absent indexer ID applies no indexer filter, and the
                keyword matches plan keywords case-insensitively as a substring.
            user: Current account with permissions loaded by `authorize`.

        Returns:
            The exact permission-filtered total and dumped plans with their indexer
            name, `running` flag, `schedule` state and any recorded first attempt.
            Scheduling comes from stored settings and never proves that an attempt
            ran or succeeded.
        """
        queries = []
        if user.role != UserRole.ADMIN:
            queries.append(Q(graph_id__in=user.perms.indexer_ids if user.perms else []))
            queries.append(
                Q(transfer_lib_id=None)
                | Q(transfer_lib_id__in=user.perms.media_lib_ids if user.perms else [])
            )
        if query.graph_id:
            queries.append(Q(graph_id=query.graph_id))
        if query.keyword:
            queries.append(Q(keyword__icontains=query.keyword))
        page = await DownloadPlan.page(*queries, **query.page_params)
        result = await cls.dump_page(page)
        # attach the graph name and running status for each plan
        graph_ids = {plan.graph_id for plan in page.items}
        graphs = {g.id: g for g in await FlowGraph.filter(id__in=graph_ids)}
        for plan, record in zip(result["items"], page.items, strict=True):
            graph = graphs.get(plan["graph_id"])
            plan["graph_name"] = graph.name if graph else None
            published = graph.state != GraphState.DRAFT if graph else False
            plan["running"] = published and not plan["inactive"]
            plan["schedule"] = record.schedule_state(published)
            if record.creation_receipt:
                first = record.creation_receipt["first_execution"]
                plan["first_execution"] = (
                    {"status": "waiting"}
                    if first["status"] == "queued" and not plan["running"]
                    else first
                )
        return result

    @classmethod
    async def search_indexer(cls, body: IndexerSearch, user: UserInfo) -> dict:
        """Preview one published search page using the normal workflow engine.

        Args:
            body: Selected indexer and explicit, bounded search inputs.
            user: Current administrator with workflow access.

        Returns:
            Bounded metadata, reported pagination and validation issues. This runs
            the indexer, which may make HTTP requests and write workflow logs, but
            does not create a plan or submit downloads. Failed searches are distinct
            from valid empty results; omitted fields are not validated samples.

        Raises:
            ForbiddenException: If the account cannot author plans or read the indexer.
            NotFoundException: If the indexer does not exist.
        """
        FlowGraphService.require_editor(user)
        graph = await FlowGraphService.get_accessible(body.workflow_id, user)
        result = {
            "workflow_id": graph.id,
            "workflow_name": graph.name,
            "query": body.model_dump(mode="json", exclude={"workflow_id"}),
        }
        issues = await cls._search_issues(graph, body.keyword, body.filters)
        if issues:
            return {
                **result,
                "status": "invalid_input",
                "issue_count": len(issues),
                "issues": issues[:10],
            }
        app = Sanic.get_app()
        auth = (await start_config(graph, "auth"))["auth"] or {}
        if (auth.get("login") or {}).get(
            "required"
        ) and await FlowGraphService.indexer_auth(
            graph.id, getattr(app.ctx, "cookies", None)
        ) is None:
            return {
                **result,
                "status": "login_required",
                "message": (
                    "Log in on the indexer's search page, then retry. Do not send "
                    "credentials in chat."
                ),
            }
        try:
            async with asyncio.timeout(10):
                output = await app.ctx.flow_engine.execute(
                    graph.id,
                    {
                        **(body.filters or {}),
                        "$start": "search_start",
                        "keyword": body.keyword,
                        "page_num": body.page_num,
                        "page_size": body.page_size,
                    },
                    repeatable=True,
                    recoverable=False,
                    asynchronous=False,
                )
        except TimeoutError:
            return {
                **result,
                "status": "timeout",
                "message": (
                    "Search exceeded 10 seconds; no automatic retry. Inspect workflow "
                    "logs before retrying."
                ),
            }
        except Exception:
            return {
                **result,
                "status": "failed",
                "message": "Search failed; inspect workflow logs. No plan was created.",
            }
        errors, _ = check_output(
            "search", output, {"page_num": body.page_num, "page_size": body.page_size}
        )
        if errors:
            return {
                **result,
                "status": "invalid_output",
                "issues_truncated": len(errors) > 10,
                "issues": [error.model_dump() for error in errors[:10]],
            }
        items = output["items"]
        total, pages = output.get("total"), output.get("totalPages")
        has_more = (
            body.page_num * body.page_size < total
            if total is not None
            else body.page_num < pages
            if pages is not None
            else None
        )
        result.update(
            status="ok",
            total=total,
            total_pages=pages,
            has_more=has_more,
            next_page=body.page_num + 1 if has_more else None,
            received_count=len(items),
            items=[],
            truncated=False,
            projection=(
                "Resource metadata only; links and extra fields omitted (sources "
                "are sealed). This preview is not a plan attempt or a "
                "downloadability check."
            ),
        )
        fields = ("id", "title", "size", "category", "uploader", "uploaded_at", "misc")
        for item in items[: body.page_size]:
            projected = {}
            for key in fields:
                value = item.get(key)
                if isinstance(value, str):
                    projected[key] = value[:256]
                    if len(value) > 256:
                        result["truncated"] = True
            link = item.get("link")
            if body.sources and isinstance(link, str) and projected.get("id"):
                # links can carry tracker passkeys, so only a sealed copy leaves here
                projected["source"] = await seal_source(
                    graph.id, projected["id"], projected.get("title"), link
                )
            result["items"].append(projected)
            if len(dumps(result)) > 7500:
                result["items"].pop()
                break
        result["returned_count"] = len(result["items"])
        result["omitted_count"] = len(items) - result["returned_count"]
        result["truncated"] |= result["omitted_count"] > 0
        return result

    @classmethod
    async def authoring_options(
        cls, workflow_id: int, user: UserInfo, page_num: int = 1, page_size: int = 20
    ) -> dict:
        """Read published search settings and a page of safe downloader choices.

        Args:
            workflow_id: Indexer selected from existing workflow queries.
            user: Current account with workflow permissions.
            page_num: One-based downloader page.
            page_size: Number of downloader choices, at most 50.

        Returns:
            Published settings, downloader capabilities and recent directories.
            Credentials, driver probes and search execution are excluded.
        """
        FlowGraphService.require_editor(user)
        graph = await FlowGraphService.get_accessible(workflow_id, user)
        page = await Downloader.page(
            page_num=page_num, page_size=min(page_size, 50), ordering="priority"
        )
        downloaders = []
        default_downloader = None
        for downloader in page.items:
            driver = load_driver(decrypt_config(downloader.config))
            supported = bool(
                driver.source_types & {DownloadSource.MAGNET, DownloadSource.TORRENT}
            )
            if default_downloader is None and supported:
                default_downloader = downloader.id
            downloaders.append(
                {
                    "id": downloader.id,
                    "name": downloader.name,
                    "source_types": sorted(driver.source_types),
                }
            )
            await driver.close()
        directories = (
            await DownloadDir.all()
            .order_by("-last_used")
            .limit(3)
            .values_list("path", flat=True)
        )
        if not directories:
            directories = [str(default_download_directory())]
        return {
            "workflow_id": graph.id,
            "workflow_name": graph.name,
            "published": graph.state != GraphState.DRAFT and bool(graph.definition),
            "search": (await start_config(graph, "search"))["search"],
            "only_preview": graph.only_preview(),
            "downloaders": {
                "items": downloaders,
                "total": page.total,
                "page_num": page_num,
                "page_size": min(page_size, 50),
                "has_more": page_num * min(page_size, 50) < page.total,
            },
            "directories": directories,
            "downloader_availability": (
                "Not probed; choices describe configured capabilities."
            ),
            "defaults": {
                "interval_num": 1,
                "interval_start": None,
                "interval_end": None,
                "batch_limit": 1,
                "total_limit": 1,
                "downloader_id": default_downloader,
                "dir": directories[0],
                "transfer_lib_id": None,
                "transfer_method": None,
            },
            "interval_unit": "hours",
            "timezone": "UTC",
            "creation_rules": (
                "One-time download of one chosen result (no plan): search_indexer "
                "with sources=true, let the user pick a result, then use only the "
                "downloader/dir/transfer defaults below with validate_download_task "
                "and, after final confirmation, create_download_task. Plans: "
                "Retain the user's title/keyword and supplied preferences. Ask them to "
                "choose a downloadable published indexer unless already specified. "
                "After selection, immediately search_indexer with that keyword. Then "
                "check possible overlaps once per title: list_media_items and "
                "list_download_plans without workflow_id (all indexers) using the core "
                "title; reuse earlier results. Matches are evidence, not identity: "
                "compare titles/years/media types/IDs, keep unknowns unknown, and say "
                "when reads were empty, denied, failed or have more pages. Ask one "
                "focused question when candidates imply different actions. Show "
                "results and overlaps, then ask about relevant declared filters (e.g. "
                "year/group), start/end, interval, per-attempt/total resource counts, "
                "downloader, directory and optional library transfer together. Show "
                "these defaults for omitted values; single-title defaults are one "
                "resource, not one episode. Ask for a total for ongoing/multi-title "
                "requests. Resolve a missing compatible downloader; list further pages "
                "if needed. Never invent or promise undeclared filters (year/group/"
                "season/episode); keyword alternatives require a new search. Re-search "
                "changed search inputs. Empty results need revised inputs or explicit "
                "agreement to wait for future resources. For ingestion into a movie/TV "
                "transfer library, follow get_library_ingestion rules. Validate and "
                "show ALL final settings/defaults, transfer preview and possible "
                "overlaps; the user may "
                "view them "
                "(navigate, get_download_plan) or confirm another plan. Never reject "
                "creation or change existing plans because of an overlap. Wait for "
                "user confirmation before creation. Set confirmed=true only after that "
                "approval; reuse request_id on retries. "
                "Preview is not the first attempt: creation queues a fresh real search "
                "and downloads, or waits for a future start/hourly check. Read the "
                "receipt separately; creation is not download success."
            ),
        }

    @classmethod
    async def _search_issues(
        cls, graph: FlowGraph, keyword: str, filters: dict | None
    ) -> list[dict[str, str]]:
        """Check published search inputs for both preview and plan creation.

        Args:
            graph: Permission-checked indexer.
            keyword: Explicit search keyword.
            filters: Declared filter values, without reserved boot parameters.

        Returns:
            Located configuration and input issues.
        """
        issues = []
        if (
            graph.category != GraphCategory.INDEXER
            or graph.state == GraphState.DRAFT
            or not graph.definition
        ):
            issues.append(
                {
                    "path": "graph_id",
                    "message": "Select an indexer with a published definition.",
                }
            )
        search = (await start_config(graph, "search"))["search"]
        if search is None or graph.only_preview():
            issues.append(
                {
                    "path": "graph_id",
                    "message": "Published indexer needs a downloadable search entry.",
                }
            )
        keyword_config = (search or {}).get("keyword") or {}
        configured = (search or {}).get("filters") or {}
        if not isinstance(keyword_config, dict) or not isinstance(configured, dict):
            issues.append(
                {
                    "path": "graph_id",
                    "message": "Published keyword/filters must be mappings.",
                }
            )
            keyword_config, configured = {}, {}
        if keyword_config.get("required") and not keyword.strip():
            issues.append(
                {"path": "keyword", "message": "This indexer requires a keyword."}
            )
        for key, value in (filters or {}).items():
            if (
                key.startswith("$")
                or key in {"keyword", "page_num", "page_size"}
                or key not in configured
            ):
                issues.append(
                    {
                        "path": f"filters.{key}",
                        "message": "Use declared filters, not reserved inputs.",
                    }
                )
                continue
            field = configured[key] if isinstance(configured[key], dict) else {}
            values = value if isinstance(value, list) else [value]
            if not field.get("type") or any(
                not isinstance(item, (str, int, float, bool)) for item in values
            ):
                issues.append(
                    {
                        "path": f"filters.{key}",
                        "message": "Match the declared filter's value type.",
                    }
                )
            elif field.get("type") in {"checkbox", "calendar-range"} and not isinstance(
                value, list
            ):
                issues.append(
                    {
                        "path": f"filters.{key}",
                        "message": "Checkbox and date-range values must be arrays.",
                    }
                )
            elif field.get("type") not in {"checkbox", "calendar-range"} and isinstance(
                value, list
            ):
                issues.append(
                    {
                        "path": f"filters.{key}",
                        "message": "This filter accepts one value.",
                    }
                )
            elif field.get("options") and any(
                str(item) not in field["options"] for item in values
            ):
                issues.append(
                    {"path": f"filters.{key}", "message": "Select a declared option."}
                )
            elif field.get("type") == "calendar-range" and len(values) != 2:
                issues.append(
                    {"path": f"filters.{key}", "message": "Supply start and end dates."}
                )
        return issues

    @classmethod
    async def validate_creation(
        cls, body: DownloadPlanCheck, user: UserInfo, *, search: bool = True
    ) -> dict:
        """Check a plan against the published indexer without executing it.

        Args:
            body: Editor fields, an optional explicit search-result sample and
                optional file names for the transfer preview.
            user: Current account; creation requires the same administrator role.
            search: Check the indexer, keyword and filters. An update that keeps
                its search inputs skips them.

        Returns:
            Located issues, normalized editor data, the transfer preview and
            first-execution semantics.

        Raises:
            ForbiddenException: If the account cannot create plans or read resources.
            NotFoundException: If the selected workflow does not exist.
        """
        FlowGraphService.require_editor(user)
        plan = body.plan
        graph = await FlowGraphService.get_accessible(plan.graph_id, user)
        issues = []
        warnings = [
            "Configuration and supplied samples only; no search, HTTP request "
            "or download was executed."
        ]
        if search:
            issues.extend(await cls._search_issues(graph, plan.keyword, plan.filters))
        if not plan.dir.strip():
            issues.append({"path": "dir", "message": "Supply a download directory."})
        for field in ("interval_start", "interval_end"):
            value = getattr(plan, field)
            if value is not None and value.tzinfo is None:
                issues.append(
                    {
                        "path": field,
                        "message": "Include a timezone offset; storage uses UTC.",
                    }
                )
        now = timezone.now()
        if plan.interval_end and plan.interval_end.tzinfo and plan.interval_end <= now:
            issues.append(
                {"path": "interval_end", "message": "End time must be in the future."}
            )
        downloader = await Downloader.get_or_none(id=plan.downloader_id)
        source_types = frozenset()
        if downloader is None:
            issues.append(
                {"path": "downloader_id", "message": "Select an existing downloader."}
            )
        else:
            driver = load_driver(decrypt_config(downloader.config))
            source_types = driver.source_types
            if (
                DownloadSource.MAGNET not in source_types
                and DownloadSource.TORRENT not in source_types
            ):
                issues.append(
                    {
                        "path": "downloader_id",
                        "message": "Download plans need magnet or torrent support.",
                    }
                )
            await driver.close()
        transfer = None
        if plan.transfer_lib_id:
            lib = await MediaLibService.get_accessible(plan.transfer_lib_id, user)
            if plan.transfer_method is None:
                issues.append(
                    {
                        "path": "transfer_method",
                        "message": "Choose a transfer method for the library.",
                    }
                )
            found, transfer = await transfer_preview(
                lib, plan.sub_pattern, plan.sub_repl, samples=body.transfer_samples
            )
            issues.extend(found)
        elif plan.sub_pattern:
            issues.append(
                {
                    "path": "transfer_lib_id",
                    "message": "Choose the library that receives renamed files.",
                }
            )
        ingestion = None
        if body.ingest_workflow_id is not None:
            if plan.transfer_lib_id:
                found, ingestion = await MediaLibService.ingestion_check(
                    plan.transfer_lib_id, body.ingest_workflow_id, user
                )
                issues.extend(found)
            else:
                issues.append(
                    {
                        "path": "transfer_lib_id",
                        "message": "Choose the library that ingests downloads.",
                    }
                )
        if body.sample is not None:
            errors, _ = check_output(
                "search", body.sample, {"page_num": 1, "page_size": plan.batch_limit}
            )
            issues.extend(
                {"path": f"sample.{error.path}", "message": error.message}
                for error in errors
            )
            for index, item in enumerate(
                body.sample.get("items", [])
                if isinstance(body.sample.get("items"), list)
                else []
            ):
                link = item.get("link") if isinstance(item, dict) else None
                # the authoring search check classifies links the same way
                kind = download_link_kind(link)
                valid = kind is not None
                if (
                    kind == "hash"
                    and downloader
                    and DownloadSource.MAGNET not in source_types
                ):
                    issues.append(
                        {
                            "path": f"sample.items.{index}.link",
                            "message": "Downloader cannot submit magnet/hash links.",
                        }
                    )
                if kind == "http":
                    warnings.append(
                        "HTTP links require real torrent content; the sample "
                        "does not prove that content or reachability."
                    )
                if not valid:
                    issues.append(
                        {
                            "path": f"sample.items.{index}.link",
                            "message": "Use a magnet, BT hash or HTTP(S) torrent URL.",
                        }
                    )
        else:
            warnings.append(
                "No search sample supplied; downloadable links are unchecked."
            )
        data = plan.model_dump(mode="json", exclude={"id"})
        for field in ("interval_start", "interval_end"):
            value = getattr(plan, field)
            if value is not None and value.tzinfo is not None:
                data[field] = value.astimezone(UTC).isoformat()
        return {
            "valid": not issues,
            "workflow_id": graph.id,
            "workflow_name": graph.name,
            "request_id": str(uuid4()),
            "plan": data,
            "issues": issues[:12],
            "warnings": list(dict.fromkeys(warnings))[:4],
            "first_execution": "scheduled"
            if plan.interval_start
            and plan.interval_start.tzinfo
            and plan.interval_start > now
            else "queued_after_creation",
            "interval_unit": "hours",
            "timezone": "UTC",
            **({"transfer": transfer} if transfer else {}),
            **({"ingestion": ingestion} if ingestion else {}),
        }

    @classmethod
    async def creation_result(
        cls, plan: DownloadPlan, user: UserInfo, *, replayed: bool = False
    ) -> dict:
        """Describe a persisted plan and its durable first-attempt receipt.

        Args:
            plan: Existing plan to describe.
            user: Current account whose workflow permissions are rechecked.
            replayed: Whether this is a repeated creation request.

        Returns:
            Current fields and first-attempt state, never download success. The
            recorded ingestion choice is rechecked for administrators; ordinary
            readers do not receive workflow configuration.
        """
        graph = await FlowGraphService.get_accessible(plan.graph_id, user)
        if plan.transfer_lib_id:
            await MediaLibService.get_accessible(plan.transfer_lib_id, user)
        receipt = plan.creation_receipt or {}
        first_execution = receipt.get("first_execution", {"status": "unavailable"})
        if first_execution["status"] == "queued" and (
            plan.inactive() or graph.state == GraphState.DRAFT
        ):
            first_execution = {"status": "waiting"}
        data = await cls.dump(plan)
        data.update(
            graph_name=graph.name,
            running=graph.state != GraphState.DRAFT and not plan.inactive(),
            first_execution=first_execution,
        )
        result = {
            "created": True,
            "plan_id": plan.id,
            "workflow_id": graph.id,
            "workflow_name": graph.name,
            "replayed": replayed,
            "plan": data,
            "first_execution": first_execution,
            "path": "/downloads/plans",
        }
        if (
            user.role == UserRole.ADMIN
            and plan.transfer_lib_id
            and (workflow_id := receipt.get("ingest_workflow_id"))
        ):
            with suppress(NotFoundException):
                _, review = await MediaLibService.ingestion_check(
                    plan.transfer_lib_id, workflow_id, user
                )
                result["ingestion"] = {
                    **review,
                    "binding": "configured"
                    if review["binding"] == "reuse"
                    else "missing",
                }
        return result

    @classmethod
    async def create_once(cls, body: DownloadPlanCreate, user: UserInfo) -> dict:
        """Persist one authorized plan and notify the existing download synchronizer.

        Args:
            body: Stable request ID, final editor settings and explicit confirmation.
            user: Current administrator; identity never comes from the request body.

        Returns:
            Validation errors, an unconfirmed preview, or the same plan on every
            retry. First execution is durable queued work; creation does not wait
            for remote download requests. A reviewed `ingestion` binding is reused
            or appended before the plan exists; a binding conflict or failure
            creates no plan, and a plan failure after binding reports both outcomes
            so a retry with the same request ID reuses the binding.

        Raises:
            ForbiddenException: If the account cannot create plans.
            BadRequestException: If one request ID is reused with different settings.
        """
        FlowGraphService.require_editor(user)
        key = hashlib.sha256(f"{user.id}:{body.request_id}".encode()).hexdigest()
        values = body.plan.model_dump(exclude={"id"})
        for field in ("interval_start", "interval_end"):
            if values[field] is not None and values[field].tzinfo is not None:
                values[field] = values[field].astimezone(UTC)
        workflow_id = body.ingestion.workflow_id if body.ingestion else None
        # requests without ingestion keep their original receipt digest
        identity = values if workflow_id is None else {**values, "ingest": workflow_id}
        digest = hashlib.sha256(
            dumps(identity, option=orjson.OPT_SORT_KEYS)
        ).hexdigest()
        existing = await DownloadPlan.get_or_none(creation_key=key)
        if existing is not None:
            if existing.creation_receipt["digest"] != digest:
                raise BadRequestException(ErrorCode.DOWNLOAD_PLAN_REQUEST_CONFLICT)
            return await cls.creation_result(existing, user, replayed=True)
        validation = await cls.validate_creation(
            DownloadPlanCheck(plan=body.plan, ingest_workflow_id=workflow_id), user
        )
        validation["request_id"] = str(body.request_id)
        if not validation["valid"]:
            return {"created": False, **validation}
        if not body.confirmed:
            return {"created": False, "confirmation_required": True, **validation}
        binding = None
        if body.ingestion is not None and body.plan.transfer_lib_id:
            # prepare the library before the new plan can queue its first attempt
            binding = await MediaLibService.ensure_ingestion(
                body.plan.transfer_lib_id,
                body.ingestion.workflow_id,
                body.ingestion.revision,
                user,
            )
            validation["ingestion"] = binding
            if binding["binding"] not in {"added", "reused"}:
                return {"created": False, **validation, "valid": False}
        try:
            plan = await DownloadPlan.create(
                **values,
                creation_key=key,
                creation_receipt={
                    "digest": digest,
                    "first_execution": {"status": "queued"},
                    **({"ingest_workflow_id": workflow_id} if workflow_id else {}),
                },
            )
        except Exception as error:
            if (
                isinstance(error, IntegrityError)
                and await DownloadPlan.filter(creation_key=key).exists()
            ):
                # a concurrent request committed the durable identity first
                return await cls.create_once(body, user)
            if binding is None:
                raise
            logger.exception("Plan creation failed after its ingestion binding")
            return {
                "created": False,
                **validation,
                "plan_error": "The library binding was kept but the plan was not "
                "created; retry with the same request_id.",
            }
        notify_download_changes()
        result = await cls.creation_result(plan, user)
        if binding is not None:
            result["ingestion"] = binding
        return result

    @classmethod
    async def get_creation(cls, plan_id: int, user: UserInfo) -> dict:
        """Read a plan's current configuration and first-attempt outcome.

        Args:
            plan_id: Plan identity, distinct from its workflow ID.
            user: Current account with workflow access.

        Returns:
            Permission-checked creation and execution facts.

        Raises:
            NotFoundException: If the plan no longer exists.
        """
        plan = await DownloadPlan.get_or_none(id=plan_id)
        if plan is None:
            raise NotFoundException
        return await cls.creation_result(plan, user)

    @classmethod
    async def find_creation(cls, request_id: UUID, user: UserInfo) -> dict:
        """Recover an editor submission using its account-scoped request identity.

        Args:
            request_id: Original validation or editor request ID.
            user: Current administrator reopening the editor.

        Returns:
            The existing plan, or `created: false` when no creation was committed.
        """
        FlowGraphService.require_editor(user)
        key = hashlib.sha256(f"{user.id}:{request_id}".encode()).hexdigest()
        plan = await DownloadPlan.get_or_none(creation_key=key)
        if plan is None:
            return {"created": False}
        return await cls.creation_result(plan, user, replayed=True)

    @classmethod
    def settings(cls, plan: DownloadPlan | DownloadPlanSettings) -> dict[str, Any]:
        """Read the editor settings that a plan change reviews and writes.

        Args:
            plan: Stored plan or proposed editor settings.

        Returns:
            JSON settings with the plan's indexer; offset times are UTC. Runtime
            counters, history and creation records are excluded.
        """
        values = {name: getattr(plan, name) for name in ("graph_id", *PLAN_SETTINGS)}
        for name in ("interval_start", "interval_end"):
            if values[name] is not None and values[name].tzinfo is not None:
                values[name] = values[name].astimezone(UTC)
        return loads(dumps(values))

    @classmethod
    def revision(cls, plan: DownloadPlan) -> str:
        """Fingerprint a plan's settings so a review detects later edits.

        Args:
            plan: Stored plan.

        Returns:
            Short digest of its indexer and editor settings. Attempts that only
            advance counters or history leave it unchanged.
        """
        return hashlib.sha256(
            dumps(cls.settings(plan), option=orjson.OPT_SORT_KEYS)
        ).hexdigest()[:16]

    @classmethod
    def change_request(
        cls,
        user_id: int,
        change: DownloadPlanChange,
        revision: str,
        nonce: bytes | None = None,
    ) -> UUID:
        """Issue a request ID that identifies one reviewed change and revision.

        Args:
            user_id: Reviewing account.
            change: Reviewed change; omitted and explicitly cleared fields differ.
            revision: Reviewed plan revision.
            nonce: Random half of an existing ID being checked; `None` issues one.

        Returns:
            A new ID, or the only ID this nonce can have for the same review.
        """
        nonce = nonce or os.urandom(8)
        digest = hashlib.sha256(
            dumps(
                change.model_dump(mode="json", exclude_unset=True),
                option=orjson.OPT_SORT_KEYS,
            )
        ).hexdigest()
        tag = hashlib.sha256(nonce + f"{user_id}:{digest}:{revision}".encode())
        return UUID(bytes=nonce + tag.digest()[:8])

    @classmethod
    async def plan_state(cls, plan: DownloadPlan) -> dict[str, Any]:
        """Describe a plan's current settings, recorded attempts and scheduling.

        Args:
            plan: Stored plan.

        Returns:
            JSON settings, `total_count`, `last_exec` and the `schedule` state
            derived from stored settings, not from an attempt.
        """
        graph = await FlowGraph.get_or_none(id=plan.graph_id)
        settings = cls.settings(plan)
        return loads(
            dumps(
                {
                    **{name: settings[name] for name in PLAN_SETTINGS},
                    "total_count": plan.total_count,
                    "last_exec": plan.last_exec,
                    "schedule": plan.schedule_state(
                        graph is not None and graph.state != GraphState.DRAFT
                    ),
                }
            )
        )

    @classmethod
    async def _review_change(
        cls, change: DownloadPlanChange, user: UserInfo
    ) -> tuple[dict[str, Any], DownloadPlan, dict[str, Any]]:
        """Check a plan change against current permissions and editor rules.

        Args:
            change: Requested update or deletion.
            user: Current account.

        Returns:
            The review, the stored plan and the changed settings an update writes.

        Raises:
            ForbiddenException: If the account cannot manage the plan's resources.
            NotFoundException: If the plan, or a library it names, does not exist.
        """
        FlowGraphService.require_editor(user)
        plan = await DownloadPlan.get_or_none(id=change.plan_id)
        if plan is None:
            raise NotFoundException
        graph = await FlowGraphService.get_accessible(plan.graph_id, user)
        if plan.transfer_lib_id:
            await MediaLibService.get_accessible(plan.transfer_lib_id, user)
        published = graph.state != GraphState.DRAFT
        before = cls.settings(plan)
        issues: list[dict[str, str]] = []
        notes: list[str] = []
        after: dict[str, Any] | None = None
        proposed = None
        writes: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        if change.action == PlanAction.UPDATE:
            provided = change.model_dump(include=set(PLAN_SETTINGS), exclude_unset=True)
            values = {**before, **provided}
            if values["transfer_lib_id"] is None:
                # the editor saves transfer options only together with a library
                values.update(
                    {
                        name: None
                        for name in ("transfer_method", "sub_pattern", "sub_repl")
                        if name not in provided
                    }
                )
            try:
                proposed = DownloadPlanSettings.model_validate(values)
            except ValidationError as exc:
                issues.extend(
                    {
                        # the only model-level rule orders the start and end
                        "path": ".".join(map(str, error["loc"])) or "interval_start",
                        "message": error["msg"].removeprefix("Value error, ")[:200],
                    }
                    for error in exc.errors(include_input=False, include_url=False)
                )
        if proposed is not None:
            after = cls.settings(proposed)
            search = any(after[name] != before[name] for name in ("keyword", "filters"))
            checked = await cls.validate_creation(
                DownloadPlanCheck(plan=proposed), user, search=search
            )
            issues.extend(checked["issues"])
            if "transfer" in checked:
                extra["transfer"] = checked["transfer"]
            for name in PLAN_SETTINGS:
                if after[name] != before[name]:
                    value = getattr(proposed, name)
                    if name in {"interval_start", "interval_end"} and value:
                        value = value.astimezone(UTC)
                    writes[name] = value
            if not writes and not issues:
                issues.append({"path": "action", "message": "Nothing would change."})
            if search and not issues:
                extra["search_preview"] = {
                    "workflow_id": graph.id,
                    "keyword": after["keyword"],
                    "filters": after["filters"],
                    "page_size": min(after["batch_limit"], 20),
                }
                notes.append(
                    "Search inputs changed: run search_indexer with search_preview "
                    "and show the results before confirming. Resources this plan "
                    "already handled stay skipped."
                )
            if writes.keys() & {
                "downloader_id",
                "dir",
                "transfer_lib_id",
                "transfer_method",
                "sub_pattern",
                "sub_repl",
            }:
                notes.append(
                    "Existing tasks keep their downloader, directory and transfer "
                    "settings; only new submissions use these."
                )
            if after["transfer_lib_id"] and "transfer_lib_id" in writes:
                notes.append("Library ingestion bindings are unchanged.")
        timing: dict[str, Any] = {"before": plan.schedule_state(published)}
        if change.action == PlanAction.DELETE:
            timing.update(
                after="deleted",
                rule="No further attempts; an attempt already running is not "
                "cancelled and may still submit one download before it stops.",
            )
            notes.append(
                "Deletes the plan and its deduplication history; its download "
                "tasks and downloaded files stay."
            )
        elif proposed is not None and not issues:
            future = DownloadPlan(
                interval_start=writes.get("interval_start", plan.interval_start),
                interval_end=writes.get("interval_end", plan.interval_end),
                total_limit=proposed.total_limit,
                total_count=plan.total_count,
            )
            timing["after"] = future.schedule_state(published)
            if plan.creation_key and plan.last_exec is None:
                timing["rule"] = (
                    "Its queued first attempt runs at the next check once active, "
                    "with these settings."
                )
            else:
                timing.update(
                    interval_elapses_at=(plan.last_exec or plan.created_at)
                    + timedelta(hours=proposed.interval_num),
                    rule="Hourly checks start an attempt while schedule is active "
                    "once interval_num hours have passed since the last attempt; "
                    "not an exact time.",
                )
            notes.append(
                "Applies from the next attempt; one already running keeps its "
                "earlier settings. Counters, history and tasks are kept."
            )
            if proposed.total_limit is not None and "total_limit" in writes:
                left = proposed.total_limit - plan.total_count
                notes.append(
                    f"At most {left} more downloads ({plan.total_count} of "
                    f"{proposed.total_limit} added)."
                    if left > 0
                    else f"total_limit {proposed.total_limit} is not above the "
                    f"{plan.total_count} already added, so no further attempts run "
                    "until it is raised or cleared."
                )
            if timing["after"] == "not_started":
                notes.append("Waits until interval_start.")
        if not published and change.action == PlanAction.UPDATE:
            notes.append("The indexer is unpublished; attempts wait until published.")
        shown_ids = [before, after or before]
        names = dict(
            await Downloader.filter(
                id__in=[settings["downloader_id"] for settings in shown_ids]
            ).values_list("id", "name")
        )
        libraries = dict(
            await MediaLib.filter(
                id__in=[settings["transfer_lib_id"] or 0 for settings in shown_ids]
            ).values_list("id", "name")
        )

        def shown(settings: dict[str, Any]) -> dict[str, Any]:
            """Add downloader and library names to settings for the summary.

            Args:
                settings: Reviewed settings.

            Returns:
                Editor settings without the indexer, plus display names.
            """
            return {
                **{name: settings[name] for name in PLAN_SETTINGS},
                "downloader_name": names.get(settings["downloader_id"]),
                "transfer_lib_name": libraries.get(settings["transfer_lib_id"]),
            }

        revision = cls.revision(plan)
        result = {
            "valid": not issues,
            "issues": issues[:12],
            "action": change.action,
            "plan_id": plan.id,
            "workflow_id": graph.id,
            "workflow_name": graph.name,
            "revision": revision,
            "before": shown(before),
            "after": shown(after) if after else None,
            "changes": [
                {"field": name, "before": before[name], "after": after[name]}
                for name in PLAN_SETTINGS
                if after and after[name] != before[name]
            ],
            "runtime": {
                "total_count": plan.total_count,
                "last_exec": plan.last_exec,
                "first_execution": (plan.creation_receipt or {}).get("first_execution"),
            },
            "timing": timing,
            "notes": notes,
            "interval_unit": "hours",
            "timezone": "UTC",
            **extra,
        }
        if not issues:
            result["request_id"] = str(cls.change_request(user.id, change, revision))
        return loads(dumps(result)), plan, writes

    @classmethod
    async def review_change(cls, change: DownloadPlanChange, user: UserInfo) -> dict:
        """Review a plan update or deletion without saving or executing anything.

        Args:
            change: Target plan and the settings to replace.
            user: Current account; only administrators change plans.

        Returns:
            Located issues, before/after settings and changes, recorded attempts,
            scheduling before and after, notes, a `search_preview` when search
            inputs change, the plan's `revision` and, when valid, a `request_id`
            for this exact review.

        Raises:
            ForbiddenException: If the account cannot manage the plan's resources.
            NotFoundException: If the plan, or a library it names, does not exist.
        """
        result, _, _ = await cls._review_change(change, user)
        return result

    @classmethod
    async def apply_change(cls, body: DownloadPlanApply, user: UserInfo) -> dict:
        """Apply a confirmed plan review once without starting an attempt.

        The change and its receipt commit together, so a repeated request reads
        the recorded outcome, including a deletion after the plan is gone.

        Args:
            body: Request ID, the exact reviewed change and revision, and the
                user's confirmation.
            user: Current account; permissions and settings are checked again.

        Returns:
            The applied outcome and current plan (`replayed` on retries), or
            `conflict`, `pending`, `stale`, `confirmation_required`, located
            issues, an `absent` plan, a `failed` change that saved nothing, or an
            `uncertain` outcome to inspect rather than repeat.

        Raises:
            ForbiddenException: If the account cannot manage the plan's resources.
            NotFoundException: If a library the change names no longer exists.
        """
        FlowGraphService.require_editor(user)
        nonce = body.request_id.bytes[:8]
        if (
            cls.change_request(user.id, body.change, body.revision, nonce)
            != body.request_id
        ):
            return {
                "applied": False,
                "conflict": True,
                "message": "This request_id was reviewed for another change or "
                "revision; review the change again.",
            }
        key = hashlib.sha256(f"{user.id}:{body.request_id}".encode()).hexdigest()
        lock = _PLAN_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            try:
                with submission_lock(f"plan-change:{key}"):
                    task = asyncio.ensure_future(cls._apply_locked(body, key, user))
                    try:
                        return await asyncio.shield(task)
                    except asyncio.CancelledError:
                        # a cancelled caller never splits a change from its receipt
                        await task
                        raise
            except LockTimeout:
                if receipt := await DownloadPlanReceipt.get_or_none(key=key):
                    return await cls._replay_change(receipt, user)
                return {
                    "applied": False,
                    "pending": True,
                    "message": "Another call is applying this request; repeat the "
                    "same request later to read its outcome.",
                }

    @classmethod
    async def _replay_change(
        cls, receipt: DownloadPlanReceipt, user: UserInfo
    ) -> dict[str, Any]:
        """Read a recorded outcome under current permissions.

        Args:
            receipt: Applied request.
            user: Current account.

        Returns:
            The recorded outcome with the plan as it is now, or `None` once deleted.

        Raises:
            ForbiddenException: If the account can no longer read its resources.
        """
        # a removed workflow or library cannot be revealed by its old outcome
        with suppress(NotFoundException):
            await FlowGraphService.get_accessible(receipt.graph_id, user)
        for change in receipt.result.get("changes", []):
            if change["field"] == "transfer_lib_id":
                for library_id in {change["before"], change["after"]} - {None}:
                    with suppress(NotFoundException):
                        await MediaLibService.get_accessible(library_id, user)
        plan = await DownloadPlan.get_or_none(id=receipt.plan_id)
        if plan and plan.transfer_lib_id:
            await MediaLibService.get_accessible(plan.transfer_lib_id, user)
        return {
            **receipt.result,
            "replayed": True,
            "plan": await cls.plan_state(plan) if plan else None,
        }

    @classmethod
    async def _apply_locked(
        cls, body: DownloadPlanApply, key: str, user: UserInfo
    ) -> dict[str, Any]:
        """Recheck and apply one request while holding its submission lock.

        Args:
            body: Confirmed request.
            key: Account-scoped request identity.
            user: Current account.

        Returns:
            Shared outcome described by `apply_change`.
        """
        if receipt := await DownloadPlanReceipt.get_or_none(key=key):
            return await cls._replay_change(receipt, user)
        change = body.change
        absent = {
            "applied": False,
            "outcome": "absent",
            "action": change.action,
            "plan_id": change.plan_id,
            "message": "The plan no longer exists; this request changed nothing.",
        }
        try:
            review, plan, writes = await cls._review_change(change, user)
        except NotFoundException:
            if not await DownloadPlan.exists(id=change.plan_id):
                return absent
            raise
        base = {
            name: review[name]
            for name in ("action", "plan_id", "workflow_id", "workflow_name")
        }
        stale = {
            "applied": False,
            **base,
            "stale": True,
            "message": "The plan's settings changed after the review; review it again.",
            "current": review["before"],
        }
        if review["revision"] != body.revision:
            return stale
        if not review["valid"]:
            return {
                "applied": False,
                **base,
                "valid": False,
                "issues": review["issues"],
            }
        if not body.confirmed:
            return {
                **review,
                "request_id": str(body.request_id),
                "applied": False,
                "confirmation_required": True,
            }
        try:
            async with in_transaction():
                # an editor or another review may have changed it since the check
                current = await DownloadPlan.get_or_none(id=plan.id)
                if current is None:
                    return absent
                if cls.revision(current) != body.revision:
                    return stale
                if change.action == PlanAction.DELETE:
                    await DownloadPlan.filter(id=plan.id).delete()
                    state = None
                else:
                    # counters and the last attempt advance independently
                    await DownloadPlan.filter(id=plan.id).update(**writes)
                    state = await cls.plan_state(await DownloadPlan.get(id=plan.id))
                result = {
                    "applied": True,
                    "outcome": "deleted"
                    if change.action == PlanAction.DELETE
                    else "updated",
                    **base,
                    "changes": review["changes"],
                    "plan": state,
                }
                await DownloadPlanReceipt.create(
                    key=key,
                    action=change.action,
                    plan_id=plan.id,
                    graph_id=base["workflow_id"],
                    result=result,
                )
        except Exception:
            logger.exception("Failed to apply a download plan change")
            try:
                receipt = await DownloadPlanReceipt.get_or_none(key=key)
            except Exception:
                return {
                    "applied": None,
                    "outcome": "uncertain",
                    **base,
                    "message": "The outcome could not be read; inspect the plan "
                    "instead of repeating the request.",
                }
            if receipt is not None:
                return await cls._replay_change(receipt, user)
            return {
                "applied": False,
                **base,
                "failed": True,
                "message": "Nothing was changed; the same request may be repeated.",
            }
        return {**result, "replayed": False}

    @classmethod
    async def upsert(cls, obj: DownloadPlanUpsert) -> DownloadPlan:
        """Create or update a download plan.

        Args:
            obj: Download plan data.

        Returns:
            Download plan instance.
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
