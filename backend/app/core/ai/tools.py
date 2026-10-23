"""Expose bounded, permission-checked workflow tools to the in-app assistant."""

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.core.ai.documents import (
    GraphProposal,
    GraphReference,
    apply_changes,
    tool_facts,
    value_summary,
)
from app.core.ai.queries import (
    log_detail,
    value_page,
    workflow_details,
    workflow_list,
    workflow_logs,
)
from app.core.exceptions import ErrorCode, KaloscopeException, NotFoundException
from app.models.assistant import WORKFLOW_PATH, ResourceID, WorkflowOrdering
from app.models.flow import (
    GraphCategory,
    GraphDocument,
    GraphQuery,
    GraphState,
    GraphWrite,
)
from app.models.mcp import NodeTypesResult
from app.models.user import UserInfo, UserRole
from app.services.flow import FlowGraphService
from app.utils.json import dumps, loads

MAX_TOOL_RESULT_BYTES = 32 * 1024
TOOL_TIMEOUT = 15


class ToolArguments(BaseModel):
    """Reject unknown arguments without echoing model-generated content."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WorkflowList(ToolArguments):
    """Bound workflow searches and their result pages."""

    name: str | None = Field(default=None, max_length=255)
    category: GraphCategory | None = None
    state: GraphState | None = None
    ordering: WorkflowOrdering = ""
    page_num: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=10, ge=1, le=100, strict=True)


class WorkflowID(ToolArguments):
    """Identify a workflow from the account's accessible list."""

    workflow_id: ResourceID


class WorkflowLogQuery(WorkflowID):
    """Limit the number of recent execution groups returned."""

    limit: int = Field(default=5, ge=1, le=20, strict=True)


class NodeQuery(ToolArguments):
    """Filter registered node schemas by workflow category and node type."""

    category: GraphCategory | None = None
    node_type: str | None = Field(default=None, min_length=1, max_length=255)


class WorkflowValidation(GraphReference):
    """Validate an exact saved version or immutable captured canvas."""


class WorkflowPreview(GraphProposal):
    """Apply bounded edits and preview them against the original saved version."""


class LogDetailQuery(WorkflowID):
    """Read a bounded page from one retained log record."""

    log_id: ResourceID
    field: Literal["retval", "bootparams", "node_data", "exc_info"] = "retval"
    path: list[str | int] = Field(default_factory=list, max_length=10)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=10, ge=1, le=2000, strict=True)


class SnapshotQuery(GraphReference):
    """Read original node fields or paginated structure from an exact graph."""

    collection: Literal["nodes", "edges"] = "nodes"
    node_id: str | None = Field(default=None, max_length=64)
    field_id: str | None = Field(
        default=None,
        max_length=128,
        description="Authored field inside node.data, e.g. url or headers; never data. "
        "Omit to read the node itself.",
    )
    path: list[str | int] = Field(
        default_factory=list,
        max_length=10,
        description="Keys inside the selected value; use integers for array indices, "
        "e.g. [0, 'value'] for the first header value.",
    )
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=10, ge=1, le=2000, strict=True)


class HistoryQuery(ToolArguments):
    """Retrieve exact earlier turn text or an actual tool result from this
    conversation."""

    turn_id: ResourceID | None = None
    before: ResourceID | None = None
    tool_call_id: str | None = Field(default=None, max_length=64)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=2000, ge=1, le=8000, strict=True)


class NavigationTarget(ToolArguments):
    """Select a registered application destination without accepting URLs."""

    page: Literal["workflows", "workflow_designer"]
    resource_id: ResourceID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        """Require a resource only for workflow-specific destinations.

        Returns:
            A complete registered navigation target.

        Raises:
            ValueError: If the resource does not match the destination type.
        """
        if (self.page == "workflows") != (self.resource_id is None):
            raise ValueError("Resource does not match the destination")
        return self


class NavigationResult(BaseModel):
    """Return a checked destination for an explicit user navigation action."""

    target: NavigationTarget
    path: str


@dataclass(frozen=True)
class AssistantTool:
    """Define a server-owned tool grant and its JSON argument schema."""

    name: str
    description: str
    arguments: type[BaseModel]
    admin: bool = False


TOOLS = (
    AssistantTool(
        "list_workflows",
        "List accessible workflows with pagination. Use returned IDs for details; "
        "never guess IDs. No graph definitions are included.",
        WorkflowList,
    ),
    AssistantTool(
        "get_workflow",
        "Read compact saved workflow facts and an exact graph base reference. "
        "Use read_workflow_snapshot for original fields. Unsaved "
        "edits belong to page snapshots.",
        WorkflowID,
    ),
    AssistantTool(
        "list_node_types",
        "List a compact node catalog, or read fields and handles "
        "for an exact node_type."
        "Read exact schemas before editing fields or constructing nodes.",
        NodeQuery,
    ),
    AssistantTool(
        "get_workflow_logs",
        "Read compact execution facts, exact output counts, errors and small samples. "
        "output_count_path identifies the counted array (root or items); "
        "null counts are unknown, not zero. "
        "Use read_workflow_log for paginated output or full exception text. "
        "Logged failures describe the workflow, not this tool.",
        WorkflowLogQuery,
    ),
    AssistantTool(
        "validate_workflow",
        "Validate the full graph by workflow_id, draft_version and "
        "optional snapshot_id."
        "Use the captured page snapshot for unsaved edits. Never "
        "regenerate the document."
        "This checks structure and fields plus separate static entry-path readiness; "
        "neither valid nor ready proves successful execution.",
        WorkflowValidation,
        admin=True,
    ),
    AssistantTool(
        "preview_workflow_draft",
        "Apply bounded changes to an exact base and preview "
        "against its saved draft_version."
        "Use snapshot_id for captured unsaved edits; changes=[] "
        "previews the unchanged base."
        "Never regenerate unchanged nodes or scripts. On an editable designer, "
        "the app displays the final successful preview directly on that canvas, "
        "then the user keeps and saves it or reverts it. Use this only when asked "
        "to change the canvas. For subsequent edits in this turn, use the latest "
        "proposal_snapshot_id as snapshot_id. You cannot save or publish. "
        "On conflict, ask the user to revert, load the latest draft and request "
        "a new edit. changed_paths are exact element-relative JSON "
        "Pointers with omission counts; validation includes static readiness only.",
        WorkflowPreview,
        admin=True,
    ),
    AssistantTool(
        "read_workflow_log",
        "Read an exact log field or nested path with explicit "
        "pagination; IDs come from get_workflow_logs.",
        LogDetailQuery,
    ),
    AssistantTool(
        "read_workflow_snapshot",
        "Read original graph fields or structure by an exact base. "
        "Use node_id, field_id and path to inspect scripts; "
        "paginate long text.",
        SnapshotQuery,
    ),
    AssistantTool(
        "read_conversation_history",
        "Read a turn's text and original tool records with turn_id, or one tool "
        "with tool_call_id (turn_id optional). Omit both for the index (page with "
        "before). Paginate originals with offset/limit; source_facts are summaries, "
        "not the original field values.",
        HistoryQuery,
    ),
    AssistantTool(
        "navigate",
        "Offer a button to open an existing internal page: workflows or "
        "workflow_designer. The designer requires an accessible "
        "workflow ID from page context or tools. The user decides whether to open "
        "the page and confirms leaving unsaved edits. This does not modify data.",
        NavigationTarget,
    ),
)


def available_tools(user: UserInfo) -> dict[str, AssistantTool]:
    """Select tools granted to the current account by the server.

    Args:
        user: Authenticated account with current resource permissions.

    Returns:
        Read and preview tools; no write or execution tools are registered.
    """
    return {
        tool.name: tool
        for tool in TOOLS
        if not tool.admin or user.role == UserRole.ADMIN
    }


def tool_schemas(user: UserInfo) -> list[dict[str, Any]]:
    """Describe currently authorized tools using their Pydantic argument models.

    Args:
        user: Account whose tool catalog is being requested.

    Returns:
        Chat Completions function definitions, validated again on execution.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.arguments.model_json_schema(),
            },
        }
        for tool in available_tools(user).values()
    ]


def tool_error(code: ErrorCode, guidance: str) -> dict[str, Any]:
    """Build a recoverable tool failure without upstream exception details.

    Args:
        code: Stable application error code.
        guidance: Server-authored recovery instructions for the model.

    Returns:
        Failed tool result suitable for the model and the event stream.
    """
    return {"ok": False, "error": {"code": code, "guidance": guidance}}


async def execute_tool(
    name: str,
    arguments: str,
    user: UserInfo,
    *,
    conversation_id: int | None = None,
    call_id: str | None = None,
    canvas: dict[str, Any] | None = None,
    canvas_snapshot_id: int | None = None,
) -> dict[str, Any]:
    """Validate and invoke a registered tool using the current account's services.

    Args:
        name: Model-selected function name, checked against the server catalog.
        arguments: Complete JSON arguments assembled by the SDK.
        user: Authenticated caller; identity never comes from model arguments.
        conversation_id: Server-owned conversation scope for immutable references.
        call_id: Server-generated call identifier; a preview in a persistent
            conversation records a pending canvas edit that only the user can save.
        canvas: Current turn's captured designer context, never historical context.
        canvas_snapshot_id: Latest successful canvas snapshot in this turn.

    Returns:
        Bounded JSON data or a sanitized recoverable tool error.
    """
    tool = available_tools(user).get(name)
    if tool is None:
        return tool_error(ErrorCode.FORBIDDEN, "Use only the tools provided to you.")
    try:
        args = tool.arguments.model_validate_json(arguments)
    except ValidationError:
        return tool_error(
            ErrorCode.BAD_REQUEST, "Correct the arguments to match the tool schema."
        )

    try:
        async with asyncio.timeout(TOOL_TIMEOUT):
            match args:
                case NavigationTarget():
                    path = WORKFLOW_PATH
                    if args.resource_id is not None:
                        graph = await FlowGraphService.get_accessible(
                            args.resource_id, user
                        )
                        readonly = user.role != UserRole.ADMIN or not graph.editable
                        path += f"/{'r/' if readonly else ''}{graph.id}"
                    result = NavigationResult(target=args, path=path)
                case WorkflowList():
                    result = await workflow_list(GraphQuery(**args.model_dump()), user)
                case LogDetailQuery():
                    result = await log_detail(**args.model_dump(), user=user)
                case WorkflowLogQuery():
                    result = await workflow_logs(args.workflow_id, args.limit, user)
                case WorkflowValidation() | WorkflowPreview() | SnapshotQuery():
                    document = await resolve_document(args, conversation_id, user)
                    if isinstance(args, SnapshotQuery):
                        value = document[args.collection]
                        if args.node_id is not None:
                            value = next(
                                (
                                    node
                                    for node in document["nodes"]
                                    if node["id"] == args.node_id
                                ),
                                None,
                            )
                            if value is None:
                                raise NotFoundException
                            if args.field_id is not None:
                                value = value["data"][args.field_id]
                        for key in args.path:
                            value = value[key]
                        result = {
                            "base": {
                                "workflow_id": args.workflow_id,
                                "draft_version": args.draft_version,
                                "snapshot_id": args.snapshot_id,
                            },
                            **value_page(value, args.offset, args.limit),
                        }
                    elif isinstance(args, WorkflowValidation):
                        result = (
                            await FlowGraphService.validate_graph(
                                args.workflow_id,
                                GraphDocument.model_validate(document),
                                user,
                            )
                        ).model_dump(mode="json")
                        result["base"] = args.model_dump()
                    else:
                        if canvas is not None and (
                            args.workflow_id != canvas["resource_id"]
                            or args.draft_version != canvas["draft_version"]
                            or args.snapshot_id != canvas_snapshot_id
                        ):
                            return tool_error(
                                ErrorCode.ASSISTANT_CANVAS_CHANGED,
                                "Edit only the current canvas. Use its original "
                                "workflow/version and the latest successful "
                                "proposal_snapshot_id, or the page snapshot for "
                                "the first edit. Do not use a saved or historical "
                                "base.",
                            )
                        proposal = apply_changes(document, args.changes)
                        (
                            preview,
                            baseline_changes,
                        ) = await FlowGraphService.preview_draft_with_baseline(
                            args.workflow_id,
                            GraphWrite(
                                **proposal.model_dump(),
                                draft_version=args.draft_version,
                            ),
                            user,
                            GraphDocument.model_validate(document),
                        )
                        result = preview.model_dump(mode="json")
                        result["change_count"] = len(preview.changes)
                        result["changes"] = [
                            {
                                **change.model_dump(exclude={"before", "after"}),
                                "before": value_summary(change.before),
                                "after": value_summary(change.after),
                            }
                            for change in preview.changes[:30]
                        ]
                        result["omitted_changes"] = max(0, len(preview.changes) - 30)
                        result["base"] = {
                            "workflow_id": args.workflow_id,
                            "draft_version": args.draft_version,
                            "snapshot_id": args.snapshot_id,
                        }
                        result["requested_change_count"] = len(args.changes)
                        result["comparison"] = "saved_draft"
                        result["pre_existing_change_count"] = baseline_changes
                        result["base_kind"] = (
                            "snapshot" if args.snapshot_id else "saved_draft"
                        )
                        if conversation_id is not None:
                            from app.services.assistant_sessions import (
                                AssistantSessionService,
                            )

                            snapshot = await AssistantSessionService.snapshot(
                                conversation_id,
                                args.workflow_id,
                                args.draft_version,
                                proposal,
                                "proposal",
                                user,
                            )
                            result["proposal_snapshot_id"] = snapshot.id
                            if call_id is not None and canvas is not None:
                                from app.services.assistant_proposals import (
                                    AssistantProposalService,
                                )

                                proposal_record = await AssistantProposalService.create(
                                    conversation_id,
                                    call_id,
                                    args.workflow_id,
                                    args.draft_version,
                                    snapshot,
                                    canvas["snapshot_id"],
                                )
                                result["proposal_id"] = proposal_record.id
                                # compare with the visible canvas, including
                                # unsaved edits, rather than with the saved draft
                                original = await AssistantSessionService.read_snapshot(
                                    conversation_id, canvas["snapshot_id"], user
                                )
                                result["canvas_change_count"] = len(
                                    FlowGraphService.compare_documents(
                                        original.document, snapshot.document
                                    )
                                )
                        while (
                            result["changes"]
                            and len(dumps(result)) > MAX_TOOL_RESULT_BYTES
                        ):
                            result["changes"].pop()
                            result["omitted_changes"] += 1
                case WorkflowID():
                    result = await workflow_details(
                        args.workflow_id, user, conversation_id
                    )
                case NodeQuery():
                    schemas = FlowGraphService.get_node_schemas(
                        args.category, args.node_type
                    )
                    result = NodeTypesResult.model_validate(
                        loads(dumps(schemas))
                    ).model_dump(mode="json")
                    if args.node_type is None:
                        result = {
                            group: [
                                {
                                    "node_type": node["node_type"],
                                    "name": node["name"],
                                    "categories": node["categories"],
                                }
                                for node in nodes
                            ]
                            for group, nodes in result.items()
                        }
                case HistoryQuery():
                    result = await read_history(args, conversation_id, user)
        data = loads(
            dumps(
                result.model_dump(mode="json")
                if isinstance(result, BaseModel)
                else result
            )
        )
        if len(dumps(data)) > MAX_TOOL_RESULT_BYTES:
            return tool_error(
                ErrorCode.ASSISTANT_RESULT_LIMIT,
                "The result is too large. Narrow the query or reduce the page size.",
            )
        return {"ok": True, "data": data}
    except KaloscopeException as exc:
        code = (
            ErrorCode(exc.message)
            if exc.message in ErrorCode
            else ErrorCode.INTERNAL_SERVER_ERROR
        )
        return tool_error(
            code,
            "For denied access, stop; permissions must change before retrying. "
            "For a missing resource or version conflict, read the latest accessible "
            "list or workflow before retrying. Do not guess IDs or versions.",
        )
    except (ValueError, KeyError, IndexError, TypeError):
        return tool_error(
            ErrorCode.BAD_REQUEST,
            "Check the referenced IDs, paths, and ordered changes. "
            "Do not alter the base version.",
        )
    except TimeoutError:
        return tool_error(
            ErrorCode.AI_TIMEOUT, "The tool timed out; report the failure."
        )
    except Exception:
        return tool_error(
            ErrorCode.INTERNAL_SERVER_ERROR, "The tool failed; report the failure."
        )


async def resolve_document(
    reference: GraphReference, conversation_id: int | None, user: UserInfo
) -> dict:
    """Resolve the full authorized base without model-generated graph content.

    Args:
        reference: Exact resource, version, and optional immutable snapshot.
        conversation_id: Server-owned scope for snapshot lookup.
        user: Account whose business permissions to recheck.

    Returns:
        Complete compressed graph retaining original script text.

    Raises:
        NotFoundException: If the snapshot belongs elsewhere or does not match.
        KaloscopeException: If the saved version has changed.
    """
    graph = await FlowGraphService.get_accessible(reference.workflow_id, user)
    if reference.snapshot_id is not None:
        if conversation_id is None:
            raise NotFoundException
        from app.services.assistant_sessions import AssistantSessionService

        snapshot = await AssistantSessionService.read_snapshot(
            conversation_id, reference.snapshot_id, user
        )
        if (
            snapshot.workflow_id != reference.workflow_id
            or snapshot.draft_version != reference.draft_version
        ):
            raise NotFoundException
        return snapshot.document
    FlowGraphService._check_version(graph, reference.draft_version)
    return graph.draft or {"nodes": [], "edges": []}


async def read_history(
    args: HistoryQuery, conversation_id: int | None, user: UserInfo
) -> dict:
    """Read a bounded historical value after account and resource checks.

    Args:
        args: Exact turn or tool record and pagination. A tool identifier alone
            resolves its owning turn inside the current conversation.
        conversation_id: Current server-owned conversation.
        user: Authenticated owner.

    Returns:
        Paginated turn text and original tool records, or an exact tool record,
        with their turn identity and compact facts retained across compaction.

    Raises:
        NotFoundException: If a reference is missing or belongs elsewhere.
        KaloscopeException: If source resource permissions have changed.
    """
    from app.models.assistant import AssistantToolCall, AssistantTurn
    from app.services.assistant_sessions import AssistantSessionService

    if conversation_id is None:
        raise NotFoundException
    await AssistantSessionService.owned(conversation_id, user)
    turn_id = args.turn_id
    if turn_id is None and args.tool_call_id is not None:
        call = await AssistantToolCall.get_or_none(
            call_id=args.tool_call_id, turn__conversation_id=conversation_id
        )
        if call is None:
            raise NotFoundException
        turn_id = call.turn_id
    if turn_id is None:
        query = AssistantTurn.filter(conversation_id=conversation_id).exclude(
            status="streaming"
        )
        if args.before is not None:
            query = query.filter(id__lt=args.before)
        turns = await query.order_by("-id").limit(6)
        items = []
        for record in turns[:5]:
            if await AssistantSessionService.allowed(record.resource_ids, user):
                items.append(
                    {
                        "turn_id": record.id,
                        "prompt": value_summary(record.prompt),
                        "status": record.status,
                        "tool_calls": await AssistantToolCall.filter(
                            turn_id=record.id
                        ).values("call_id", "name", "status"),
                    }
                )
        return {
            "items": items,
            "next_before": turns[4].id if len(turns) > 5 else None,
            "historical": True,
        }
    turn = await AssistantTurn.get_or_none(id=turn_id, conversation_id=conversation_id)
    if turn is None:
        raise NotFoundException
    if not await AssistantSessionService.allowed(turn.resource_ids, user):
        raise KaloscopeException(ErrorCode.PERMISSION_DENIED, status_code=403)
    source_facts = None
    if args.tool_call_id is not None:
        call = await AssistantToolCall.get_or_none(
            turn_id=turn.id, call_id=args.tool_call_id
        )
        if call is None:
            raise NotFoundException
        value = dumps(
            {"name": call.name, "arguments": call.arguments, "result": call.result}
        ).decode()
        if call.result:
            source_facts = (
                call.result.get("data", {}).get("source_facts")
                if call.name == "read_conversation_history"
                else tool_facts(call.result)
            )
    else:
        calls = await AssistantToolCall.filter(turn_id=turn.id).exclude(
            status="running"
        )
        value = dumps(
            {
                "prompt": turn.prompt,
                "tools": [
                    {
                        "id": call.call_id,
                        "name": call.name,
                        "arguments": call.arguments,
                        "result": call.result,
                    }
                    for call in calls
                ],
                "reply": turn.content,
                "status": turn.status,
                "context": turn.context,
            }
        ).decode()
        facts = [
            tool_facts(call.result)
            for call in calls
            if call.result and call.name != "read_conversation_history"
        ]
        count = len(facts)
        while len(facts) > 8 or len(dumps(facts)) > 6000:
            facts.pop()
        source_facts = {
            "tools": facts,
            "tool_count": count,
            "omitted_tools": count - len(facts),
        }
    return {
        "turn_id": turn.id,
        "historical": True,
        **({"source_facts": source_facts} if source_facts is not None else {}),
        **value_page(value, args.offset, args.limit),
    }
