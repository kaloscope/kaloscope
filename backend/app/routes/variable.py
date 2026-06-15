from sanic import Blueprint, HTTPResponse, empty, json
from sanic_ext import validate
from tortoise.expressions import Q

from app.core.decorators import authorize
from app.models.base import IDs
from app.models.general import GlobalVariable, VariableQuery, VariableUpsert
from app.models.user import UserRole
from app.services.variable import VariableService

# subroutes for all variable related operations
variable = Blueprint("variable", url_prefix="/variable")


@variable.get("/list")
@validate(query=VariableQuery)
async def list_variables(_, query: VariableQuery) -> HTTPResponse:
    """List the global variables."""
    queries = []
    if query.key:
        queries.append(Q(key__icontains=query.key))
    page = await GlobalVariable.page(*queries, **query.page_params)
    result = await VariableService.dump_page(page)
    # hide encrypted values
    for var in result["items"]:
        if var["encrypted"]:
            var["value"] = ""
    return json(result)


@variable.post("/upsert")
@authorize(role=UserRole.ADMIN)
@validate(json=VariableUpsert)
async def upsert_variable(_, body: VariableUpsert) -> HTTPResponse:
    """Create or update a global variable."""
    variable = await VariableService.upsert(body)
    return json(await VariableService.dump(variable))


@variable.post("/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_variables(_, body: IDs) -> HTTPResponse:
    """Delete the global variables."""
    await GlobalVariable.filter(id__in=body.ids).delete()
    return empty()
