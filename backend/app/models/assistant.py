"""Validate stateless assistant conversations and public stream events."""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.ai import AIModelSelection
from app.models.flow import GraphCategory, GraphDocument, GraphState
from app.utils.json import JSONType, dumps

MAX_HISTORY_BYTES = 128 * 1024
MAX_HISTORY_DATA_BYTES = 256 * 1024
MAX_PAGE_CONTEXT_BYTES = 128 * 1024
WORKFLOW_PATH = "/settings/workflows/graphs"
type ResourceID = Annotated[int, Field(gt=0, le=2**53 - 1, strict=True)]
type WorkflowOrdering = Literal[
    "",
    "name",
    "-name",
    "category",
    "-category",
    "updated_at",
    "-updated_at",
    "state",
    "-state",
]


class ContextModel(BaseModel):
    """Reject unregistered context fields and implicit scalar coercion."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class WorkflowFilters(ContextModel):
    """Describe the visible workflow list without accepting arbitrary query keys."""

    name: str = Field(default="", max_length=255)
    category: Annotated[GraphCategory, Field(strict=False)] | None = None
    state: Annotated[GraphState, Field(strict=False)] | None = None
    ordering: WorkflowOrdering = ""
    page_num: int = Field(default=1, ge=1)
    page_size: int = Field(default=15, ge=1, le=100)


class WorkflowListContext(ContextModel):
    """Identify selected workflows and the current list filters."""

    page_type: Literal["workflows"]
    route: Literal["/settings/workflows/graphs"]
    selected_ids: list[ResourceID] = Field(default_factory=list, max_length=100)
    filters: WorkflowFilters = Field(default_factory=WorkflowFilters)


class WorkflowDesignerContext(ContextModel):
    """Capture the visible canvas independently of the saved workflow draft."""

    page_type: Literal["workflow_designer"]
    route: str = Field(max_length=160)
    resource_id: ResourceID
    draft_version: int = Field(ge=0)
    unsaved: bool
    canvas: GraphDocument
    selected_node_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=500
    )

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        """Require the route and selected nodes to identify this canvas.

        Returns:
            Consistent designer context.

        Raises:
            ValueError: If the route or selection references another resource.
        """
        if self.route not in {
            f"{WORKFLOW_PATH}/{self.resource_id}",
            f"{WORKFLOW_PATH}/r/{self.resource_id}",
        } or not set(self.selected_node_ids).issubset(
            {node.id for node in self.canvas.nodes}
        ):
            raise ValueError("Context does not match the visible workflow")
        return self


type AssistantPageContext = Annotated[
    WorkflowListContext | WorkflowDesignerContext,
    Field(discriminator="page_type"),
]


class AssistantToolError(ContextModel):
    """Describe a historical tool failure as untrusted conversation data."""

    code: str = Field(min_length=1, max_length=128)
    guidance: str | None = Field(default=None, max_length=1024)


class AssistantToolResult(ContextModel):
    """Retain a bounded result without accepting an authoritative tool message."""

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    ok: bool
    data: dict[str, JSONType] | None = None
    error: AssistantToolError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Require exactly the payload matching the reported historical outcome.

        Returns:
            A consistently shaped historical result, not proof of execution.

        Raises:
            ValueError: If the success or failure payload is missing or ambiguous.
        """
        if self.ok:
            if self.data is None or self.error is not None:
                raise ValueError("A successful result requires data only")
        elif self.error is None or self.data is not None:
            raise ValueError("A failed result requires an error only")
        return self


class AssistantMessage(BaseModel):
    """Accept text and historical data without system or tool message roles."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)
    context: AssistantPageContext | None = None
    tool_results: list[AssistantToolResult] = Field(default_factory=list, max_length=8)


class AssistantChat(BaseModel):
    """Request one assistant turn using an explicitly configured model."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    model: AIModelSelection
    messages: list[AssistantMessage] = Field(min_length=1, max_length=32)
    context: AssistantPageContext | None = None

    @model_validator(mode="after")
    def validate_history(self) -> Self:
        """Require alternating, nonblank turns within the conversation byte limit.

        Returns:
            Validated conversation ending in the current user request.

        Raises:
            ValueError: If roles, text, or context exceed the request boundaries.
        """
        for index, message in enumerate(self.messages):
            expected = "user" if index % 2 == 0 else "assistant"
            if message.role != expected or not message.content.strip():
                raise ValueError("Use alternating nonblank user and assistant turns")
            if message.role == "user" and message.tool_results:
                raise ValueError("Attach tool results to their assistant reply")
            if message.role == "assistant" and message.context is not None:
                raise ValueError("Attach page snapshots to their user request")
            if len({result.id for result in message.tool_results}) != len(
                message.tool_results
            ):
                raise ValueError(
                    "Historical tool result IDs must be unique within a turn"
                )
        if self.messages[-1].role != "user":
            raise ValueError("End the conversation with a user request")
        if self.messages[-1].context is not None:
            raise ValueError("Supply the current page snapshot through context")
        if (
            sum(len(item.content.encode()) for item in self.messages)
            > MAX_HISTORY_BYTES
        ):
            raise ValueError("Conversation exceeds the context limit")
        try:
            history_data = [
                message.model_dump(
                    mode="json", include={"context", "tool_results"}, exclude_none=True
                )
                for message in self.messages[:-1]
            ]
            if len(dumps(history_data)) > MAX_HISTORY_DATA_BYTES:
                raise ValueError(
                    "Historical page and tool data exceed the context limit"
                )
            for context in [
                self.context,
                *(message.context for message in self.messages[:-1]),
            ]:
                if context is None:
                    continue
                if len(dumps(context.model_dump(mode="json"))) > MAX_PAGE_CONTEXT_BYTES:
                    raise ValueError("Page context exceeds the context limit")
        except (TypeError, OverflowError) as exc:
            raise ValueError(
                "Conversation data contain unsupported JSON values"
            ) from exc
        return self


class AssistantEvent(BaseModel):
    """Carry a named SSE event without the ordinary API response envelope."""

    event: Literal["start", "text_delta", "tool_start", "tool_result", "done", "error"]
    data: dict[str, Any]
