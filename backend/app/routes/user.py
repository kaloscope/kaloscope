from sanic import Blueprint, HTTPResponse, Request, empty, json
from sanic_ext import validate
from tortoise.expressions import Q

from app.core.decorators import authorize
from app.core.exceptions import content_error
from app.core.media.common import ContentError
from app.core.middleware import SessionHolder
from app.models.base import IDs
from app.models.user import (
    FavoriteQuery,
    HistoryEntry,
    HistoryQuery,
    Permissions,
    User,
    UserAvatar,
    UserCreate,
    UserHistory,
    UserInfo,
    UserPermission,
    UserPreference,
    UserPwd,
    UserQuery,
    UserRole,
)
from app.services.user import (
    UserFavoriteService,
    UserHistoryService,
    UserPermissionService,
    UserService,
)

user = Blueprint("user", url_prefix="/user")


@user.get("/count")
async def count(_) -> HTTPResponse:
    """Get count of users.

    Args:
        _: Unused request.

    Returns:
        Response containing the requested data.
    """
    return json(await User.all().count())


@user.get("/list")
@authorize(role=UserRole.ADMIN)
@validate(query=UserQuery)
async def list_users(_, query: UserQuery) -> HTTPResponse:
    """List the users.

    Args:
        _: Unused request.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
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
    """Create first admin user if no users exist.

    Args:
        _: Unused request.
        body: Account credentials.

    Returns:
        Empty response on success.
    """
    count = await User.all().count()
    if not count:
        await UserService.create(body.username, body.password, UserRole.ADMIN)
    return empty()


@user.post("/create")
@authorize(role=UserRole.ADMIN)
@validate(form=UserCreate)
async def create_user(_, body: UserCreate) -> HTTPResponse:
    """Create a new user.

    Args:
        _: Unused request.
        body: Account credentials.

    Returns:
        Empty response on success.
    """
    await UserService.create(body.username, body.password)
    return empty()


@user.post("/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=IDs)
async def delete_users(_, body: IDs) -> HTTPResponse:
    """Delete the users.

    Args:
        _: Unused request.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    await User.filter(id__in=body.ids).delete()
    return empty()


@user.post("/change_pwd")
@validate(form=UserPwd)
async def change_pwd(request: Request, body: UserPwd) -> HTTPResponse:
    """Change current user's password.

    Args:
        request: Incoming request with session and application context.
        body: Current and new passwords.

    Returns:
        Empty response on success.
    """
    user: UserInfo = request.ctx.user
    await UserService.change_pwd(user.username, body.cur_pwd, body.new_pwd)
    return empty()


@user.post("/change_avatar")
@validate(form=UserAvatar)
async def change_avatar(request: Request, body: UserAvatar) -> HTTPResponse:
    """Change current user's avatar.

    Args:
        request: Incoming request with session and application context.
        body: Selected avatar.

    Returns:
        Response containing the requested data.
    """
    user: UserInfo = request.ctx.user
    return json(await UserService.change_avatar(user.id, body.avatar))


@user.post("/update_pref")
@validate(json=UserPreference)
async def update_pref(request: Request, body: UserPreference) -> HTTPResponse:
    """Update current user's preference.

    Args:
        request: Incoming request with session and application context.
        body: Updated user preferences.

    Returns:
        Response containing the requested data.
    """
    user: UserInfo = request.ctx.user
    await UserService.update_pref(user.id, body)
    return json(body.model_dump())


@user.post("/favorite/list")
@authorize()
@validate(json=FavoriteQuery)
async def list_favorites(request: Request, body: FavoriteQuery) -> HTTPResponse:
    """List the current user's favorites.

    Args:
        request: Incoming request with session and application context.
        body: Favorite query.

    Returns:
        Response containing the requested data.
    """
    return json(await UserFavoriteService.list_page(body, request.ctx.user))


@user.get("/history/list")
@authorize()
@validate(query=HistoryQuery)
async def list_histories(request: Request, query: HistoryQuery) -> HTTPResponse:
    """List the current user's histories.

    Args:
        request: Incoming request with session and application context.
        query: Query filters and options.

    Returns:
        Response containing the requested data.
    """
    user: UserInfo = request.ctx.user
    return json(await UserHistoryService.get_page(user, query))


@user.post("/history/record")
@authorize()
@validate(json=HistoryEntry)
async def record_history(request: Request, body: HistoryEntry) -> HTTPResponse:
    """Record a user history entry.

    Args:
        request: Incoming request with session and application context.
        body: Resource and saved reading or playback progress.

    Returns:
        Empty response on success.
    """
    user: UserInfo = request.ctx.user
    try:
        await UserHistoryService.record(user, body)
    except ContentError as error:
        raise content_error(error) from error
    return empty()


@user.post("/history/delete")
@validate(json=IDs)
async def delete_histories(request: Request, body: IDs) -> HTTPResponse:
    """Delete the current user's histories.

    Args:
        request: Incoming request with session and application context.
        body: Record IDs to act on.

    Returns:
        Empty response on success.
    """
    user: UserInfo = request.ctx.user
    await UserHistory.filter(user_id=user.id, id__in=body.ids).delete()
    return empty()


@user.get("/<user_id:int>/permissions")
@authorize(role=UserRole.ADMIN)
async def get_permissions(_, user_id: int) -> HTTPResponse:
    """Get user's permissions.

    Args:
        _: Unused request.
        user_id: User ID.

    Returns:
        Response containing the requested data.
    """
    perms = await UserPermission.filter(user_id=user_id).values("rel_type", "rel_id")
    return json(list(perms))


@user.post("/<user_id:int>/permissions")
@authorize(role=UserRole.ADMIN)
@validate(json=Permissions)
async def update_permissions(_, user_id: int, body: Permissions) -> HTTPResponse:
    """Update user's permissions.

    Args:
        _: Unused request.
        user_id: User ID.
        body: Granted resource IDs.

    Returns:
        Empty response on success.
    """
    await UserPermissionService.update_permissions(user_id, body)
    return empty()
