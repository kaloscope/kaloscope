from datetime import timedelta
from fnmatch import fnmatch
from multiprocessing.managers import DictProxy
from multiprocessing.synchronize import Lock

from sanic import HTTPResponse, Request, Sanic, json
from sanic.response import JSONResponse
from tortoise import timezone

from app.core.constants import ENCODING, SESSION_ID, URL_PREFIX
from app.core.exceptions import ErrorCode, UnauthorizedException
from app.models.user import UserInfo, UserSession

FRONTEND_NO_CACHE = "no-store, no-cache, must-revalidate, max-age=0"
FRONTEND_NO_CACHE_PATHS = {
    "/",
    "/index.html",
    "/404.html",
    "/service-worker.js",
    "/manifest.webmanifest",
    "/_app/version.json",
}


async def on_request(request: Request):
    """The middleware for request.

    Args:
        request: The request object.

    Raises:
        UnauthorizedException: If the request is unauthorized.
    """
    app = request.app
    # check if the request path is in the exclude paths
    if not request.path.startswith(URL_PREFIX):
        return
    for path in app.config.AUTH_EXCLUDE_PATHS:
        if fnmatch(request.path, URL_PREFIX + path):
            return
    # check if the token is in the request
    token = request.token or request.cookies.get(SESSION_ID)
    if not token:
        raise UnauthorizedException
    # check if the token is in the online users
    sessions: dict[str, UserInfo] = app.shared_ctx.sessions
    user = sessions.get(token)
    if user is None:
        raise UnauthorizedException
    # check if the token is expired
    now = timezone.now()
    if user.expire_at < now:
        sessions.pop(token, None)
        raise UnauthorizedException(ErrorCode.LOGIN_EXPIRED)
    # update the expire time
    user.expire_at = now + timedelta(hours=app.config.TOKEN_EXPIRATION_HOURS)
    user.last_activity = now
    sessions[token] = user
    # inject the user to the request context
    request.ctx.user = user


async def on_response(request: Request, response) -> HTTPResponse | None:
    """The middleware for response.

    Args:
        request: The request object.
        response: The response object.

    Returns:
        The wrapped response object.
    """
    if not isinstance(response, HTTPResponse):
        return None

    set_cache_headers(request, response)

    if 200 <= response.status < 300 and hasattr(request.ctx, "user"):
        # add the token to the response cookies
        user: UserInfo = request.ctx.user
        if token := request.token or request.cookies.get(SESSION_ID):
            response.add_cookie(
                SESSION_ID, token, expires=user.expire_at, secure=False, httponly=True
            )

    if request.server_path.startswith(URL_PREFIX) and response.status == 200:
        if isinstance(response, JSONResponse):
            data = response.raw_body
        elif response.content_type and response.content_type.startswith("text/plain"):
            data = response.body.decode(ENCODING) if response.body else ""
        else:
            return response
        # wrap the json or text response with a standard format
        ctx = request.ctx
        wrapped = json(
            {
                "request_id": request.id,
                "status": response.status,
                "message": ctx.message if hasattr(ctx, "message") else "",
                "data": data,
            }
        )
        wrapped.headers = response.headers
        return wrapped

    return response


def set_cache_headers(request: Request, response: HTTPResponse):
    """Prevent stale frontend shells from surviving deployments.

    Args:
        request: The request object.
        response: The response object.
    """
    if request.path.startswith(URL_PREFIX):
        return

    is_html = bool(
        response.content_type and response.content_type.startswith("text/html")
    )
    if request.path not in FRONTEND_NO_CACHE_PATHS and not is_html:
        return

    response.headers["Cache-Control"] = FRONTEND_NO_CACHE
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"


class SessionHolder:
    """A class to hold user sessions in memory and persist them to the database."""

    def __init__(self, app: Sanic):
        self._sessions: dict[str, UserInfo] = app.shared_ctx.sessions
        self._sessions_lock: Lock = app.shared_ctx.sessions_lock

    @staticmethod
    def get_sessions() -> DictProxy[str, UserInfo]:
        """Get the shared user sessions.

        Returns:
            The user sessions dictionary.
        """
        return Sanic.get_app().shared_ctx.sessions

    async def load(self):
        """Load user sessions from the database."""
        if self._sessions_lock.acquire(block=False):
            try:
                sessions = await UserSession.all()
                if not sessions:
                    return
                for session in sessions:
                    user_info = UserInfo.model_validate(session.user_info)
                    self._sessions[session.session_id] = user_info
                await UserSession.all().delete()
            finally:
                self._sessions_lock.release()

    async def save(self):
        """Save user sessions to the database."""
        if self._sessions_lock.acquire(block=False):
            try:
                if not self._sessions:
                    return
                await UserSession.bulk_create(
                    [
                        UserSession(session_id=session_id, user_info=user_info)
                        for session_id, user_info in self._sessions.items()
                    ]
                )
                self._sessions.clear()
            finally:
                self._sessions_lock.release()
