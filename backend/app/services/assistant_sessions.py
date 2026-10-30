"""Persist account-owned assistant conversations and immutable graph snapshots."""

import hashlib
from datetime import timedelta
from typing import Any

from tortoise import timezone
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.flow.validation import compress_document
from app.models.assistant import (
    MAX_SNAPSHOT_BYTES,
    AssistantConversation,
    AssistantSnapshot,
    AssistantToolCall,
    AssistantTurn,
    AssistantTurnRequest,
    ConversationCreate,
    WorkflowDesignerContext,
    media_resource,
)
from app.models.download import DownloadPlan
from app.models.flow import FlowJob, GraphDocument
from app.models.user import UserInfo
from app.services.flow import FlowGraphService
from app.services.media import MediaLibService
from app.utils.json import dumps

LEASE_SECONDS = 180


def resource_order(resource_id: int | str) -> tuple[bool, int | str]:
    """Sort workflow IDs numerically before typed media library references.

    Args:
        resource_id: Bare workflow ID or typed reference from `media_resource`.

    Returns:
        A key that never compares integers with strings.
    """
    return isinstance(resource_id, str), resource_id


class AssistantSessionService:
    """Apply ownership checks to every persistent conversation operation."""

    @classmethod
    async def owned(cls, conversation_id: int, user: UserInfo) -> AssistantConversation:
        """Load a conversation only within the current account.

        Args:
            conversation_id: Conversation identifier supplied by the caller.
            user: Authenticated account, including administrators.

        Returns:
            The account's conversation.

        Raises:
            NotFoundException: If the conversation is absent or belongs to another
                account.
        """
        result = await AssistantConversation.get_or_none(
            id=conversation_id, user_id=user.id
        )
        if result is None:
            raise NotFoundException
        return result

    @staticmethod
    def describe(conversation: AssistantConversation) -> dict:
        """Return public conversation metadata without exposing checkpoint contents.

        Args:
            conversation: An already authorized conversation.

        Returns:
            Metadata for the history selector.
        """
        return {
            "id": conversation.id,
            "title": conversation.title,
            "model": conversation.model,
            "updated_at": conversation.updated_at,
            "compacted": bool(conversation.summary),
            "busy": bool(
                conversation.active_request
                and conversation.lease_until
                and conversation.lease_until > timezone.now()
            ),
        }

    @classmethod
    async def create(cls, body: ConversationCreate, user: UserInfo) -> dict:
        """Create a conversation owned by the authenticated account.

        Args:
            body: Initial title and selected model.
            user: Authenticated owner.

        Returns:
            Public conversation metadata.
        """
        return cls.describe(
            await AssistantConversation.create(
                user_id=user.id, title=body.title.strip(), model=body.model.model_dump()
            )
        )

    @classmethod
    async def list_conversations(
        cls, user: UserInfo, before: int | None = None
    ) -> dict:
        """List bounded pages of owned conversations in stable creation order.

        Args:
            user: Authenticated owner.
            before: Exclusive conversation cursor for older records.

        Returns:
            Conversation metadata and a continuation cursor.
        """
        query = AssistantConversation.filter(user_id=user.id)
        if before is not None:
            query = query.filter(id__lt=before)
        items = await query.order_by("-id").limit(31)
        return {
            "items": [cls.describe(item) for item in items[:30]],
            "next_before": items[29].id if len(items) > 30 else None,
        }

    @classmethod
    async def rename(cls, conversation_id: int, title: str, user: UserInfo) -> dict:
        """Rename an owned conversation without modifying its history.

        Args:
            conversation_id: Conversation to rename.
            title: Nonblank bounded title.
            user: Authenticated owner.

        Returns:
            Updated conversation metadata.

        Raises:
            KaloscopeException: If the title is blank.
        """
        conversation = await cls.owned(conversation_id, user)
        if not title.strip():
            raise KaloscopeException(ErrorCode.BAD_REQUEST, status_code=400)
        conversation.title = title.strip()
        await conversation.save(update_fields=["title", "updated_at"])
        return cls.describe(conversation)

    @classmethod
    async def delete(cls, conversation_id: int, user: UserInfo) -> None:
        """Delete an idle owned conversation with its dependent records.

        Args:
            conversation_id: Conversation to delete.
            user: Authenticated owner.

        Raises:
            NotFoundException: If the conversation is absent or belongs to another
                account.
            KaloscopeException: If a generation is still active.
        """
        await cls.delete_many([conversation_id], user)

    @classmethod
    async def delete_many(cls, conversation_ids: list[int], user: UserInfo) -> None:
        """Delete owned idle conversations and their records as one transaction.

        Args:
            conversation_ids: Selected conversations; repeated IDs are deleted once.
            user: Authenticated owner, including administrators.

        Raises:
            BadRequestException: If no conversations are selected.
            NotFoundException: If any conversation is absent or belongs to another
                account.
            KaloscopeException: If any generation is active; the entire batch is
                retained.
        """
        ids = set(conversation_ids)
        if not ids:
            raise BadRequestException
        async with in_transaction():
            conversations = AssistantConversation.filter(id__in=ids, user_id=user.id)
            if await conversations.count() != len(ids):
                raise NotFoundException
            await conversations.filter(
                Q(active_request=None) | Q(lease_until__lt=timezone.now())
            ).delete()
            # sqlite deletion counts include cascaded records, so inspect the parents
            if await conversations.exists():
                raise KaloscopeException(ErrorCode.ASSISTANT_BUSY, status_code=409)

    @classmethod
    async def allowed(cls, resource_ids: list[int | str], user: UserInfo) -> bool:
        """Recheck historical business resources before revealing their stored content.

        Args:
            resource_ids: Resources contributing to one record or checkpoint: bare
                workflow IDs or typed media library references.
            user: Account with current permissions.

        Returns:
            Whether all source resources remain accessible. Unknown reference
            types are treated as inaccessible.
        """
        for resource_id in set(resource_ids):
            try:
                if isinstance(resource_id, str):
                    kind, _, value = resource_id.partition(":")
                    if kind != "media_lib" or not value.isdecimal():
                        return False
                    await MediaLibService.get_accessible(int(value), user)
                else:
                    await FlowGraphService.get_accessible(resource_id, user)
            except KaloscopeException as exc:
                if exc.status_code not in (403, 404):
                    raise
                return False
        return True

    @classmethod
    async def turns(
        cls, conversation_id: int, user: UserInfo, before: int | None = None
    ) -> dict:
        """Read a bounded page of persisted turns and authorized tool results.

        Args:
            conversation_id: Owned conversation to read.
            user: Account with current resource permissions.
            before: Exclusive turn cursor for earlier history.

        Returns:
            Chronological turns with redacted inaccessible historical material.
        """
        conversation = await cls.owned(conversation_id, user)
        await cls.recover(conversation)
        query = AssistantTurn.filter(conversation_id=conversation_id)
        if before is not None:
            query = query.filter(id__lt=before)
        records = await query.order_by("-id").limit(21)
        items = []
        visible = []
        for turn in reversed(records[:20]):
            allowed = await cls.allowed(turn.resource_ids, user)
            calls = await AssistantToolCall.filter(turn_id=turn.id) if allowed else []
            visible.extend(call.call_id for call in calls)
            items.append(
                {
                    "id": str(turn.id),
                    "request_id": turn.request_id,
                    "prompt": turn.prompt,
                    "content": turn.content if allowed else "",
                    "context": turn.context if allowed else None,
                    "status": turn.status if allowed else "error",
                    "error": turn.error if allowed else "permission_denied",
                    "tools": [
                        {
                            "id": call.call_id,
                            "name": call.name,
                            "offset": call.offset,
                            "status": call.status,
                            "result": call.result,
                        }
                        for call in calls
                    ],
                    "usage": turn.usage,
                    "retry_of": turn.retry_of,
                }
            )
        from app.services.assistant_proposals import AssistantProposalService

        # attach each preview's latest decision so reloads restore its outcome
        proposals = await AssistantProposalService.latest(conversation_id, visible)
        for item in items:
            for tool in item["tools"]:
                if tool["id"] in proposals:
                    tool["proposal"] = proposals[tool["id"]]
        return {
            "conversation": cls.describe(conversation),
            "items": items,
            "next_before": records[19].id if len(records) > 20 else None,
        }

    @classmethod
    async def recover(cls, conversation: AssistantConversation) -> None:
        """Interrupt expired attempts without affecting a new lease owner.

        Args:
            conversation: Last observed conversation lease.
        """
        if (
            not conversation.active_request
            or not conversation.lease_until
            or conversation.lease_until >= timezone.now()
        ):
            return
        async with in_transaction():
            released = await AssistantConversation.filter(
                id=conversation.id,
                active_request=conversation.active_request,
                lease_until__lt=timezone.now(),
            ).update(active_request=None, lease_until=None)
            if released:
                turns = AssistantTurn.filter(
                    conversation_id=conversation.id, status="streaming"
                )
                ids = await turns.values_list("id", flat=True)
                await turns.update(
                    status="interrupted", error="assistant_stream_interrupted"
                )
                await AssistantToolCall.filter(
                    turn_id__in=ids, status="running"
                ).update(status="interrupted")

    @classmethod
    async def snapshot(
        cls,
        conversation_id: int,
        workflow_id: int,
        version: int,
        document: GraphDocument,
        origin: str,
        user: UserInfo,
    ) -> AssistantSnapshot:
        """Persist an immutable graph after both ownership and resource authorization.

        Args:
            conversation_id: Conversation owning the snapshot.
            workflow_id: Referenced business resource.
            version: Saved version underlying the document.
            document: Complete graph copied from the page or authorized storage.
            origin: Source label, such as `canvas`, `saved`, or `proposal`.
            user: Authenticated owner.

        Returns:
            A stored snapshot deduplicated within this conversation.

        Raises:
            KaloscopeException: If the document exceeds the attachment budget.
        """
        await cls.owned(conversation_id, user)
        await FlowGraphService.get_accessible(workflow_id, user)
        data = compress_document(document)
        encoded = dumps(data)
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise KaloscopeException(ErrorCode.ASSISTANT_CONTEXT_LIMIT)
        record, _ = await AssistantSnapshot.get_or_create(
            conversation_id=conversation_id,
            workflow_id=workflow_id,
            draft_version=version,
            digest=hashlib.sha256(encoded).hexdigest(),
            defaults={"document": data, "origin": origin},
        )
        return record

    @classmethod
    async def read_snapshot(
        cls, conversation_id: int, snapshot_id: int, user: UserInfo
    ) -> AssistantSnapshot:
        """Resolve an owned snapshot with current resource permissions.

        Args:
            conversation_id: Conversation using the reference.
            snapshot_id: Immutable graph identifier.
            user: Authenticated owner with current resource permissions.

        Returns:
            The original complete graph snapshot.

        Raises:
            NotFoundException: If the snapshot is not part of this conversation.
        """
        await cls.owned(conversation_id, user)
        record = await AssistantSnapshot.get_or_none(
            id=snapshot_id, conversation_id=conversation_id
        )
        if record is None:
            raise NotFoundException
        await FlowGraphService.get_accessible(record.workflow_id, user)
        return record

    @classmethod
    async def reviewed(cls, turn: AssistantTurn) -> set[str]:
        """Name the change reviews the previous completed turn showed as valid.

        Args:
            turn: Active turn whose conversation history is read.

        Returns:
            Schedule, plan and task review tools whose results may be confirmed
            in this turn.
        """
        previous = (
            await AssistantTurn.filter(
                conversation_id=turn.conversation_id, id__lt=turn.id, status="complete"
            )
            .order_by("-id")
            .first()
        )
        if previous is None:
            return set()
        return {
            call.name
            for call in await AssistantToolCall.filter(
                turn_id=previous.id,
                name__in=[
                    "review_workflow_schedule",
                    "review_download_plan_change",
                    "review_download_task_action",
                ],
            )
            if ((call.result or {}).get("data") or {}).get("valid")
        }

    @classmethod
    async def begin(
        cls, conversation_id: int, body: AssistantTurnRequest, user: UserInfo
    ) -> AssistantTurn:
        """Claim a conversation and persist the request before generation.

        Args:
            conversation_id: Owned conversation receiving the request.
            body: New message and independently captured current page.
            user: Authenticated account.

        Returns:
            The persisted turn in the claimed conversation.

        Raises:
            KaloscopeException: If the request is active or duplicated.
            BadRequestException: If a snapshot, selected node, schedule or plan
                reference does not match its workflow.
            NotFoundException: If a retry references another conversation.
        """
        conversation = await cls.owned(conversation_id, user)
        await cls.recover(conversation)
        context = body.context.model_dump(mode="json") if body.context else None
        resource_ids: list[int | str] = []
        if context:
            # page references are rechecked now and with every later history read
            if context["page_type"] == "workflows":
                resource_ids = context["selected_ids"]
            elif context["page_type"] == "workflow_designer":
                resource_ids = [context["resource_id"]]
            elif context["page_type"] == "media_library":
                resource_ids = [media_resource(context["resource_id"])]
            elif context["page_type"] == "media_item":
                resource_ids = [media_resource(context["library_id"])]
            elif context["page_type"] == "workflow_jobs" and context["selected_job"]:
                resource_ids = [context["selected_job"]["workflow_id"]]
            elif context["page_type"] == "download_plans":
                if context["filters"]["workflow_id"]:
                    resource_ids = [context["filters"]["workflow_id"]]
                if selected := context["selected_plan"]:
                    plan = await DownloadPlan.get_or_none(
                        id=selected["plan_id"], graph_id=selected["workflow_id"]
                    )
                    if plan is None:
                        raise BadRequestException
                    # a selected plan also reveals the library it transfers to
                    resource_ids.append(plan.graph_id)
                    if plan.transfer_lib_id:
                        resource_ids.append(media_resource(plan.transfer_lib_id))
            # download pages show shared tasks, so they add nothing to recheck
            if not await cls.allowed(resource_ids, user):
                raise KaloscopeException(ErrorCode.PERMISSION_DENIED, status_code=403)
            if context["page_type"] == "workflow_jobs" and context["selected_job"]:
                selected = context["selected_job"]
                if not await FlowJob.filter(
                    id=selected["job_id"], graph_id=selected["workflow_id"]
                ).exists():
                    raise BadRequestException
        async with in_transaction():
            claimed = await AssistantConversation.filter(
                id=conversation_id, user_id=user.id, active_request=None
            ).update(
                active_request=body.request_id,
                lease_until=timezone.now() + timedelta(seconds=LEASE_SECONDS),
                updated_at=timezone.now(),
                model=body.model.model_dump(),
            )
            if not claimed:
                raise KaloscopeException(ErrorCode.ASSISTANT_BUSY, status_code=409)
            if await AssistantTurn.filter(
                conversation_id=conversation_id, request_id=body.request_id
            ).exists():
                raise KaloscopeException(
                    ErrorCode.ASSISTANT_REQUEST_EXISTS, status_code=409
                )
            if (
                body.retry_of is not None
                and not await AssistantTurn.filter(
                    id=body.retry_of, conversation_id=conversation_id
                )
                .exclude(status="complete")
                .exists()
            ):
                raise NotFoundException
            if isinstance(body.context, WorkflowDesignerContext):
                if context is None:
                    raise BadRequestException
                snapshot = await cls.read_snapshot(
                    conversation_id, body.context.snapshot_id, user
                )
                if (
                    snapshot.workflow_id != body.context.resource_id
                    or snapshot.draft_version != body.context.draft_version
                    or not set(body.context.selected_node_ids).issubset(
                        {node["id"] for node in snapshot.document["nodes"]}
                    )
                ):
                    raise BadRequestException
                context.update(
                    editable=await cls.direct_edit(body.context, user),
                    graph={
                        "node_count": len(snapshot.document["nodes"]),
                        "edge_count": len(snapshot.document["edges"]),
                    },
                )
            turn = await AssistantTurn.create(
                conversation_id=conversation_id,
                request_id=body.request_id,
                prompt=body.prompt.strip(),
                model=body.model.model_dump(),
                context=context,
                resource_ids=resource_ids,
                retry_of=body.retry_of,
            )
            if not conversation.title:
                await AssistantConversation.filter(id=conversation_id).update(
                    title=body.prompt.strip()[:120]
                )
        return turn

    @staticmethod
    async def direct_edit(context: WorkflowDesignerContext, user: UserInfo) -> bool:
        """Decide whether a designer turn may create saveable canvas edits.

        The page reports only its own mode; current editor rights and workflow
        editability can narrow it but never widen it. Tools still recheck both
        when a preview runs.

        Args:
            context: Validated designer context captured when the request was sent.
            user: Authenticated account with current permissions.

        Returns:
            Whether previews in this turn may become direct edits of the canvas.
        """
        if not context.editable:
            return False
        try:
            FlowGraphService.require_editor(user)
            graph = await FlowGraphService.get_accessible(context.resource_id, user)
        except (ForbiddenException, NotFoundException):
            return False
        return graph.editable

    @classmethod
    async def checkpoint(cls, turn: AssistantTurn, **values: Any) -> None:
        """Persist progress only while this turn still owns the conversation lease.

        Args:
            turn: Active persisted attempt.
            **values: Updated text, usage, state, or error fields.

        Raises:
            KaloscopeException: If another attempt owns the conversation.
        """
        async with in_transaction():
            claimed = await AssistantConversation.filter(
                id=turn.conversation_id, active_request=turn.request_id
            ).update(
                lease_until=timezone.now() + timedelta(seconds=LEASE_SECONDS),
                updated_at=timezone.now(),
            )
            if not claimed:
                raise KaloscopeException(ErrorCode.ASSISTANT_BUSY, status_code=409)
            await AssistantTurn.filter(id=turn.id, status="streaming").update(**values)

    @classmethod
    async def finish(cls, turn: AssistantTurn, **values: Any) -> None:
        """Finalize an owned attempt and release its lease in the same transaction.

        Args:
            turn: Active persisted attempt.
            **values: Final text, terminal state, error, and usage.
        """
        async with in_transaction():
            await cls.checkpoint(turn, **values)
            await AssistantToolCall.filter(turn_id=turn.id, status="running").update(
                status="interrupted"
            )
            await AssistantConversation.filter(
                id=turn.conversation_id, active_request=turn.request_id
            ).update(active_request=None, lease_until=None)

    @classmethod
    async def record_resources(
        cls, turn: AssistantTurn, resource_ids: list[int | str]
    ) -> None:
        """Track business resources contributing data to this attempt.

        Args:
            turn: Active persisted attempt.
            resource_ids: Workflow IDs or typed media library references returned
                by authorized tools.
        """
        turn.resource_ids = sorted(
            set(turn.resource_ids) | set(resource_ids), key=resource_order
        )
        await cls.checkpoint(turn, resource_ids=turn.resource_ids)
