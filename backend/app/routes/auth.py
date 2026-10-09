import uuid

from sanic import Blueprint, HTTPResponse, Request, empty, json
from sanic_ext import validate
from tortoise import timezone

from app.core.middleware import SessionHolder
from app.models.base import IDs
from app.models.user import User, UserInfo, UserLogin
from app.services.user import UserService
from app.utils.dict import remove, values

auth = Blueprint("auth", url_prefix="/auth")


@auth.post("/login")
@validate(form=UserLogin)
async def login(request: Request, body: UserLogin) -> HTTPResponse:
    """Login a user and return a token."""
    user = await UserService.login(body.username, body.password)
    token = uuid.uuid4().hex
    # store the user session in the shared context
    sessions = SessionHolder.get_sessions()
    sessions[token] = user
    # set the user and token in the request context
    request.ctx.user = user
    request.parsed_token = token
    return json({"token": token, "user": user.model_dump()})


@auth.post("/logout")
async def logout(request: Request) -> HTTPResponse:
    """Logout the current user."""
    sessions = SessionHolder.get_sessions()
    remove(sessions, request.token)
    return empty()


@auth.get("/current")
async def current(request: Request) -> HTTPResponse:
    """Get the current user's information."""
    result = request.ctx.user.model_dump() if hasattr(request.ctx, "user") else {}
    if result:
        user = await User.get_or_none(id=result["id"])
        preferences = user.preferences if user is not None else result["preferences"]
        result["preferences"] = UserService.DEFAULT_PREFERENCES | (
            preferences if isinstance(preferences, dict) else {}
        )
    return json(result)


@auth.get("/online")
async def online(request: Request) -> HTTPResponse:
    """Get the current user's sessions."""
    sessions = SessionHolder.get_sessions()
    # remove expired tokens
    now = timezone.now()
    remove(sessions, vfilter=lambda u: u.expire_at < now)
    # get the current user's sessions
    user: UserInfo = request.ctx.user
    current = values(
        sessions, vfilter=lambda u: u.id == user.id and u.login_id != user.login_id
    )
    current = [user, *sorted(current, key=lambda u: u.login_at, reverse=True)]
    return json([s.model_dump() for s in current])


@auth.post("/kickout")
@validate(json=IDs)
async def kickout(request: Request, body: IDs) -> HTTPResponse:
    """Kick out the specified user sessions."""
    user: UserInfo = request.ctx.user
    sessions = SessionHolder.get_sessions()
    remove(sessions, vfilter=lambda u: u.id == user.id and u.login_id in body.ids)
    return empty()
