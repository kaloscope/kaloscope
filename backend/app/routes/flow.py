import time

from sanic import Blueprint, HTTPResponse, Request, empty, json, raw, text
from sanic.log import logger
from sanic_ext import validate
from tortoise.expressions import Q

from app.core.constants import APP_NAME
from app.core.cookiejar import SQLiteCookieJar
from app.core.decorators import authorize
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
)
from app.core.flow.context import AUTH_KEY, MANUAL_KEY
from app.core.flow.engine import FlowEngine
from app.core.flow.nodes.base import start_config
from app.models.base import IDs
from app.models.flow import (
    FlowGraph,
    FlowJob,
    FlowRepository,
    FlowTemplate,
    FlowVariable,
    GraphBasics,
    GraphCategory,
    GraphDocument,
    GraphImport,
    GraphQuery,
    GraphState,
    GraphWrite,
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
    """List the flow repositories.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    repos = await FlowRepository.all()
    return json(await FlowRepositoryService.dump_list(repos))


@flow.post("/repo/add")
@authorize(role=UserRole.ADMIN)
@validate(json=RepositoryAdd)
async def add_repository(_, body: RepositoryAdd) -> HTTPResponse:
    """Add a flow repository.

    Args:
        _: Unused request.
        body: Repository URL and checkout settings.

    Returns:
        Response containing the requested data.
    """
    repo = await FlowRepositoryService.add(body)
    return json(await FlowRepositoryService.dump(repo))


@flow.post("/repo/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_repositories(_, body: IDs) -> HTTPResponse:
    """Delete the flow repositories.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
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
    """List the flow templates.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
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
    """Reference a template to create a new flow graph.

    Args:
        _: Unused request.
        id: Template ID.
        body: Graph name and description.

    Returns:
        Response containing the requested data.
    """
    return json(await FlowTemplateService.reference_template(id, body.name))


@flow.post("/tmpl/<id:int>/copy")
@validate(json=GraphBasics)
async def copy_template(_, id: int, body: GraphBasics) -> HTTPResponse:
    """Copy a template to create a new flow graph.

    Args:
        _: Unused request.
        id: Template ID.
        body: Graph name and description.

    Returns:
        Response containing the requested data.
    """
    return json(await FlowTemplateService.copy_template(id, body.name))


@flow.get("/graph/name")
@validate(query=GraphQuery)
async def gen_graph_name(_, query: GraphQuery) -> HTTPResponse:
    """Generate a unique name for the flow graph.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    if not query.name:
        raise BadRequestException
    return text(await FlowGraphService.unique_name(query.name))


@flow.get("/graph/list")
@authorize()
@validate(query=GraphQuery)
async def list_graphs(request: Request, query: GraphQuery) -> HTTPResponse:
    """List the flow graphs.

    Args:
        request: Incoming request with session and application context.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    return json(await FlowGraphService.list_graphs(query, request.ctx.user))


@flow.post("/graph/upsert")
@authorize(role=UserRole.ADMIN)
@validate(form=GraphBasics)
async def upsert_graph(_, body: GraphBasics) -> HTTPResponse:
    """Create or update a flow graph.

    Args:
        _: Unused request.
        body: Graph name and description.

    Returns:
        Response containing the requested data.
    """
    graph = await FlowGraphService.upsert(body)
    return json(await FlowGraphService.dump(graph))


@flow.get("/graph/<id:int>")
@authorize()
async def get_graph_details(request: Request, id: int) -> HTTPResponse:
    """Get details of the flow graph.

    Args:
        request: Incoming request with the authenticated user's permissions.
        id: Graph ID.

    Returns:
        Response containing the requested data.
    """
    return json(await FlowGraphService.get_details(id, request.ctx.user))


@flow.get("/graph/<id:int>/logs")
@authorize()
async def get_graph_logs(request: Request, id: int) -> HTTPResponse:
    """Get execution logs of the flow graph.

    Args:
        request: Incoming request with the authenticated user's permissions.
        id: Graph ID.

    Returns:
        Response containing the requested data.
    """
    return json(await FlowLogService.get_accessible_logs(id, request.ctx.user))


@flow.post("/graph/<id:int>/save")
@authorize(role=UserRole.ADMIN)
@validate(json=GraphWrite)
async def save_graph(request: Request, id: int, body: GraphWrite) -> HTTPResponse:
    """Save flow graph.

    Args:
        request: Incoming request with session and application context.
        id: Graph ID.
        body: Draft contents and expected version.

    Returns:
        Response containing the requested data.
    """
    graph = await FlowGraphService.save_graph(id, body, request.ctx.user)
    return json(await FlowGraphService.dump(graph))


@flow.post("/graph/<id:int>/publish")
@authorize(role=UserRole.ADMIN)
@validate(json=GraphWrite)
async def publish_graph(request: Request, id: int, body: GraphWrite) -> HTTPResponse:
    """Publish the flow graph.

    Args:
        request: Incoming request with session and application context.
        id: Graph ID.
        body: Editor contents and expected version.

    Returns:
        Response containing the requested data.
    """
    graph = await FlowGraphService.publish_graph(id, body, request.ctx.user)
    return json(await FlowGraphService.dump(graph))


@flow.post("/graph/<id:int>/validate")
@authorize(role=UserRole.ADMIN)
@validate(json=GraphDocument)
async def validate_graph(
    request: Request, id: int, body: GraphDocument
) -> HTTPResponse:
    """Validate an editor document without changing stored workflow data.

    Args:
        request: Request carrying the authenticated account.
        id: Workflow ID.
        body: Proposed nodes and edges.

    Returns:
        Validation issues with node, field, and edge locations.
    """
    result = await FlowGraphService.validate_graph(id, body, request.ctx.user)
    return json(result.model_dump())


@flow.post("/graph/<id:int>/upgrade")
@authorize(role=UserRole.ADMIN)
async def upgrade_graph(request: Request, id: int) -> HTTPResponse:
    """Upgrade the flow graph.

    Args:
        request: Incoming request with session and application context.
        id: Graph ID.

    Returns:
        Response containing the requested data.
    """
    graph = await FlowGraphService.upgrade_graph(id, request.json)
    return json(await FlowGraphService.dump(graph))


@flow.post("/graph/<id:int>/retract")
@authorize(role=UserRole.ADMIN)
async def retract_graph(_, id: int) -> HTTPResponse:
    """Retract the flow graph.

    Args:
        _: Unused request.
        id: Graph ID.

    Returns:
        Empty response on success.
    """
    await FlowGraphService.retract_graph(id)
    return empty()


@flow.post("/graph/<id:int>/execute")
async def execute_graph(request: Request, id: int) -> HTTPResponse:
    """Execute the flow graph.

    Args:
        request: Incoming request with session and application context.
        id: Graph ID.

    Returns:
        Response containing the requested data.
    """
    engine: FlowEngine = request.app.ctx.flow_engine
    bootparams: dict = request.json or {}
    if await FlowGraph.filter(id=id, category=GraphCategory.INGEST).exists():
        if request.ctx.user.role != UserRole.ADMIN:
            raise ForbiddenException
        # interactive ingest execution only previews reading metadata candidates
        bootparams[MANUAL_KEY] = True
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
    """Delete the flow graphs.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
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
    """Export the flow graphs as a zip file.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Response containing the requested data.
    """
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
    """Import the flow graphs from a zip file.

    Args:
        _: Unused request.
        body: Graph definition to import.

    Returns:
        Response containing the requested data.
    """
    try:
        return json(await FlowGraphService.import_graphs(body.zip))
    except Exception as e:
        logger.error("Failed to import the flow graphs!", exc_info=True)
        raise KaloscopeException(ErrorCode.IMPORT_ITEMS_FAILED) from e


@flow.get("/node/schemas")
@authorize()
@validate(query=GraphQuery)
async def get_node_schemas(_, query: GraphQuery) -> HTTPResponse:
    """Get node schemas.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    return json(FlowGraphService.get_node_schemas(query.category))


@flow.get("/indexer/<id:int>/config")
async def get_indexer_config(_, id: int) -> HTTPResponse:
    """Get configuration of the indexer flow.

    Args:
        _: Unused request.
        id: Graph ID.

    Returns:
        Response containing the requested data.
    """
    return json(await start_config(id, "auth", "search", "board", "details"))


@flow.get("/indexer/<id:int>/auth")
async def get_indexer_auth(request: Request, id: int) -> HTTPResponse:
    """Get authentication of the indexer flow.

    Args:
        request: Incoming request with session and application context.
        id: Graph ID.

    Returns:
        Response containing the requested data.
    """
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
    """Clear the indexer authentication state.

    Args:
        request: Incoming request with session and application context.
        id: Graph ID.

    Returns:
        Empty response on success.
    """
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
    """Favorite the resource.

    Args:
        request: Incoming request with session and application context.
        id: Graph ID.
        body: Indexer and resource details.

    Returns:
        Empty response on success.
    """
    if body.rsrc is not None:
        user: UserInfo = request.ctx.user
        await UserFavoriteService.favorite(user.id, id, body)
    return empty()


@flow.post("/indexer/<id:int>/unfavorite")
@validate(json=IndexerResource)
async def unfavorite_resource(
    request: Request, id: int, body: IndexerResource
) -> HTTPResponse:
    """Remove the resource from favorites.

    Args:
        request: Incoming request with session and application context.
        id: Graph ID.
        body: Indexer and resource details.

    Returns:
        Empty response on success.
    """
    user: UserInfo = request.ctx.user
    await UserFavoriteService.unfavorite(user.id, id, body)
    return empty()


@flow.get("/job/list")
@authorize()
@validate(query=JobQuery)
async def list_jobs(request: Request, query: JobQuery) -> HTTPResponse:
    """List schedules within the account's workflow permissions.

    Args:
        request: Authenticated request with loaded workflow permissions.
        query: Pagination and schedule filters.

    Returns:
        Page of accessible schedules with their workflow names.
    """
    return json(await FlowJobService.list_jobs(query, request.ctx.user))


@flow.post("/job/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=JobUpsert)
async def upsert_job(_, body: JobUpsert) -> HTTPResponse:
    """Create or update a flow job.

    Args:
        _: Unused request.
        body: Workflow schedule settings.

    Returns:
        Response containing the requested data.
    """
    job = await FlowJobService.upsert(body)
    return json(await FlowJobService.dump(job))


@flow.post("/job/<id:int>/pause")
async def pause_job(request: Request, id: int) -> HTTPResponse:
    """Pause the job.

    Args:
        request: Incoming request with session and application context.
        id: Scheduled job ID.

    Returns:
        Empty response on success.
    """
    engine: FlowEngine = request.app.ctx.flow_engine
    await engine.pause_job(id)
    return empty()


@flow.post("/job/<id:int>/resume")
async def resume_job(request: Request, id: int) -> HTTPResponse:
    """Resume the job.

    Args:
        request: Incoming request with session and application context.
        id: Scheduled job ID.

    Returns:
        Empty response on success.
    """
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
    """Delete the job.

    Args:
        request: Incoming request with session and application context.
        id: Scheduled job ID.

    Returns:
        Empty response on success.
    """
    engine: FlowEngine = request.app.ctx.flow_engine
    await engine.delete_job(id)
    return empty()
