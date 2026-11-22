"""Expose authenticated in-app assistant model choices and SSE conversations."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from typing import Any
from uuid import UUID

from sanic import Blueprint, HTTPResponse, Request, json
from sanic.exceptions import ServerError
from sanic_ext import validate

from app.core.decorators import authorize
from app.models.assistant import (
    AssistantEvent,
    AssistantSnapshotRequest,
    AssistantTurnRequest,
    ConversationCreate,
    ConversationDelete,
    ConversationRename,
    DeviceReceipt,
    ProposalConfirm,
    ProposalDecision,
)
from app.models.assistant_questions import AnswerGroup
from app.models.download import PlanReviewDecision, ReplacementDecision
from app.models.user import UserRole
from app.services.agent_actions import action_status, decide_action
from app.services.assistant import AssistantService
from app.services.assistant_plan_review import plan_review
from app.services.assistant_proposals import AssistantProposalService
from app.services.assistant_questions import answer_card
from app.services.assistant_reviews import assistant_review
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
@authorize(role=UserRole.ADMIN)
async def list_models(_: Request) -> HTTPResponse:
    """List configured model choices for the current authenticated account.

    Args:
        _: Authenticated browser request.

    Returns:
        Enabled providers and model IDs without credentials or endpoint settings.
    """
    return private_json(await AssistantService.list_models())


async def serve_events(
    request: Request, events: AsyncGenerator[AssistantEvent]
) -> None:
    """Deliver assistant events with heartbeat and cancellation propagation.

    Args:
        request: Authenticated streaming HTTP request.
        events: Request-owned asynchronous event generator that is closed on exit.

    Raises:
        ServerError: If response middleware prevents streaming a response.
    """
    response = await request.respond(
        content_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
    if response is None:
        raise ServerError("Assistant stream response is unavailable")
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
@authorize(role=UserRole.ADMIN)
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
@authorize(role=UserRole.ADMIN)
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
@authorize(role=UserRole.ADMIN)
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
@authorize(role=UserRole.ADMIN)
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
@authorize(role=UserRole.ADMIN)
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
@authorize(role=UserRole.ADMIN)
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


@assistant.post("/conversations/delete")
@authorize(role=UserRole.ADMIN)
@validate(json=ConversationDelete)
async def delete_conversations(
    request: Request, body: ConversationDelete
) -> HTTPResponse:
    """Delete a selected batch of idle conversations owned by the current account.

    Args:
        request: Authenticated request.
        body: Conversation IDs to delete together.

    Returns:
        An empty JSON result after the entire batch is deleted.
    """
    await AssistantSessionService.delete_many(body.ids, request.ctx.user)
    return private_json(None)


@assistant.post("/conversations/<conversation_id:int>/chat")
@authorize(role=UserRole.ADMIN)
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


@assistant.post("/conversations/<conversation_id:int>/device/<call_id:str>")
@authorize(role=UserRole.ADMIN)
@validate(json=DeviceReceipt)
async def record_device(
    request: Request, conversation_id: int, call_id: str, body: DeviceReceipt
) -> HTTPResponse:
    """Record an owned client-reported navigation outcome without changing media.

    Args:
        request: Authenticated current device.
        conversation_id: Conversation offering the action.
        call_id: Immutable navigation tool call.
        body: Device-reported outcome.

    Returns:
        The latest outcome, retaining a previously confirmed opening.
    """
    return private_json(
        await AssistantSessionService.record_device_result(
            conversation_id, call_id, body.opened, request.ctx.user
        )
    )


@assistant.post("/conversations/<conversation_id:int>/questions/<call_id:str>/answer")
@authorize(role=UserRole.ADMIN)
@validate(json=AnswerGroup)
async def submit_answers(
    request: Request, conversation_id: int, call_id: str, body: AnswerGroup
) -> HTTPResponse:
    """Record answers and create a subscription confirmed by its bound card.

    Args:
        request: Authenticated request.
        conversation_id: Owned conversation.
        call_id: Displayed interaction identity.
        body: Answers and the displayed version.

    Returns:
        A recorded selection or a field-specific validation issue.
    """
    return private_json(
        await answer_card(conversation_id, call_id, body, request.ctx.user)
    )


PROPOSAL = "/conversations/<conversation_id:int>/proposals/<proposal_id:int>"


@assistant.get(f"{PROPOSAL}/canvas")
@authorize(role=UserRole.ADMIN)
async def get_proposal_canvas(
    request: Request, conversation_id: int, proposal_id: int
) -> HTTPResponse:
    """Read an owned edit for direct display in the workflow designer.

    Args:
        request: Authenticated request with current editor permissions.
        conversation_id: Conversation owning the immutable edit.
        proposal_id: Edit whose complete canvas and outcome to read.

    Returns:
        Renderable canvas, validation, and save metadata without publishing it.
    """
    return private_json(
        await AssistantProposalService.canvas(
            conversation_id, proposal_id, request.ctx.user
        )
    )


@assistant.get(f"{PROPOSAL}/outcome")
@authorize(role=UserRole.ADMIN)
async def get_proposal_outcome(
    request: Request, conversation_id: int, proposal_id: int
) -> HTTPResponse:
    """Recover an owned edit's save outcome without reading workflow contents.

    Args:
        request: Authenticated request from the edit owner.
        conversation_id: Conversation owning the edit.
        proposal_id: Edit whose recorded outcome to read.

    Returns:
        The recorded outcome, even after the workflow or editing rights are gone.
    """
    return private_json(
        await AssistantProposalService.outcome(
            conversation_id, proposal_id, request.ctx.user
        )
    )


@assistant.post(f"{PROPOSAL}/confirm")
@authorize(role=UserRole.ADMIN)
@validate(json=ProposalConfirm)
async def confirm_proposal(
    request: Request, conversation_id: int, proposal_id: int, body: ProposalConfirm
) -> HTTPResponse:
    """Save the displayed canvas edit after the user chooses to keep it.

    Args:
        request: Authenticated request from the edit owner.
        conversation_id: Conversation owning the edit.
        proposal_id: Canvas edit to save.
        body: Displayed identity and the original captured snapshot identifier.

    Returns:
        The single recorded outcome.
    """
    return private_json(
        await AssistantProposalService.confirm(
            conversation_id, proposal_id, body, request.ctx.user
        )
    )


@assistant.post(f"{PROPOSAL}/cancel")
@authorize(role=UserRole.ADMIN)
@validate(json=ProposalDecision)
async def cancel_proposal(
    request: Request, conversation_id: int, proposal_id: int, body: ProposalDecision
) -> HTTPResponse:
    """Dismiss an open edit without writing the workflow.

    Args:
        request: Authenticated request from the edit owner.
        conversation_id: Conversation owning the edit.
        proposal_id: Canvas edit to revert.
        body: Displayed identity.

    Returns:
        The recorded lifecycle state.
    """
    return private_json(
        await AssistantProposalService.cancel(
            conversation_id, proposal_id, body, request.ctx.user
        )
    )


@assistant.get("/operations/<key:uuid>")
@authorize(role=UserRole.ADMIN)
async def get_operation(request: Request, key: UUID) -> HTTPResponse:
    """Read the review owned by the browser account.

    Args:
        request: Administrator session.
        key: Durable operation identity.

    Returns:
        Current preview, decision status and any result.
    """
    return private_json(await action_status(key, request.ctx.user))


@assistant.post("/operations/<key:uuid>/decision")
@authorize(role=UserRole.ADMIN)
@validate(json=ReplacementDecision)
async def decide_operation(
    request: Request, key: UUID, body: ReplacementDecision
) -> HTTPResponse:
    """Approve or cancel the exact review displayed in the browser.

    Args:
        request: Administrator session.
        key: Durable operation identity.
        body: The displayed digest and explicit decision.

    Returns:
        Current persisted outcome.
    """
    return private_json(
        await decide_action(key, body.digest, body.approve, request.ctx.user)
    )


@assistant.get("/plan-reviews/<call_id:str>")
@authorize(role=UserRole.ADMIN)
async def read_plan_review(request: Request, call_id: str) -> HTTPResponse:
    """Read the current decision for an owned assistant review.

    Args:
        request: Authenticated administrator session.
        call_id: Original server-assigned tool call.

    Returns:
        Its stored preview and decision state.
    """
    return private_json(await plan_review(call_id, request.ctx.user))


@assistant.post("/plan-reviews/<call_id:str>/decision")
@authorize(role=UserRole.ADMIN)
@validate(json=PlanReviewDecision)
async def decide_plan_review(
    request: Request, call_id: str, body: PlanReviewDecision
) -> HTTPResponse:
    """Record a browser decision for an owned subscription update or deletion.

    Args:
        request: Authenticated administrator session.
        call_id: Original server-assigned tool call.
        body: Exact request and explicit user decision.

    Returns:
        Current decision state, including a previously committed decision.
    """
    return private_json(await plan_review(call_id, request.ctx.user, body))


@assistant.get("/reviews/<call_id:str>")
@authorize(role=UserRole.ADMIN)
async def get_review(request: Request, call_id: str) -> HTTPResponse:
    """Read the trusted status of a chat proposal.

    Args:
        request: Authenticated browser session.
        call_id: Server-issued review identity.

    Returns:
        The account-owned preview and durable outcome.
    """
    return private_json(await assistant_review(call_id, request.ctx.user))


@assistant.post("/reviews/<call_id:str>/decision")
@authorize(role=UserRole.ADMIN)
@validate(json=PlanReviewDecision)
async def decide_review(
    request: Request, call_id: str, body: PlanReviewDecision
) -> HTTPResponse:
    """Decide the exact settings originally displayed by a chat tool.

    Args:
        request: Authenticated browser session.
        call_id: Server-issued review identity.
        body: Displayed request ID and explicit decision.

    Returns:
        The authoritative outcome or any existing decision.
    """
    return private_json(await assistant_review(call_id, request.ctx.user, body))
