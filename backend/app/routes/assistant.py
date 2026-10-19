"""Expose authenticated in-app assistant model choices and SSE conversations."""

import asyncio
from contextlib import aclosing, suppress

from sanic import Blueprint, HTTPResponse, Request, json
from sanic_ext import validate

from app.core.decorators import authorize
from app.models.assistant import AssistantChat, AssistantEvent
from app.services.assistant import AssistantService
from app.utils.json import dumps

assistant = Blueprint("assistant", url_prefix="/assistant")
HEARTBEAT_INTERVAL = 10


@assistant.get("/models")
@authorize()
async def list_models(_: Request) -> HTTPResponse:
    """List configured model choices for the current authenticated account.

    Args:
        _: Authenticated browser request.

    Returns:
        Enabled providers and model IDs without credentials or endpoint settings.
    """
    return json(
        await AssistantService.list_models(), headers={"Cache-Control": "no-store"}
    )


@assistant.post("/chat")
@authorize()
@validate(json=AssistantChat)
async def chat(request: Request, body: AssistantChat) -> None:
    """Stream one assistant turn and cancel it when the browser disconnects.

    Args:
        request: Authenticated browser request with current account permissions.
        body: Validated text history and selected model.
    """
    response = await request.respond(
        content_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
    queue: asyncio.Queue[AssistantEvent | None] = asyncio.Queue(maxsize=16)

    async def produce() -> None:
        """Run the conversation in one task and apply bounded backpressure."""
        async with aclosing(AssistantService.stream(body, request.ctx.user)) as events:
            async for event in events:
                await queue.put(event)
        await queue.put(None)

    task = asyncio.create_task(produce(), name="assistant-turn")
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), HEARTBEAT_INTERVAL)
            except TimeoutError:
                await response.send(": keep-alive\n\n")
                continue
            if event is None:
                break
            data = dumps(event.data).decode()
            await response.send(f"event: {event.event}\ndata: {data}\n\n")
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
