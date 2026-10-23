"""Save and recover account-owned edits to the current workflow canvas."""

from contextlib import suppress
from copy import deepcopy
from typing import Any

from tortoise import timezone
from tortoise.expressions import F
from tortoise.transactions import in_transaction

from app.core.exceptions import (
    ErrorCode,
    KaloscopeException,
    NotFoundException,
)
from app.core.flow.nodes.base import Node
from app.core.flow.validation import compress_document, validate_document
from app.models.assistant import (
    AssistantProposal,
    AssistantSnapshot,
    ProposalConfirm,
    ProposalDecision,
)
from app.models.flow import (
    FlowGraph,
    GraphDocument,
    GraphState,
    GraphWrite,
)
from app.models.user import UserInfo
from app.services.assistant_sessions import AssistantSessionService
from app.services.flow import FlowGraphService

OPEN_STATUSES = ("pending", "conflict")
EMPTY_GRAPH = {"nodes": [], "edges": []}


def describe_proposal(proposal: AssistantProposal) -> dict[str, Any]:
    """Return the public lifecycle state of one owned edit.

    Args:
        proposal: An already authorized edit record.

    Returns:
        Identity, captured base, and the recorded outcome without graph contents.
    """
    return {
        "id": proposal.id,
        "call_id": proposal.call_id,
        "status": proposal.status,
        "workflow_id": proposal.workflow_id,
        "base_version": proposal.base_version,
        "digest": proposal.digest,
        "saved_version": proposal.saved_version,
        "error": proposal.error,
    }


class AssistantProposalService:
    """Apply only explicit user decisions to immutable assistant edits."""

    @classmethod
    async def create(
        cls,
        conversation_id: int,
        call_id: str,
        workflow_id: int,
        base_version: int,
        snapshot: AssistantSnapshot,
        base_snapshot_id: int,
    ) -> AssistantProposal:
        """Record a pending edit for a previewed immutable document.

        Args:
            conversation_id: Server-owned conversation of the preview.
            call_id: Server-generated identifier of the preview tool call.
            workflow_id: Workflow whose draft the edit would replace.
            base_version: Saved draft version that must still be current to save.
            snapshot: Stored proposed document.
            base_snapshot_id: Original canvas captured before the edit.

        Returns:
            The pending edit.
        """
        return await AssistantProposal.create(
            conversation_id=conversation_id,
            call_id=call_id,
            snapshot_id=snapshot.id,
            base_snapshot_id=base_snapshot_id,
            workflow_id=workflow_id,
            base_version=base_version,
            digest=snapshot.digest,
        )

    @classmethod
    async def latest(cls, conversation_id: int, call_ids: list[str]) -> dict[str, dict]:
        """Load the recorded outcomes of visible canvas-edit calls.

        Args:
            conversation_id: Owned conversation containing the calls.
            call_ids: Preview tool call identifiers.

        Returns:
            Public edit state keyed by call identifier.
        """
        if not call_ids:
            return {}
        records = await AssistantProposal.filter(
            conversation_id=conversation_id, call_id__in=call_ids
        ).order_by("id")
        return {record.call_id: describe_proposal(record) for record in records}

    @classmethod
    async def owned(
        cls, conversation_id: int, proposal_id: int, user: UserInfo
    ) -> AssistantProposal:
        """Load an edit only through the account's own conversation.

        Args:
            conversation_id: Conversation supplied by the caller.
            proposal_id: Canvas edit supplied by the caller.
            user: Authenticated account, including administrators.

        Returns:
            The owned edit.

        Raises:
            NotFoundException: If either record is absent or belongs elsewhere.
        """
        await AssistantSessionService.owned(conversation_id, user)
        proposal = await AssistantProposal.get_or_none(
            id=proposal_id, conversation_id=conversation_id
        )
        if proposal is None:
            raise NotFoundException
        return proposal

    @staticmethod
    def check_decision(proposal: AssistantProposal, body: ProposalDecision) -> None:
        """Bind a decision to the exact edit shown to the user.

        Args:
            proposal: Owned edit being decided.
            body: Workflow, base version, and digest from the canvas edit.

        Raises:
            KaloscopeException: If the displayed identity differs from the record.
        """
        if (body.workflow_id, body.base_version, body.digest) != (
            proposal.workflow_id,
            proposal.base_version,
            proposal.digest,
        ):
            raise KaloscopeException(
                ErrorCode.ASSISTANT_PROPOSAL_CHANGED, status_code=409
            )

    @classmethod
    async def _documents(cls, proposal: AssistantProposal) -> tuple[dict, dict | None]:
        """Load the immutable result and the original canvas.

        Args:
            proposal: Owned canvas edit.

        Returns:
            The edited document and captured base. Historical records without a
            captured canvas remain readable in history but cannot be saved again.
        """
        document = (await AssistantSnapshot.get(id=proposal.snapshot_id)).document
        base = await AssistantSnapshot.get_or_none(id=proposal.base_snapshot_id)
        return document, base.document if base else None

    @classmethod
    async def _graph(cls, proposal: AssistantProposal, user: UserInfo) -> FlowGraph:
        """Recheck editor rights and workflow access for an edit.

        Args:
            proposal: Owned edit.
            user: Authenticated account with current permissions.

        Returns:
            The current workflow.

        Raises:
            ForbiddenException: If the account can no longer edit workflows.
            NotFoundException: If the workflow no longer exists.
        """
        FlowGraphService.require_editor(user)
        return await FlowGraphService.get_accessible(proposal.workflow_id, user)

    @classmethod
    async def canvas(
        cls, conversation_id: int, proposal_id: int, user: UserInfo
    ) -> dict[str, Any]:
        """Load a validated edit for display in its original designer.

        Args:
            conversation_id: Owned conversation containing the edit.
            proposal_id: Immutable edit to display or whose save outcome to recover.
            user: Authenticated owner with current editor permissions.

        Returns:
            Expanded canvas, validation, original capture identity, and save metadata.
            Structurally invalid documents and records without a captured base
            have no renderable canvas.

        Raises:
            ForbiddenException: If editor access has been revoked.
            NotFoundException: If the conversation, edit, or workflow is unavailable.
        """
        proposal = await cls.owned(conversation_id, proposal_id, user)
        graph = await cls._graph(proposal, user)
        document, base = await cls._documents(proposal)
        parsed = GraphDocument.model_validate(document)
        structure = await validate_document(parsed, graph.category, complete=False)
        validation = await validate_document(parsed, graph.category)
        state = GraphState.DRAFT
        if graph.state != GraphState.DRAFT:
            state = (
                GraphState.PUBLISHED
                if document == graph.definition
                else GraphState.MODIFIED
            )
        return {
            "proposal": describe_proposal(proposal),
            "base_snapshot_id": proposal.base_snapshot_id,
            "current_version": graph.draft_version,
            "editable": graph.editable,
            "canvas": Node.decompress(deepcopy(document))
            if structure.valid and base is not None
            else None,
            "validation": validation.model_dump(mode="json"),
            "change_count": len(
                FlowGraphService.compare_documents(base or EMPTY_GRAPH, document)
            ),
            "workflow": {
                "id": graph.id,
                "state": state,
                "draft_version": proposal.saved_version
                if proposal.saved_version is not None
                else proposal.base_version,
                "updated_at": proposal.resolved_at or graph.updated_at,
            },
        }

    @classmethod
    async def confirm(
        cls,
        conversation_id: int,
        proposal_id: int,
        body: ProposalConfirm,
        user: UserInfo,
    ) -> dict[str, Any]:
        """Save a canvas edit once and record the actual outcome.

        The claim, version-checked draft write, and outcome commit together, so a
        lost connection or crash leaves the edit pending without a write.
        Duplicate or concurrent requests return the single recorded outcome.

        Args:
            conversation_id: Owned conversation.
            proposal_id: Canvas edit accepted by the user.
            body: Displayed identity and the original designer canvas.
            user: Authenticated owner with current editor rights.

        Returns:
            The recorded lifecycle state.

        Raises:
            ForbiddenException: If the account can no longer edit workflows.
            KaloscopeException: If the canvas edit or canvas has changed.
        """
        proposal = await cls.owned(conversation_id, proposal_id, user)
        cls.check_decision(proposal, body)
        FlowGraphService.require_editor(user)
        try:
            async with in_transaction():
                claimed = await AssistantProposal.filter(
                    id=proposal.id, status="pending"
                ).update(attempts=F("attempts") + 1, updated_at=timezone.now())
                if claimed:
                    outcome = await cls._save(proposal, body, user)
                    await AssistantProposal.filter(id=proposal.id).update(
                        **outcome, resolved_at=timezone.now(), updated_at=timezone.now()
                    )
        except KaloscopeException:
            raise
        except Exception:
            # the write rolled back; keep the edit retryable and visible
            with suppress(Exception):
                await AssistantProposal.filter(id=proposal.id, status="pending").update(
                    attempts=F("attempts") + 1,
                    error=ErrorCode.INTERNAL_SERVER_ERROR,
                    updated_at=timezone.now(),
                )
            raise
        return describe_proposal(await AssistantProposal.get(id=proposal.id))

    @classmethod
    async def _save(
        cls, proposal: AssistantProposal, body: ProposalConfirm, user: UserInfo
    ) -> dict[str, Any]:
        """Write the edit through the shared draft path inside a claimed save.

        Args:
            proposal: Claimed pending edit.
            body: Confirmation with the original designer canvas.
            user: Authenticated editor.

        Returns:
            Outcome fields to record.

        Raises:
            KaloscopeException: If the visible canvas no longer matches the base or
                permissions changed; the claim then rolls back.
        """
        try:
            graph = await FlowGraphService.get_accessible(proposal.workflow_id, user)
        except NotFoundException:
            return {"status": "failed", "error": ErrorCode.NOT_FOUND}
        if not graph.editable:
            return {"status": "failed", "error": ErrorCode.FLOW_NOT_EDITABLE}
        if graph.draft_version != proposal.base_version:
            return {"status": "conflict", "error": ErrorCode.FLOW_DRAFT_CONFLICT}
        proposed, base = await cls._documents(proposal)
        if base is None or FlowGraphService.compare_documents(
            base, compress_document(body.canvas)
        ):
            raise KaloscopeException(
                ErrorCode.ASSISTANT_CANVAS_CHANGED, status_code=409
            )
        if not FlowGraphService.compare_documents(graph.draft or EMPTY_GRAPH, proposed):
            return {
                "status": "unchanged",
                "saved_version": graph.draft_version,
                "error": None,
            }
        try:
            saved = await FlowGraphService.save_graph(
                proposal.workflow_id,
                GraphWrite(**proposed, draft_version=proposal.base_version),
                user,
            )
        except KaloscopeException as exc:
            match exc.message:
                case ErrorCode.FLOW_DRAFT_CONFLICT:
                    return {"status": "conflict", "error": exc.message}
                case ErrorCode.FLOW_GRAPH_INVALID | ErrorCode.FLOW_NOT_EDITABLE:
                    return {"status": "failed", "error": exc.message}
            raise
        return {"status": "saved", "saved_version": saved.draft_version, "error": None}

    @classmethod
    async def cancel(
        cls,
        conversation_id: int,
        proposal_id: int,
        body: ProposalDecision,
        user: UserInfo,
    ) -> dict[str, Any]:
        """Close an open edit without writing the workflow.

        Args:
            conversation_id: Owned conversation.
            proposal_id: Canvas edit the user reverted.
            body: Displayed identity.
            user: Authenticated owner.

        Returns:
            The recorded lifecycle state; resolved edits are unchanged.
        """
        proposal = await cls.owned(conversation_id, proposal_id, user)
        cls.check_decision(proposal, body)
        await AssistantProposal.filter(id=proposal.id, status__in=OPEN_STATUSES).update(
            status="cancelled", resolved_at=timezone.now(), updated_at=timezone.now()
        )
        return describe_proposal(await AssistantProposal.get(id=proposal.id))
