from enum import StrEnum, auto
from pathlib import Path

from sanic import NotFound, Request, SanicException, file
from sanic.log import Colors, logger
from sanic_ext.exceptions import ValidationError

# get the root path of the project
ROOT_PATH = Path(__file__).resolve().parents[3]


class ErrorCode(StrEnum):
    """The error codes for Kaloscope application."""

    # HTTP error codes
    INTERNAL_SERVER_ERROR = auto()
    UNAUTHORIZED = auto()
    BAD_REQUEST = auto()
    FORBIDDEN = auto()
    NOT_FOUND = auto()

    # custom error codes
    LOGIN_FAILED = auto()
    LOGIN_EXPIRED = auto()
    USER_NOT_FOUND = auto()
    FLOW_NOT_FOUND = auto()
    FLOW_NOT_PUBLISHED = auto()
    FLOW_ALREADY_RUNNING = auto()
    NAME_ALREADY_EXISTS = auto()
    USERNAME_ALREADY_EXISTS = auto()
    PATTERN_ALREADY_EXISTS = auto()
    SCAN_IN_PROGRESS = auto()
    INCORRECT_PASSWORD = auto()
    PERMISSION_DENIED = auto()
    DUPLICATE_DIRECTORY = auto()
    INVALID_YAML_CONFIG = auto()
    INFO_HASH_COLLISION = auto()
    GET_INFO_HASH_FAILED = auto()
    HTTP_REQUEST_FAILED = auto()
    EXPORT_ITEMS_FAILED = auto()
    IMPORT_ITEMS_FAILED = auto()
    FILE_NOT_EXISTS = auto()


class KaloscopeException(SanicException):
    """The base exception class for Kaloscope application."""

    status_code = 500
    quiet = False


class BadRequestException(KaloscopeException):
    """The exception class for bad requests."""

    status_code = 400
    message = ErrorCode.BAD_REQUEST


class UnauthorizedException(KaloscopeException):
    """The exception class for unauthorized access."""

    status_code = 401
    message = ErrorCode.UNAUTHORIZED
    quiet = True


class ForbiddenException(KaloscopeException):
    """The exception class for forbidden access."""

    status_code = 403
    message = ErrorCode.FORBIDDEN
    quiet = True


class NotFoundException(KaloscopeException):
    """The exception class for not found resources."""

    status_code = 404
    message = ErrorCode.NOT_FOUND
    quiet = True


async def error_handler(request: Request, exception: Exception):
    """Wrap all exceptions as `KaloscopeException` with specific error codes.

    See https://sanic.dev/en/guide/best-practices/exceptions.html for more details.

    Args:
        request: The request object.
        exception: The exception object.

    Raises:
        KaloscopeException: The wrapped exception.

    Returns:
        The 404.html page for all 404 errors.
    """
    if isinstance(exception, NotFound):
        # return the frontend 404.html for all 404 errors
        return await file(ROOT_PATH / "frontend/build/404.html")

    if request.route:
        # set the error format to JSON for all routes
        request.route.extra.error_format = "json"

    # wrap the exception as KaloscopeException
    if not isinstance(exception, KaloscopeException):
        if isinstance(exception, ValidationError):
            msg = exception.extra.get("exception") if exception.extra else None
            logger.error(f"Validation error: {Colors.RED}%s{Colors.END}", msg)
            exception = BadRequestException()
        elif isinstance(exception, SanicException):
            exception = KaloscopeException(exception.message, exception.status_code)
        else:
            exception = KaloscopeException(ErrorCode.INTERNAL_SERVER_ERROR)

    raise exception
