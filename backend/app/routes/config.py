from sanic import Blueprint, HTTPResponse, empty, json
from sanic_ext import validate
from tortoise.expressions import Q

from app.core.decorators import authorize
from app.models.base import IDs
from app.models.general import ConfigQuery, ConfigUpsert, GlobalConfig
from app.models.user import UserRole
from app.services.config import ConfigService

# subroutes for all config related operations
config = Blueprint("config", url_prefix="/config")


@config.get("/list")
@validate(query=ConfigQuery)
async def list_configs(_, query: ConfigQuery) -> HTTPResponse:
    """List the global configs."""
    queries = []
    if query.key:
        queries.append(Q(key__icontains=query.key))
    page = await GlobalConfig.page(*queries, **query.page_params)
    return json(await ConfigService.dump_page(page))


@config.post("/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=ConfigUpsert)
async def upsert_config(_, body: ConfigUpsert) -> HTTPResponse:
    """Create or update a global config."""
    config = await ConfigService.upsert(body)
    return json(await ConfigService.dump(config))


@config.post("/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_configs(_, body: IDs) -> HTTPResponse:
    """Delete the global configs."""
    await GlobalConfig.filter(id__in=body.ids).delete()
    return empty()
