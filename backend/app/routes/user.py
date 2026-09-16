from sanic import Blueprint, HTTPResponse, Request, empty, json
from sanic_ext import validate
from tortoise.expressions import Q

from app.core.decorators import authorize
from app.core.middleware import SessionHolder
from app.models.base import IDs, KVPair
from app.models.flow import FlowGraph
from app.models.media import MediaItem
from app.models.user import (
    FavoriteQuery,
    HistoryEntry,
    HistoryQuery,
    HistoryType,
    Permissions,
    User,
    UserAvatar,
    UserCreate,
    UserFavorite,
    UserHistory,
    UserInfo,
    UserPermission,
    UserPwd,
    UserQuery,
    UserRole,
)
from app.services.flow import FlowGraphService
from app.services.media import MediaItemService
from app.services.user import (
    UserFavoriteService,
    UserHistoryService,
    UserPermissionService,
    UserService,
)

user = Blueprint("user", url_prefix="/user")


@user.get("/count")
async def count(_) -> HTTPResponse:
    """Get the count of users."""
    return json(await User.all().count())


@user.get("/list")
@validate(query=UserQuery)
async def list_users(_, query: UserQuery) -> HTTPResponse:
    """List the users."""
    queries = []
    if query.username:
        queries.append(Q(username__icontains=query.username))
    page = await User.page(*queries, **query.page_params)
    result = await UserService.dump_page(page)
    # attach the last activity for each online user
    sessions = SessionHolder.get_sessions()
    activities = {
        u.id: u.last_activity
        for u in sorted(sessions.values(), key=lambda u: u.last_activity)
    }
    for user in result["items"]:
        user["last_activity"] = activities.get(user["id"])
    return json(result)


@user.post("/create_admin")
@validate(form=UserCreate)
async def create_admin(_, body: UserCreate) -> HTTPResponse:
    """Create the first admin user if no users exist."""
    count = await User.all().count()
    if not count:
        await UserService.create(body.username, body.password, UserRole.ADMIN)
    return empty()


@user.post("/create")
@authorize(role=UserRole.ADMIN)
@validate(form=UserCreate)
async def create_user(_, body: UserCreate) -> HTTPResponse:
    """Create a new user."""
    await UserService.create(body.username, body.password)
    return empty()


@user.post("/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_users(_, body: IDs) -> HTTPResponse:
    """Delete the users."""
    await User.filter(id__in=body.ids).delete()
    return empty()


@user.post("/change_pwd")
@validate(form=UserPwd)
async def change_pwd(request: Request, body: UserPwd) -> HTTPResponse:
    """Change current user's password."""
    user: UserInfo = request.ctx.user
    await UserService.change_pwd(user.username, body.cur_pwd, body.new_pwd)
    return empty()


@user.post("/change_avatar")
@validate(form=UserAvatar)
async def change_avatar(request: Request, body: UserAvatar) -> HTTPResponse:
    """Change current user's avatar."""
    user: UserInfo = request.ctx.user
    return json(await UserService.change_avatar(user.id, body.avatar))


@user.post("/update_pref")
@validate(json=KVPair)
async def update_pref(request: Request, body: KVPair) -> HTTPResponse:
    """Update current user's preference."""
    user: UserInfo = request.ctx.user
    await UserService.update_pref(user.id, body)
    return json(body.model_dump())


@user.post("/favorite/list")
@validate(json=FavoriteQuery)
async def list_favorites(request: Request, body: FavoriteQuery) -> HTTPResponse:
    """List the current user's favorites."""
    user: UserInfo = request.ctx.user
    queries = [Q(user_id=user.id)]
    if body.indexer_id:
        queries.append(Q(indexer_id=body.indexer_id))
    if body.rsrc_ids:
        queries.append(Q(rsrc_id__in=body.rsrc_ids))
    page = await UserFavorite.page(*queries, **body.page_params)
    return json(await UserFavoriteService.dump_page(page))


@user.get("/history/list")
@validate(query=HistoryQuery)
async def list_histories(request: Request, query: HistoryQuery) -> HTTPResponse:
    """List the current user's histories."""
    user: UserInfo = request.ctx.user
    # clean expired history records
    rel_type = query.rel_type
    await UserHistoryService.clean_expired(user.id, rel_type)
    # list the histories with pagination
    page = await UserHistory.page(
        user_id=user.id, rel_type=rel_type, **query.page_params
    )
    result = await UserHistoryService.dump_page(page)
    # attach related data based on the history type
    for his in result["items"]:
        if rel_id := his["rel_id"]:
            if rel_type == HistoryType.SEARCH:
                graph = await FlowGraph.get_or_none(id=rel_id)
                if graph is not None:
                    graph = await FlowGraphService.dump(
                        graph, exclude={"draft", "definition", "logs"}
                    )
                his["graph"] = graph
            elif rel_type == HistoryType.VIDEO:
                media = await MediaItem.get_or_none(id=rel_id)
                if media is not None:
                    media = await MediaItemService.dump(media, exclude={"children"})
                his["media"] = media
    return json(result)


@user.post("/history/record")
@validate(json=HistoryEntry)
async def record_history(request: Request, body: HistoryEntry) -> HTTPResponse:
    """Record a user history entry."""
    user: UserInfo = request.ctx.user
    await UserHistoryService.record(user.id, body)
    return empty()


@user.post("/history/delete")
@validate(json=IDs)
async def delete_histories(request: Request, body: IDs) -> HTTPResponse:
    """Delete the current user's histories."""
    user: UserInfo = request.ctx.user
    await UserHistory.filter(user_id=user.id, id__in=body.ids).delete()
    return empty()


@user.get("/<user_id:int>/permissions")
async def get_permissions(_, user_id: int) -> HTTPResponse:
    """Get the user's permissions."""
    perms = await UserPermission.filter(user_id=user_id).values("rel_type", "rel_id")
    return json(list(perms))


@user.post("/<user_id:int>/permissions")
@authorize(role=UserRole.ADMIN)
@validate(json=Permissions)
async def update_permissions(_, user_id: int, body: Permissions) -> HTTPResponse:
    """Update the user's permissions."""
    await UserPermissionService.update_permissions(user_id, body)
    return empty()
