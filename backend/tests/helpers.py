"""Shared setup helpers for backend tests."""

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import cast

from sanic import HTTPResponse, Request
from tortoise import Tortoise


@asynccontextmanager
async def memory_db(db_url: str = "sqlite://:memory:") -> AsyncGenerator[None]:
    """Provide a fresh application schema without starting application resources.

    Args:
        db_url: Database to initialize; defaults to a private in-memory SQLite.

    Yields:
        Control while the database is available; connections close afterwards.
    """
    await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        yield
    finally:
        await Tortoise.close_connections()


async def call_route(handler: object, request: Request, **arguments: object):
    """Call a decorated route using its runtime asynchronous handler contract.

    Args:
        handler: Route callable whose signature is obscured by Sanic decorators.
        request: Request passed through authorization and validation.
        **arguments: URL parameters supplied by the router.

    Returns:
        Response produced by the decorated route.
    """
    callback = cast(Callable[..., Awaitable[HTTPResponse]], handler)
    return await callback(request, **arguments)


def response_json(response: HTTPResponse):
    """Decode a route response after verifying that it contains a body.

    Args:
        response: Response returned by the decorated route.

    Returns:
        Decoded JSON payload.
    """
    assert response.body is not None
    return json.loads(response.body)
