from functools import wraps
from inspect import isawaitable

from sanic import Request

from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.models.user import (
    Permissions,
    PermType,
    User,
    UserInfo,
    UserPermission,
    UserRole,
)


async def require_browser_admin(user: UserInfo) -> None:
    """Require a browser session with a current administrator role.

    Args:
        user: Authenticated identity submitting or viewing the decision.

    Raises:
        ForbiddenException: If the identity uses an MCP credential or lacks the role.
    """
    if (
        getattr(user, "mcp_scope", None) is not None
        or user.role != UserRole.ADMIN
        or not await User.exists(id=user.id, role=UserRole.ADMIN)
    ):
        raise ForbiddenException


def authorize(role: UserRole | None = None):
    """Require a logged-in user and optionally a specific role.

    Args:
        role: Required role; `None` allows any logged-in user by default.

    Returns:
        Route decorator that checks the session and loads user permissions.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            if not hasattr(request.ctx, "user") or not request.ctx.user:
                raise UnauthorizedException

            user: UserInfo = request.ctx.user
            if role is not None:
                if user.role != role:
                    raise ForbiddenException
            elif user.role != UserRole.ADMIN:
                # attach permissions to the user info if no specific role is required
                indexer_ids = []
                lib_ids = []
                for perm in await UserPermission.filter(user_id=user.id):
                    if perm.rel_type == PermType.INDEXER:
                        indexer_ids.append(perm.rel_id)
                    elif perm.rel_type == PermType.MEDIA_LIB:
                        lib_ids.append(perm.rel_id)
                user.perms = Permissions(indexer_ids=indexer_ids, media_lib_ids=lib_ids)

            # call the main method and await if it's a coroutine
            response = func(request, *args, **kwargs)
            if isawaitable(response):
                response = await response
            return response

        return wrapper

    return decorator


def request_ctx(ctx: dict):
    """Attach context values before calling the route handler.

    Args:
        ctx: Values to attach to the request context.

    Returns:
        Route decorator that populates the request context.
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            for key, value in ctx.items():
                setattr(request.ctx, key, value)

            # call the main method and await if it's a coroutine
            response = func(request, *args, **kwargs)
            if isawaitable(response):
                response = await response
            return response

        return wrapper

    return decorator


def before(method: str):
    """Call a hook before the wrapped method.

    Args:
        method: Name of the instance hook to call without arguments.

    Returns:
        Method decorator that runs the hook before the wrapped call.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            getattr(self, method)()
            return func(self, *args, **kwargs)

        return wrapper

    return decorator


def after(method: str):
    """Call a hook after the wrapped method returns.

    Args:
        method: Name of the instance hook to call without arguments.

    Returns:
        Method decorator that runs the hook after a successful call.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            result = func(self, *args, **kwargs)
            getattr(self, method)()
            return result

        return wrapper

    return decorator
