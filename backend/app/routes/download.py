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
from app.core.dl.adapter import load_config
from app.core.dl.syncer import DLSyncer
from app.models.base import IDs
from app.models.download import (
    DownloadAdd,
    DownloadDel,
    DownloadDir,
    Downloader,
    DownloaderUpsert,
    DownloadPlan,
    DownloadPlanQuery,
    DownloadPlanUpsert,
    DownloadQuery,
    DownloadTask,
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

# subroutes for all download related operations
download = Blueprint("download", url_prefix="/download")


@download.get("/manager/list")
async def list_downloaders(_) -> HTTPResponse:
    """List the downloaders."""
    downloaders = await DownloaderService.dump_list(Downloader.all())
    for downloader in downloaders:
        # check if the downloader is up or down
        adapter = load_config(downloader["config"])
        if not adapter.methods.get("version"):
            downloader["status"] = "unknown"
            continue
        version = await adapter.version()
        downloader["status"] = "up" if version else "down"
        if version and downloader["version"] != version:
            downloader["version"] = version
            await Downloader.filter(id=downloader["id"]).update(version=version)
    return json(downloaders)


@download.post("/manager/sort")
@validate(json=IDs)
async def sort_downloaders(_, body: IDs) -> HTTPResponse:
    """Sort the downloaders."""
    await DownloaderService.update_priorities(body.ids)
    return empty()


@download.get("/manager/presets")
async def get_downloader_presets(_) -> HTTPResponse:
    """Get the downloader presets."""
    return json(await DownloaderService.get_presets())


@download.post("/manager/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=DownloaderUpsert)
async def upsert_downloader(_, body: DownloaderUpsert) -> HTTPResponse:
    """Create or update a downloader."""
    downloader = await DownloaderService.upsert(body)
    return json(await DownloaderService.dump(downloader))


@download.post("/manager/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_downloaders(_, body: IDs) -> HTTPResponse:
    """Delete the downloaders."""
    await Downloader.filter(id__in=body.ids).delete()
    return empty()


@download.get("/plan/list")
@validate(query=DownloadPlanQuery)
async def list_plans(_, query: DownloadPlanQuery) -> HTTPResponse:
    """List the download plans."""
    queries = []
    if query.graph_id:
        queries.append(Q(graph_id=query.graph_id))
    if query.keyword:
        queries.append(Q(keyword__icontains=query.keyword))
    page = await DownloadPlan.page(*queries, **query.page_params)
    result = await DownloadPlanService.dump_page(page)
    # attach the graph name and running status for each plan
    graph_ids = {job.graph_id for job in page.items}
    graphs = {g.id: g for g in await FlowGraph.filter(id__in=graph_ids)}
    for plan in result["items"]:
        graph = graphs.get(plan["graph_id"])
        plan["graph_name"] = graph.name if graph else None
        published = graph.state != GraphState.DRAFT if graph else False
        plan["running"] = published and not plan["inactive"]
    return json(result)


@download.post("/plan/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=DownloadPlanUpsert)
async def upsert_plan(_, body: DownloadPlanUpsert) -> HTTPResponse:
    """Create or update a download plan."""
    plan = await DownloadPlanService.upsert(body)
    return json(await DownloadPlanService.dump(plan))


@download.post("/plan/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_plans(_, body: IDs) -> HTTPResponse:
    """Delete the download plans."""
    await DownloadPlan.filter(id__in=body.ids).delete()
    return empty()


@download.get("/dir/list")
async def list_directories(_) -> HTTPResponse:
    """List the download directories."""
    directories = await DownloadDir.all().order_by("-last_used").limit(3).values("path")
    if not directories:
        # check if the path is readable
        def readable(p: Path) -> bool:
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
    """List the download tasks."""
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
    return json(await DownloadTaskService.dump_page(page))


@download.post("/validate")
async def valid_magnet_link(request: Request) -> HTTPResponse:
    """Validate a magnet link."""
    link = request.body.decode(ENCODING)
    return json((await standardize_magnet(link)) is not None)


@download.post("/add")
@authorize(role=UserRole.ADMIN)
@validate(form=DownloadAdd)
async def add_task(_, body: DownloadAdd) -> HTTPResponse:
    """Add a download task."""
    task = await DownloadTaskService.add(body)
    return json(await DownloadTaskService.dump(task))


@download.post("/pause")
@validate(json=IDs)
async def pause_tasks(_, body: IDs) -> HTTPResponse:
    """Pause the download tasks."""
    for id in body.ids:
        try:
            await DownloadTaskService.pause(int(id))
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to pause the download task: %s", id, exc_info=True)
    return empty()


@download.post("/start")
@validate(json=IDs)
async def start_tasks(_, body: IDs) -> HTTPResponse:
    """Start the download tasks."""
    for id in body.ids:
        try:
            await DownloadTaskService.start(int(id))
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to start the download task: %s", id, exc_info=True)
    return empty()


@download.post("/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=DownloadDel)
async def delete_tasks(_, body: DownloadDel) -> HTTPResponse:
    """Delete the download tasks."""
    for id in body.ids:
        try:
            await DownloadTaskService.delete(int(id), body.local)
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to delete the download task: %s", id, exc_info=True)
    return empty()


@download.get("/stats")
async def get_stats(request: Request):
    """Get the download statistics."""
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
