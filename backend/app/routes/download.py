import asyncio
from datetime import datetime
from uuid import UUID

from sanic import Blueprint, HTTPResponse, Request, empty, json
from sanic.log import logger
from sanic_ext import validate

from app.core.constants import ENCODING
from app.core.decorators import authorize
from app.core.dl.access import ADMIN_CHANGE, ADMIN_PREVIEW, READ_PLAN
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
    ReplacementDecision,
)
from app.models.user import UserRole
from app.services.download import (
    DownloaderService,
    DownloadPlanService,
    DownloadTaskService,
    default_download_directory,
)
from app.services.plan_replacement import PlanReplacementService
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
@authorize(role=UserRole.ADMIN)
async def list_downloaders(_) -> HTTPResponse:
    """List the download clients with their configurations and status.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    downloaders = await DownloaderService.dump_list(Downloader.all())
    for downloader in downloaders:
        # check if the downloader is up or down
        driver = load_driver(downloader["config"])
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
@authorize(role=UserRole.ADMIN)
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
@authorize(role=UserRole.ADMIN)
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
@authorize(role=READ_PLAN.role)
@validate(query=DownloadPlanQuery)
async def list_plans(request: Request, query: DownloadPlanQuery) -> HTTPResponse:
    """List plans within current workflow and transfer-library permissions.

    Args:
        request: Authenticated request with current resource permissions.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    return json(await DownloadPlanService.list_plans(query, request.ctx.user))


@download.post("/plan/validate")
@authorize(role=ADMIN_PREVIEW.role)
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
@authorize(role=ADMIN_CHANGE.role)
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
@authorize(role=READ_PLAN.role)
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
@authorize(role=ADMIN_PREVIEW.role)
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
@authorize(role=ADMIN_CHANGE.role)
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
@authorize(role=ADMIN_CHANGE.role)
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
@authorize(role=UserRole.ADMIN)
async def list_directories(_) -> HTTPResponse:
    """List the download directories.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    directories = await DownloadDir.all().order_by("-last_used").limit(3).values("path")
    if not directories:
        path = default_download_directory()
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)

        directories = [{"path": str(path.resolve())}]

    # attach the free space for each directory
    for directory in directories:
        directory["free"] = disk_usage(directory["path"]).free_space()
    return json(directories)


@download.get("/list")
@authorize(role=ADMIN_PREVIEW.role)
@validate(query=DownloadQuery)
async def list_tasks(request: Request, query: DownloadQuery) -> HTTPResponse:
    """List the download tasks.

    Args:
        request: Authenticated administrator request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    return json(await DownloadTaskService.list_tasks(query, request.ctx.user))


@download.post("/validate")
@authorize(role=UserRole.ADMIN)
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
@authorize(role=ADMIN_CHANGE.role)
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
@authorize(role=ADMIN_CHANGE.role)
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
@authorize(role=ADMIN_CHANGE.role)
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
@authorize(role=ADMIN_CHANGE.role)
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
@authorize(role=ADMIN_CHANGE.role)
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
@authorize(role=ADMIN_PREVIEW.role)
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


@download.get("/plan/replacement/<operation_id:uuid>")
@authorize(role=UserRole.ADMIN)
async def replacement_status(request: Request, operation_id: UUID) -> HTTPResponse:
    """Read an existing review for the signed-in administrator.

    Args:
        request: Authenticated session.
        operation_id: Review returned by the assistant or MCP.

    Returns:
        Frozen preview and current operation status.
    """
    return json(
        await PlanReplacementService.status(str(operation_id), request.ctx.user),
        headers={"Cache-Control": "no-store"},
    )


@download.post("/plan/replacement/<operation_id:uuid>/decision")
@authorize(role=UserRole.ADMIN)
@validate(json=ReplacementDecision)
async def decide_replacement(
    request: Request, operation_id: UUID, body: ReplacementDecision
) -> HTTPResponse:
    """Record an explicit decision from the review card.

    Args:
        request: Authenticated browser session.
        operation_id: Account-owned review.
        body: Displayed digest and the user's decision.

    Returns:
        Latest operation receipt including the replacement identity when committed.
    """
    return json(
        await PlanReplacementService.decide(
            str(operation_id),
            body.digest,
            approve=body.approve,
            user=request.ctx.user,
        ),
        headers={"Cache-Control": "no-store"},
    )
