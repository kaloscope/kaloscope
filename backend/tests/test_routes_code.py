"""Unit tests for code formatting and template evaluation routes."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from sanic import HTTPResponse, Request, Sanic
from sanic.compat import Header
from sanic.models.protocol_types import TransportProtocol

from app.core.exceptions import ErrorCode, ForbiddenException, UnauthorizedException
from app.models.user import UserRole
from app.routes import code as routes

APP_NAME = "code-route-test"
ESCAPE = "{{ cycler.__init__.__globals__.os.getpid() }}"


@pytest.fixture(autouse=True)
def application():
    app = Sanic(APP_NAME, configure_logging=False)
    try:
        yield app
    finally:
        Sanic.unregister_app(app)


def request(body: dict, role: UserRole | None = UserRole.ADMIN) -> Request:
    """Create a real Sanic request for the template evaluation route.

    Args:
        body: JSON payload.
        role: Session role, or `None` for an unauthenticated request.

    Returns:
        Request suitable for the authorization and validation decorators.
    """
    result = Request(
        url_bytes=b"/_api/code/evaluate",
        headers=Header({"content-type": "application/json"}),
        version="1.1",
        method="POST",
        transport=cast(TransportProtocol, None),
        app=Sanic.get_app(APP_NAME),
    )
    result.body = json.dumps(body).encode()
    result.ctx.user = SimpleNamespace(role=role) if role else None
    return result


def evaluate(body: dict, role: UserRole | None = UserRole.ADMIN) -> str:
    """Call the template evaluation route and decode its text body.

    Args:
        body: JSON payload.
        role: Session role, or `None` for an unauthenticated request.

    Returns:
        Rendered template or the reported traceback.
    """
    callback = cast(Callable[[Request], Awaitable[HTTPResponse]], routes.evaluate)
    response = asyncio.run(callback(request(body, role)))
    assert response.body is not None
    return response.body.decode()


@pytest.mark.parametrize(
    ("role", "error", "code"),
    [
        (None, UnauthorizedException, ErrorCode.UNAUTHORIZED),
        (UserRole.USER, ForbiddenException, ErrorCode.FORBIDDEN),
    ],
)
def test_authorization(role, error, code):
    with pytest.raises(error) as exc_info:
        evaluate({"template": ESCAPE, "document": "{}"}, role)

    assert exc_info.value.message == code


def test_evaluate():
    result = evaluate(
        {
            "template": "{{ page.title }}",
            "document": "Kaloscope",
            "doc_name": "page.title",
        }
    )

    assert result == "Kaloscope"


def test_json():
    result = evaluate({"template": '{"a": {{ a }}}', "document": '{"a": 1}'})

    assert json.loads(result) == {"a": 1}
