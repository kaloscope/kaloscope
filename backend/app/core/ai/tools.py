"""Expose bounded, permission-checked workflow tools to the in-app assistant."""

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.flow import (
    GraphCategory,
    GraphDocument,
    GraphQuery,
    GraphState,
    GraphWrite,
)
from app.models.mcp import (
    NodeTypesResult,
    PageResult,
    WorkflowDetails,
    WorkflowLogs,
    WorkflowResult,
)
from app.models.user import UserInfo, UserRole
from app.services.flow import FlowGraphService, FlowLogService
from app.utils.json import dumps, loads

MAX_TOOL_RESULT_BYTES = 256 * 1024
TOOL_TIMEOUT = 15
type ResourceID = Annotated[int, Field(gt=0, strict=True)]


class ToolArguments(BaseModel):
    """Reject unknown arguments without echoing model-generated content."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WorkflowList(ToolArguments):
    """Bound workflow searches and their result pages."""

    name: str | None = Field(default=None, max_length=255)
    category: GraphCategory | None = None
    state: GraphState | None = None
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


class WorkflowValidation(WorkflowID):
    """Validate proposed editor contents without executing nodes."""

    document: GraphDocument


class WorkflowPreview(WorkflowID):
    """Preview changes against the version originally read by the caller."""

    document: GraphWrite


@dataclass(frozen=True)
class AssistantTool:
    """Define a server-owned tool grant and its JSON argument schema."""

    name: str
    description: str
    arguments: type[ToolArguments]
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
        "Read an accessible workflow, its saved draft_version, draft, and published "
        "definition. These do not include unsaved browser edits.",
        WorkflowID,
    ),
    AssistantTool(
        "list_node_types",
        "Read registered node fields, defaults and connection handles. Read these "
        "before composing graphs. Filter by exact node_type to narrow results.",
        NodeQuery,
    ),
    AssistantTool(
        "get_workflow_logs",
        "Read recent retained execution groups for an accessible workflow. Logged "
        "failures describe the workflow, not a failure of this tool.",
        WorkflowLogQuery,
    ),
    AssistantTool(
        "validate_workflow",
        "Check proposed workflow nodes and edges without saving or executing them. "
        "Return issues located by node, field and edge.",
        WorkflowValidation,
        admin=True,
    ),
    AssistantTool(
        "preview_workflow_draft",
        "Compare a proposed graph with its saved draft_version and validate it. "
        "Return changes and an internal editor path. This does not save changes; "
        "on a version conflict, read the latest workflow and review again.",
        WorkflowPreview,
        admin=True,
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


async def execute_tool(name: str, arguments: str, user: UserInfo) -> dict[str, Any]:
    """Validate and invoke a registered tool using the current account's services.

    Args:
        name: Model-selected function name, checked against the server catalog.
        arguments: Complete JSON arguments assembled by the SDK.
        user: Authenticated caller; identity never comes from model arguments.

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
                case WorkflowList():
                    result = PageResult[WorkflowResult].model_validate(
                        loads(
                            dumps(
                                await FlowGraphService.list_graphs(
                                    GraphQuery(**args.model_dump()), user
                                )
                            )
                        )
                    )
                case WorkflowLogQuery():
                    logs = await FlowLogService.get_accessible_logs(
                        args.workflow_id, user
                    )
                    result = WorkflowLogs(runs=loads(dumps(logs[: args.limit])))
                case WorkflowValidation():
                    result = await FlowGraphService.validate_graph(
                        args.workflow_id, args.document, user
                    )
                case WorkflowPreview():
                    result = await FlowGraphService.preview_draft(
                        args.workflow_id, args.document, user
                    )
                case WorkflowID():
                    result = WorkflowDetails.model_validate(
                        loads(
                            dumps(
                                await FlowGraphService.get_details(
                                    args.workflow_id, user
                                )
                            )
                        )
                    )
                case NodeQuery():
                    result = NodeTypesResult.model_validate(
                        loads(
                            dumps(
                                FlowGraphService.get_node_schemas(
                                    args.category, args.node_type
                                )
                            )
                        )
                    )
        data = result.model_dump(mode="json")
        if len(dumps(data)) > MAX_TOOL_RESULT_BYTES:
            return tool_error(
                ErrorCode.AI_ASSISTANT_LIMIT,
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
    except TimeoutError:
        return tool_error(
            ErrorCode.AI_TIMEOUT, "The tool timed out; report the failure."
        )
    except Exception:
        return tool_error(
            ErrorCode.INTERNAL_SERVER_ERROR, "The tool failed; report the failure."
        )
