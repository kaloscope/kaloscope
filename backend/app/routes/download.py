import asyncio
import os
from datetime import datetime
from pathlib import Path

from sanic import Blueprint, HTTPResponse, Request, empty, json
from sanic.log import logger
from sanic_ext import validate
from tortoise.expressions import Q

from app.core.config import KaloscopeConfig
from app.core.constants import ENCODING
from app.core.decorators import authorize
from app.core.dl.config import load_driver
from app.core.dl.endpoint import Endpoint
from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.syncer import DLSyncer
from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.base import IDs
from app.models.download import (
    DownloadAdd,
    DownloadDel,
    DownloadDir,
    Downloader,
    DownloaderUpsert,
    DownloadPlan,
    DownloadPlanCheck,
    DownloadPlanCreate,
    DownloadPlanQuery,
    DownloadPlanUpsert,
    DownloadQuery,
)
from app.models.flow import FlowGraph, GraphState
from app.models.user import UserRole
from app.services.download import (
    DownloaderService,
    DownloadPlanService,
    DownloadTaskService,
)
from app.utils.bittorrent import standardize_magnet
from app.utils.disk import disk_usage

download = Blueprint("download", url_prefix="/download")


def _publish_task_action(
    request: Request,
    id: int,
    command,
    *,
    local: bool = False,
):
    """Publish a deferred driver action to the synchronizer owner.

    Args:
        request: Current Sanic request.
        id: Local `DownloadTask` ID.
        command: Optional `(action, expected_state)` service result.
        local: Whether deletion should include local files. Defaults to `False`.
    """
    if command is None:
        return
    action, state = command
    syncer: DLSyncer = request.app.ctx.dl_syncer
    syncer.publish(id, action, state, local=local)


@download.get("/manager/list")
async def list_downloaders(request: Request) -> HTTPResponse:
    """List the download clients.

    Args:
        request: Incoming request with session and application context.

    Returns:
        Response containing the requested data.
    """
    downloaders = await DownloaderService.dump_list(Downloader.all())
    for downloader in downloaders:
        # check if the downloader is up or down
        driver = load_driver(downloader["config"])
        if request.ctx.user.role is not UserRole.ADMIN:
            downloader.pop("config", None)
        if not driver.supports_version:
            continue
        try:
            version = await driver.version()
        except Exception:
            downloader["status"] = "down"
            continue
        downloader["status"] = "up" if version else "down"
        if version and downloader["version"] != version:
            downloader["version"] = version
            await Downloader.filter(id=downloader["id"]).update(version=version)
    return json(downloaders)


@download.post("/manager/sort")
@validate(json=IDs)
async def sort_downloaders(_, body: IDs) -> HTTPResponse:
    """Sort the download clients.

    Args:
        _: Unused request.
        body: Record IDs in display order.

    Returns:
        Empty response on success.
    """
    await DownloaderService.update_priorities(body.ids)
    return empty()


@download.get("/manager/presets")
async def get_downloader_presets(_) -> HTTPResponse:
    """Get downloader presets.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    return json(await DownloaderService.get_presets())


@download.post("/manager/openlist/tools")
@authorize(role=UserRole.ADMIN)
@validate(json=Endpoint)
async def get_openlist_tools(request: Request, body: Endpoint) -> HTTPResponse:
    """Get offline download tools exposed by an OpenList endpoint.

    Args:
        request: Incoming request with session and application context.
        body: Remote endpoint connection settings.

    Returns:
        Response containing the requested data.
    """
    try:
        tools = await OpenListClient(body, request.app.ctx.httpx).tools(
            request.args.get("path", "")
        )
    except OpenListClientError:
        raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED) from None
    return json(tools)


@download.post("/manager/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=DownloaderUpsert)
async def upsert_downloader(_, body: DownloaderUpsert) -> HTTPResponse:
    """Create or update a downloader.

    Args:
        _: Unused request.
        body: Download client settings.

    Returns:
        Response containing the requested data.
    """
    downloader = await DownloaderService.upsert(body)
    return json(await DownloaderService.dump(downloader))


@download.post("/manager/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_downloaders(_, body: IDs) -> HTTPResponse:
    """Delete the download clients.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    await Downloader.filter(id__in=body.ids).delete()
    return empty()


@download.get("/plan/list")
@authorize()
@validate(query=DownloadPlanQuery)
async def list_plans(request: Request, query: DownloadPlanQuery) -> HTTPResponse:
    """List plans within current workflow and transfer-library permissions.

    Args:
        request: Authenticated request with current resource permissions.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    queries = []
    user = request.ctx.user
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
    result = await DownloadPlanService.dump_page(page)
    # attach the graph name and running status for each plan
    graph_ids = {job.graph_id for job in page.items}
    graphs = {g.id: g for g in await FlowGraph.filter(id__in=graph_ids)}
    for plan, record in zip(result["items"], page.items, strict=True):
        graph = graphs.get(plan["graph_id"])
        plan["graph_name"] = graph.name if graph else None
        published = graph.state != GraphState.DRAFT if graph else False
        plan["running"] = published and not plan["inactive"]
        if record.creation_receipt:
            first = record.creation_receipt["first_execution"]
            plan["first_execution"] = (
                {"status": "waiting"}
                if first["status"] == "queued" and not plan["running"]
                else first
            )
    return json(result)


@download.post("/plan/validate")
@authorize(role=UserRole.ADMIN)
@validate(json=DownloadPlanCheck)
async def validate_plan(request: Request, body: DownloadPlanCheck) -> HTTPResponse:
    """Validate a plan without executing the indexer or downloader.

    Args:
        request: Authenticated administrator request.
        body: Proposed settings and optional sample.

    Returns:
        Shared validation result for the existing editor.
    """
    return json(await DownloadPlanService.validate_creation(body, request.ctx.user))


@download.post("/plan/create")
@authorize(role=UserRole.ADMIN)
@validate(json=DownloadPlanCreate)
async def create_plan(request: Request, body: DownloadPlanCreate) -> HTTPResponse:
    """Create once and queue the authorized first attempt without waiting for it.

    Args:
        request: Authenticated administrator request.
        body: Stable request identity and final settings.

    Returns:
        Created or replayed plan, or field validation errors.
    """
    return json(await DownloadPlanService.create_once(body, request.ctx.user))


@download.get("/plan/<id:int>")
@authorize()
async def get_plan(request: Request, id: int) -> HTTPResponse:
    """Read a plan and its stored first-attempt result.

    Args:
        request: Authenticated request with current workflow permissions.
        id: Plan identity.

    Returns:
        Shared permission-checked plan result.
    """
    return json(await DownloadPlanService.get_creation(id, request.ctx.user))


@download.get("/plan/request/<request_id:uuid>")
@authorize(role=UserRole.ADMIN)
async def find_plan(request: Request, request_id) -> HTTPResponse:
    """Recover the current account's plan after an uncertain editor response.

    Args:
        request: Authenticated administrator request.
        request_id: Original creation identity parsed from the route.

    Returns:
        The committed plan or a negative creation result.
    """
    return json(await DownloadPlanService.find_creation(request_id, request.ctx.user))


@download.post("/plan/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=DownloadPlanUpsert)
async def upsert_plan(_, body: DownloadPlanUpsert) -> HTTPResponse:
    """Create or update a download plan.

    Args:
        _: Unused request.
        body: Download plan fields.

    Returns:
        Response containing the requested data.
    """
    plan = await DownloadPlanService.upsert(body)
    return json(await DownloadPlanService.dump(plan))


@download.post("/plan/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_plans(_, body: IDs) -> HTTPResponse:
    """Delete the download plans.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    await DownloadPlan.filter(id__in=body.ids).delete()
    return empty()


@download.get("/dir/list")
async def list_directories(_) -> HTTPResponse:
    """List the download directories.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    directories = await DownloadDir.all().order_by("-last_used").limit(3).values("path")
    if not directories:
        # check if the path is readable
        def readable(p: Path) -> bool:
            """Check whether a download directory can be read.

            Args:
                p: Candidate download directory.

            Returns:
                Whether the path is a readable directory.
            """
            try:
                return p.is_dir() and os.access(p, os.R_OK)
            except OSError:
                return False

        # find the first readable path from the candidates
        path = None
        for p in [Path("/downloads"), Path.home() / "Downloads"]:
            if readable(p):
                path = p
                break

        # if no readable path is found, use the workspace downloads directory
        if path is None:
            path = Path(KaloscopeConfig.get()._workspace) / "downloads"
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)

        directories = [{"path": str(path.resolve())}]

    # attach the free space for each directory
    for directory in directories:
        directory["free"] = disk_usage(directory["path"]).free_space()
    return json(directories)


@download.get("/list")
@validate(query=DownloadQuery)
async def list_tasks(_, query: DownloadQuery) -> HTTPResponse:
    """List the download tasks.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    return json(await DownloadTaskService.list_tasks(query))


@download.post("/validate")
async def valid_magnet_link(request: Request) -> HTTPResponse:
    """Validate a download source.

    Args:
        request: Incoming request with session and application context.

    Returns:
        Response containing the requested data.
    """
    link = request.body.decode(ENCODING)
    downloader_id = request.args.get("downloader_id")
    if downloader_id is not None:
        try:
            id = int(downloader_id)
        except ValueError:
            return json(False)
        if id <= 0:
            return json(False)
        return json(await DownloadTaskService.validate_source(id, link))
    return json((await standardize_magnet(link)) is not None)


@download.post("/add")
@authorize(role=UserRole.ADMIN)
@validate(form=DownloadAdd)
async def add_task(_, body: DownloadAdd) -> HTTPResponse:
    """Add a download task.

    Args:
        _: Unused request.
        body: Download source, destination and options.

    Returns:
        Response containing the requested data.
    """
    task = await DownloadTaskService.add(body)
    return json(await DownloadTaskService.dump(task))


@download.post("/pause")
@validate(json=IDs)
async def pause_tasks(request: Request, body: IDs) -> HTTPResponse:
    """Pause the download tasks.

    Args:
        request: Incoming request with session and application context.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    for id in body.ids:
        try:
            task_id = int(id)
            command = await DownloadTaskService.pause(task_id)
            _publish_task_action(request, task_id, command)
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to pause the download task: %s", id, exc_info=True)
    return empty()


@download.post("/start")
@validate(json=IDs)
async def start_tasks(request: Request, body: IDs) -> HTTPResponse:
    """Start the download tasks.

    Args:
        request: Incoming request with session and application context.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    for id in body.ids:
        try:
            task_id = int(id)
            command = await DownloadTaskService.start(task_id)
            _publish_task_action(request, task_id, command)
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to start the download task: %s", id, exc_info=True)
    return empty()


@download.post("/retry")
@validate(json=IDs)
async def retry_tasks(request: Request, body: IDs) -> HTTPResponse:
    """Retry the download tasks.

    Args:
        request: Incoming request with session and application context.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    for id in body.ids:
        try:
            task_id = int(id)
            command = await DownloadTaskService.retry(task_id)
            _publish_task_action(request, task_id, command)
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to retry the download task: %s", id, exc_info=True)
    return empty()


@download.post("/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=DownloadDel)
async def delete_tasks(request: Request, body: DownloadDel) -> HTTPResponse:
    """Delete the download tasks.

    Args:
        request: Incoming request with session and application context.
        body: Task IDs and local file deletion option.

    Returns:
        Empty response on success.
    """
    for id in body.ids:
        try:
            task_id = int(id)
            command = await DownloadTaskService.delete(task_id, body.local)
            _publish_task_action(request, task_id, command, local=body.local)
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to delete the download task: %s", id, exc_info=True)
    return empty()


@download.get("/stats")
async def get_stats(request: Request):
    """Get download statistics.

    Args:
        request: Incoming request with session and application context.
    """
    syncer: DLSyncer = request.app.ctx.dl_syncer
    syncer.accelerate()
    try:
        start = datetime.now()
        response = await request.respond(
            headers={"Cache-Control": "no-cache"}, content_type="text/event-stream"
        )
        if response:
            while True:
                data = await DownloadTaskService.stats()
                await response.send(f"data: {data.model_dump_json()}\n\n")
                await asyncio.sleep(1)
                if (datetime.now() - start).total_seconds() > 60:
                    # stop the event stream every 60 seconds
                    break
    finally:
        syncer.decelerate()
