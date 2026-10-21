"""Expose authenticated in-app assistant model choices and SSE conversations."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from typing import Any

from sanic import Blueprint, HTTPResponse, Request, json
from sanic_ext import validate

from app.core.decorators import authorize
from app.models.assistant import (
    AssistantChat,
    AssistantEvent,
    AssistantSnapshotRequest,
    AssistantTurnRequest,
    ConversationCreate,
    ConversationRename,
)
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService
from app.utils.json import dumps

assistant = Blueprint("assistant", url_prefix="/assistant")
HEARTBEAT_INTERVAL = 10


def private_json(data: Any) -> HTTPResponse:
    """Prevent HTTP caching before the application wraps business responses.

    Args:
        data: Account-owned business data to serialize.

    Returns:
        An unwrapped JSON response with a private cache policy.
    """
    return json(data, headers={"Cache-Control": "no-store"})


@assistant.get("/models")
@authorize()
async def list_models(_: Request) -> HTTPResponse:
    """List configured model choices for the current authenticated account.

    Args:
        _: Authenticated browser request.

    Returns:
        Enabled providers and model IDs without credentials or endpoint settings.
    """
    return private_json(await AssistantService.list_models())


@assistant.post("/chat")
@authorize()
@validate(json=AssistantChat)
async def chat(request: Request, body: AssistantChat) -> None:
    """Stream one assistant turn and cancel it when the browser disconnects.

    Args:
        request: Authenticated browser request with current account permissions.
        body: Validated text history and selected model.
    """
    await serve_events(request, AssistantService.stream(body, request.ctx.user))


async def serve_events(request: Request, events: AsyncIterator[AssistantEvent]) -> None:
    """Deliver assistant events with heartbeat and cancellation propagation.

    Args:
        request: Authenticated streaming HTTP request.
        events: Request-owned asynchronous event iterator.
    """
    response = await request.respond(
        content_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
    queue: asyncio.Queue[AssistantEvent | None] = asyncio.Queue(maxsize=16)

    async def produce() -> None:
        """Run the conversation in one task and apply bounded backpressure."""
        try:
            async with aclosing(events) as source:
                async for event in source:
                    await queue.put(event)
        except Exception:
            await queue.put(
                AssistantEvent(event="error", data={"code": "internal_server_error"})
            )
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


@assistant.get("/conversations")
@authorize()
async def list_conversations(request: Request) -> HTTPResponse:
    """List the current account's conversations.

    Args:
        request: Authenticated request with an optional older-page cursor.

    Returns:
        A bounded metadata page.
    """
    return private_json(
        await AssistantSessionService.list_conversations(
            request.ctx.user, cursor(request)
        )
    )


def cursor(request: Request) -> int | None:
    """Validate an optional exclusive pagination cursor.

    Args:
        request: HTTP request containing optional `before` query data.

    Returns:
        A positive cursor or `None` for the newest page.

    Raises:
        BadRequestException: If the cursor is not a positive integer.
    """
    from app.core.exceptions import BadRequestException

    value = request.args.get("before")
    if value is None:
        return None
    if len(value) > 16 or not value.isdecimal() or not 0 < int(value) <= 2**53 - 1:
        raise BadRequestException
    return int(value)


@assistant.post("/conversations")
@authorize()
@validate(json=ConversationCreate)
async def create_conversation(
    request: Request, body: ConversationCreate
) -> HTTPResponse:
    """Create a persistent conversation for the current account.

    Args:
        request: Authenticated request.
        body: Selected model and optional title.

    Returns:
        Created conversation metadata.
    """
    return private_json(await AssistantSessionService.create(body, request.ctx.user))


@assistant.post("/conversations/<conversation_id:int>/snapshots")
@authorize()
@validate(json=AssistantSnapshotRequest)
async def create_snapshot(
    request: Request, conversation_id: int, body: AssistantSnapshotRequest
) -> HTTPResponse:
    """Store a captured canvas without modifying the workflow draft.

    Args:
        request: Authenticated request with current resource permissions.
        conversation_id: Account-owned conversation receiving the attachment.
        body: Complete canvas and its saved workflow version.

    Returns:
        An immutable reference scoped to this conversation and workflow.
    """
    snapshot = await AssistantSessionService.snapshot(
        conversation_id,
        body.workflow_id,
        body.draft_version,
        body.canvas,
        "canvas",
        request.ctx.user,
    )
    return private_json(
        {
            "snapshot_id": snapshot.id,
            "workflow_id": snapshot.workflow_id,
            "draft_version": snapshot.draft_version,
        }
    )


@assistant.get("/conversations/<conversation_id:int>/turns")
@authorize()
async def get_turns(request: Request, conversation_id: int) -> HTTPResponse:
    """Read a page of owned conversation history.

    Args:
        request: Authenticated request with optional pagination.
        conversation_id: Conversation whose turns to read.

    Returns:
        Authorized turn records and conversation metadata.
    """
    return private_json(
        await AssistantSessionService.turns(
            conversation_id, request.ctx.user, cursor(request)
        )
    )


@assistant.patch("/conversations/<conversation_id:int>")
@authorize()
@validate(json=ConversationRename)
async def rename_conversation(
    request: Request, conversation_id: int, body: ConversationRename
) -> HTTPResponse:
    """Rename an account-owned conversation.

    Args:
        request: Authenticated request.
        conversation_id: Conversation to rename.
        body: New bounded title.

    Returns:
        Updated public metadata.
    """
    return private_json(
        await AssistantSessionService.rename(
            conversation_id, body.title, request.ctx.user
        )
    )


@assistant.delete("/conversations/<conversation_id:int>")
@authorize()
async def delete_conversation(request: Request, conversation_id: int) -> HTTPResponse:
    """Delete an idle owned conversation and its retained history.

    Args:
        request: Authenticated request.
        conversation_id: Conversation to remove.

    Returns:
        An empty JSON result after deletion.
    """
    await AssistantSessionService.delete(conversation_id, request.ctx.user)
    return private_json(None)


@assistant.post("/conversations/<conversation_id:int>/chat")
@authorize()
@validate(json=AssistantTurnRequest)
async def persistent_chat(
    request: Request, conversation_id: int, body: AssistantTurnRequest
) -> None:
    """Stream a new persisted turn using server-owned history.

    Args:
        request: Authenticated streaming request.
        conversation_id: Conversation receiving this turn.
        body: New prompt, idempotency key, and current page snapshot.
    """
    await AssistantSessionService.owned(conversation_id, request.ctx.user)
    await serve_events(
        request,
        AssistantService.persistent_stream(conversation_id, body, request.ctx.user),
    )
