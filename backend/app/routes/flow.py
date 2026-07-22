import time
from itertools import groupby

from sanic import Blueprint, HTTPResponse, Request, empty, json, raw, text
from sanic.log import logger
from sanic_ext import validate
from tortoise.expressions import Q

from app.core.constants import APP_NAME
from app.core.cookiejar import SQLiteCookieJar
from app.core.decorators import authorize
from app.core.exceptions import BadRequestException, ErrorCode, KaloscopeException
from app.core.flow.context import AUTH_KEY
from app.core.flow.engine import FlowEngine
from app.core.flow.nodes.base import Node, start_config
from app.models.base import IDs
from app.models.flow import (
    FlowGraph,
    FlowJob,
    FlowRepository,
    FlowTemplate,
    FlowVariable,
    GraphBasics,
    GraphImport,
    GraphQuery,
    GraphState,
    IndexerResource,
    JobQuery,
    JobUpsert,
    RepositoryAdd,
    TmplQuery,
)
from app.models.general import GlobalCookie
from app.models.user import UserInfo, UserRole
from app.services.flow import (
    FlowGraphService,
    FlowJobService,
    FlowLogService,
    FlowRepositoryService,
    FlowTemplateService,
)
from app.services.user import UserFavoriteService

flow = Blueprint("flow", url_prefix="/flow")


@flow.get("/repo/list")
async def list_repositories(_) -> HTTPResponse:
    """List the flow repositories."""
    repos = await FlowRepository.all()
    return json(await FlowRepositoryService.dump_list(repos))


@flow.post("/repo/add")
@authorize(role=UserRole.ADMIN)
@validate(json=RepositoryAdd)
async def add_repository(_, body: RepositoryAdd) -> HTTPResponse:
    """Add a flow repository."""
    repo = await FlowRepositoryService.add(body)
    return json(await FlowRepositoryService.dump(repo))


@flow.post("/repo/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_repositories(_, body: IDs) -> HTTPResponse:
    """Delete the flow repositories."""
    for id in body.ids:
        try:
            await FlowRepositoryService.delete(int(id))
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to delete the flow repository: %s", id, exc_info=True)
    return empty()


@flow.get("/tmpl/list")
@validate(query=TmplQuery)
async def list_templates(_, query: TmplQuery) -> HTTPResponse:
    """List the flow templates."""
    queries = []
    if query.name:
        queries.append(Q(name__icontains=query.name))
    if query.repo:
        queries.append(Q(repo_id=query.repo))
    if query.category:
        queries.append(Q(category=query.category))
    if query.newest is not None:
        queries.append(Q(newest=query.newest))
    page = await FlowTemplate.page(*queries, **query.page_params)
    return json(
        await FlowTemplateService.dump_page(
            page, exclude={"graphs": {"__all__": {"draft", "definition", "logs"}}}
        )
    )


@flow.post("/tmpl/<id:int>/ref")
@validate(json=GraphBasics)
async def reference_template(_, id: int, body: GraphBasics) -> HTTPResponse:
    """Reference a template to create a new flow graph."""
    return json(await FlowTemplateService.reference_template(id, body.name))


@flow.post("/tmpl/<id:int>/copy")
@validate(json=GraphBasics)
async def copy_template(_, id: int, body: GraphBasics) -> HTTPResponse:
    """Copy a template to create a new flow graph."""
    return json(await FlowTemplateService.copy_template(id, body.name))


@flow.get("/graph/name")
@validate(query=GraphQuery)
async def gen_graph_name(_, query: GraphQuery) -> HTTPResponse:
    """Generate a unique name for the flow graph."""
    if not query.name:
        raise BadRequestException
    return text(await FlowGraphService.unique_name(query.name))


@flow.get("/graph/list")
@authorize()
@validate(query=GraphQuery)
async def list_graphs(request: Request, query: GraphQuery) -> HTTPResponse:
    """List the flow graphs."""
    queries = []
    if query.name:
        queries.append(Q(name__icontains=query.name))
    if query.state:
        queries.append(Q(state=query.state))
    if query.states:
        queries.append(Q(state__in=query.states))
    if query.category:
        queries.append(Q(category=query.category))
    # filter the graphs by the user's permissions
    user: UserInfo = request.ctx.user
    if user.perms is not None:
        queries.append(Q(id__in=user.perms.indexer_ids))
    # list the graphs with pagination
    page = await FlowGraph.page(*queries, **query.page_params)
    result = await FlowGraphService.dump_page(
        page, exclude={"draft", "definition", "logs"}
    )
    # attach the newest template for each graph
    for graph in result["items"]:
        if tmpl := graph["tmpl"]:
            graph["newest_tmpl"] = await FlowTemplateService.get_newest(tmpl)
    return json(result)


@flow.post("/graph/upsert")
@authorize(role=UserRole.ADMIN)
@validate(form=GraphBasics)
async def upsert_graph(_, body: GraphBasics) -> HTTPResponse:
    """Create or update a flow graph."""
    graph = await FlowGraphService.upsert(body)
    return json(await FlowGraphService.dump(graph))


@flow.get("/graph/<id:int>")
async def get_graph_details(_, id: int) -> HTTPResponse:
    """Get the details of the flow graph."""
    graph = await FlowGraph.get(id=id)
    if isinstance(graph.draft, dict):
        graph.draft = Node.decompress(graph.draft)
    if isinstance(graph.definition, dict):
        graph.definition = Node.decompress(graph.definition)
    return json(await FlowGraphService.dump(graph))


@flow.get("/graph/<id:int>/logs")
async def get_graph_logs(_, id: int) -> HTTPResponse:
    """Get the execution logs of the flow graph."""
    return json(await FlowLogService.get_logs(id))


@flow.post("/graph/<id:int>/save")
@authorize(role=UserRole.ADMIN)
async def save_graph(request: Request, id: int) -> HTTPResponse:
    """Save the flow graph."""
    graph = await FlowGraphService.save_graph(id, request.json)
    return json(await FlowGraphService.dump(graph))


@flow.post("/graph/<id:int>/publish")
@authorize(role=UserRole.ADMIN)
async def publish_graph(request: Request, id: int) -> HTTPResponse:
    """Publish the flow graph."""
    graph = await FlowGraphService.publish_graph(id, request.json)
    return json(await FlowGraphService.dump(graph))


@flow.post("/graph/<id:int>/upgrade")
@authorize(role=UserRole.ADMIN)
async def upgrade_graph(request: Request, id: int) -> HTTPResponse:
    """Upgrade the flow graph."""
    graph = await FlowGraphService.upgrade_graph(id, request.json)
    return json(await FlowGraphService.dump(graph))


@flow.post("/graph/<id:int>/retract")
@authorize(role=UserRole.ADMIN)
async def retract_graph(_, id: int) -> HTTPResponse:
    """Retract the flow graph."""
    await FlowGraphService.retract_graph(id)
    return empty()


@flow.post("/graph/<id:int>/execute")
async def execute_graph(request: Request, id: int) -> HTTPResponse:
    """Execute the flow graph."""
    engine: FlowEngine = request.app.ctx.flow_engine
    bootparams: dict = request.json or {}
    kwargs = {
        "graph_id": id,
        "bootparams": bootparams,
        "repeatable": bootparams.pop("repeatable", True),
        "recoverable": bootparams.pop("recoverable", False),
        "recovery_id": bootparams.pop("recovery_id", None),
        "asynchronous": bootparams.pop("asynchronous", False),
    }
    return json(await engine.execute(**kwargs))


@flow.post("/graph/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_graphs(_, body: IDs) -> HTTPResponse:
    """Delete the flow graphs."""
    for id in body.ids:
        try:
            await FlowGraphService.delete(int(id))
        except Exception:
            if len(body.ids) == 1:
                raise
            logger.error("Failed to delete the flow graph: %s", id, exc_info=True)
    return empty()


@flow.post("/graph/export")
@validate(json=IDs)
async def export_graphs(_, body: IDs) -> HTTPResponse:
    """Export the flow graphs as a zip file."""
    zip = await FlowGraphService.export_graphs(body.ids)
    if zip is None:
        raise KaloscopeException(ErrorCode.EXPORT_ITEMS_FAILED)
    return raw(
        zip,
        content_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{APP_NAME}.zip"',
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        },
    )


@flow.post("/graph/import")
@validate(form=GraphImport)
async def import_graphs(_, body: GraphImport) -> HTTPResponse:
    """Import the flow graphs from a zip file."""
    try:
        return json(await FlowGraphService.import_graphs(body.zip))
    except Exception as e:
        logger.error("Failed to import the flow graphs!", exc_info=True)
        raise KaloscopeException(ErrorCode.IMPORT_ITEMS_FAILED) from e


@flow.get("/node/schemas")
@validate(query=GraphQuery)
async def get_node_schemas(_, query: GraphQuery) -> HTTPResponse:
    """Get the node schemas."""
    schemas = Node.schemas
    # filter the schemas by category
    if query.category:
        schemas = filter(
            lambda s: not s.categories or query.category in s.categories, schemas
        )
    # sort and group the schemas
    schemas = groupby(
        sorted(schemas, key=lambda s: (s.group.index, s.order)),
        key=lambda s: s.group,
    )
    return json({k.name.lower(): list(v) for k, v in schemas})


@flow.get("/indexer/<id:int>/config")
async def get_indexer_config(_, id: int) -> HTTPResponse:
    """Get the configuration of the indexer flow."""
    return json(await start_config(id, "auth", "search", "board", "details"))


@flow.get("/indexer/<id:int>/auth")
async def get_indexer_auth(request: Request, id: int) -> HTTPResponse:
    """Get the authentication of the indexer flow."""
    now = time.time()
    # check the auth variable
    var = await FlowVariable.get_or_none(
        Q(graph_id=id, key=AUTH_KEY) & (Q(expires__gte=now) | Q(expires__isnull=True))
    )
    if var is not None and isinstance(var.value, dict) and "name" in var.value:
        return json(var.value)
    # check the auth cookie
    config = await start_config(id, "auth")
    cookie = config.get("auth", {}).get("cookie")
    if isinstance(cookie, dict) and (domain := cookie.get("domain")):
        cookies: SQLiteCookieJar = request.app.ctx.cookies
        _cookie = cookies.get_cookie(
            domain=domain, path=cookie.get("path"), name=cookie.get("name")
        )
        if _cookie is not None:
            return json({"name": ""})
    return json(None)


@flow.post("/indexer/<id:int>/logout")
async def indexer_logout(request: Request, id: int) -> HTTPResponse:
    """Logout the indexer flow."""
    # delete the auth variable
    await FlowVariable.filter(graph_id=id, key=AUTH_KEY).delete()
    # delete the auth cookie
    config = await start_config(id, "auth")
    cookie = config.get("auth", {}).get("cookie")
    if isinstance(cookie, dict) and (domain := cookie.get("domain")):
        path = cookie.get("path")
        name = cookie.get("name")
        cookies: SQLiteCookieJar = request.app.ctx.cookies
        if cookies.get_cookie(domain, path, name):
            cookies.clear(domain, path, name)
        # delete the cookie from the database
        filter = Q(domain=domain)
        if path:
            filter &= Q(path=path)
        if name:
            filter &= Q(name=name)
        await GlobalCookie.filter(filter).delete()
    return empty()


@flow.post("/indexer/<id:int>/favorite")
@validate(json=IndexerResource)
async def favorite_resource(
    request: Request, id: int, body: IndexerResource
) -> HTTPResponse:
    """Favorite the resource."""
    if body.rsrc is not None:
        user: UserInfo = request.ctx.user
        await UserFavoriteService.favorite(user.id, id, body)
    return empty()


@flow.post("/indexer/<id:int>/unfavorite")
@validate(json=IndexerResource)
async def unfavorite_resource(
    request: Request, id: int, body: IndexerResource
) -> HTTPResponse:
    """Unfavorite the resource."""
    user: UserInfo = request.ctx.user
    await UserFavoriteService.unfavorite(user.id, id, body)
    return empty()


@flow.get("/job/list")
@validate(query=JobQuery)
async def list_jobs(_, query: JobQuery) -> HTTPResponse:
    """List the flow jobs."""
    queries = []
    if query.name:
        queries.append(Q(graph__name__icontains=query.name))
    if query.state:
        queries.append(Q(state=query.state))
    if query.trigger:
        queries.append(Q(trigger=query.trigger))
    page = await FlowJob.page(*queries, **query.page_params)
    result = await FlowJobService.dump_page(page)
    # attach the graph name for each job
    graph_ids = {job.graph_id for job in page.items}
    graphs = {g.id: g.name for g in await FlowGraph.filter(id__in=graph_ids)}
    for job in result["items"]:
        job["graph_name"] = graphs.get(job["graph_id"])
    return json(result)


@flow.post("/job/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=JobUpsert)
async def upsert_job(_, body: JobUpsert) -> HTTPResponse:
    """Create or update a flow job."""
    job = await FlowJobService.upsert(body)
    return json(await FlowJobService.dump(job))


@flow.post("/job/<id:int>/pause")
async def pause_job(request: Request, id: int) -> HTTPResponse:
    """Pause the job."""
    engine: FlowEngine = request.app.ctx.flow_engine
    await engine.pause_job(id)
    return empty()


@flow.post("/job/<id:int>/resume")
async def resume_job(request: Request, id: int) -> HTTPResponse:
    """Resume the job."""
    job = await FlowJob.get(id=id)
    graph = await FlowGraph.get_or_none(id=job.graph_id, state__not=GraphState.DRAFT)
    if graph is None:
        raise KaloscopeException(ErrorCode.FLOW_NOT_PUBLISHED)
    engine: FlowEngine = request.app.ctx.flow_engine
    await engine.resume_job(id)
    return empty()


@flow.post("/job/<id:int>/delete")
@authorize(role=UserRole.ADMIN)
async def delete_job(request: Request, id: int) -> HTTPResponse:
    """Delete the job."""
    engine: FlowEngine = request.app.ctx.flow_engine
    await engine.delete_job(id)
    return empty()
