"""Define owned assistant conversations, immutable snapshots, and requests."""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tortoise.fields import (
    CharField,
    DatetimeField,
    ForeignKeyField,
    IntField,
    JSONField,
    TextField,
)

from app.models.ai import AIModelSelection
from app.models.base import TortoiseModel
from app.models.flow import GraphCategory, GraphDocument, GraphState
from app.utils.json import JSONType, dumps

MAX_HISTORY_BYTES = 128 * 1024
MAX_HISTORY_DATA_BYTES = 256 * 1024
MAX_PAGE_CONTEXT_BYTES = 128 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024
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
    canvas: GraphDocument | None = None
    snapshot_id: ResourceID | None = None
    selected_node_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=500
    )

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        """Require the route and selected nodes to identify this canvas.

        Returns:
            Consistent designer context.

        Raises:
            ValueError: If the source is ambiguous or the route/selection is invalid.
        """
        if self.route not in {
            f"{WORKFLOW_PATH}/{self.resource_id}",
            f"{WORKFLOW_PATH}/r/{self.resource_id}",
        } or (self.canvas is None) == (self.snapshot_id is None):
            raise ValueError("Context does not match the visible workflow")
        if self.canvas is not None and not set(self.selected_node_ids).issubset(
            {node.id for node in self.canvas.nodes}
        ):
            raise ValueError("Selection does not match the visible workflow")
        return self


class AssistantSnapshotRequest(ContextModel):
    """Upload an immutable canvas separately from the bounded model context."""

    workflow_id: ResourceID
    draft_version: int = Field(ge=0)
    canvas: GraphDocument

    @model_validator(mode="after")
    def validate_size(self) -> Self:
        """Bound attachment input before attempting persistence.

        Returns:
            A complete canvas within the existing snapshot budget.

        Raises:
            ValueError: If the document is oversized or cannot be serialized.
        """
        try:
            if len(dumps(self.canvas.model_dump(mode="json"))) > MAX_SNAPSHOT_BYTES:
                raise ValueError("Canvas exceeds the snapshot limit")
        except (TypeError, OverflowError) as exc:
            raise ValueError("Unsupported canvas values") from exc
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
                if (
                    isinstance(context, WorkflowDesignerContext)
                    and context.canvas is None
                ):
                    raise ValueError(
                        "Snapshot references require a persistent conversation"
                    )
                if len(dumps(context.model_dump(mode="json"))) > MAX_PAGE_CONTEXT_BYTES:
                    raise ValueError("Page context exceeds the context limit")
        except (TypeError, OverflowError) as exc:
            raise ValueError(
                "Conversation data contain unsupported JSON values"
            ) from exc
        return self


class AssistantEvent(BaseModel):
    """Carry a named SSE event without the ordinary API response envelope."""

    event: Literal[
        "start",
        "text_delta",
        "tool_start",
        "tool_result",
        "compacting",
        "done",
        "error",
    ]
    data: dict[str, Any]


class AssistantConversation(TortoiseModel):
    """Store one account's conversation and its replaceable context checkpoint."""

    user = ForeignKeyField("models.User", related_name=False, db_index=True)
    title = CharField(max_length=120)
    model = JSONField(default=dict)
    summary = JSONField(default=dict)
    summary_through = IntField(default=0)
    summary_resources = JSONField(default=list)
    active_request = CharField(max_length=64, null=True)
    lease_until = DatetimeField(null=True)

    class Meta:
        table = "assistant_conversation"
        ordering = ["-updated_at", "-id"]


class AssistantTurn(TortoiseModel):
    """Retain the original request and response, including interrupted attempts."""

    conversation = ForeignKeyField("models.AssistantConversation", related_name=False)
    request_id = CharField(max_length=64)
    prompt = TextField()
    content = TextField(default="")
    model = JSONField(default=dict)
    context = JSONField(null=True)
    resource_ids = JSONField(default=list)
    status = CharField(max_length=16, default="streaming")
    error = CharField(max_length=128, null=True)
    usage = JSONField(null=True)
    retry_of = IntField(null=True)

    class Meta:
        table = "assistant_turn"
        ordering = ["id"]
        unique_together = (("conversation", "request_id"),)


class AssistantToolCall(TortoiseModel):
    """Record actual tool execution independently of generated explanations."""

    turn = ForeignKeyField("models.AssistantTurn", related_name=False, db_index=True)
    call_id = CharField(max_length=64, unique=True)
    name = CharField(max_length=255)
    arguments = TextField()
    offset = IntField(default=0)
    status = CharField(max_length=16, default="running")
    result = JSONField(null=True)

    class Meta:
        table = "assistant_tool_call"
        ordering = ["id"]


class AssistantSnapshot(TortoiseModel):
    """Retain an immutable graph independently of rotating workflow revisions."""

    conversation = ForeignKeyField("models.AssistantConversation", related_name=False)
    workflow_id = IntField()
    draft_version = IntField()
    digest = CharField(max_length=64)
    origin = CharField(max_length=16)
    document = JSONField()

    class Meta:
        table = "assistant_snapshot"
        unique_together = (("conversation", "workflow_id", "draft_version", "digest"),)


class ConversationCreate(ContextModel):
    """Create an account-owned conversation without accepting owner identifiers."""

    title: str = Field(default="", max_length=120)
    model: AIModelSelection


class ConversationRename(ContextModel):
    """Rename one owned conversation."""

    title: str = Field(min_length=1, max_length=120)


class AssistantTurnRequest(ContextModel):
    """Submit a new request without accepting client-authored conversation history."""

    model: AIModelSelection
    request_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    prompt: str = Field(min_length=1, max_length=32768)
    context: AssistantPageContext | None = None
    retry_of: ResourceID | None = None

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        """Bound the new request while preserving its exact canvas snapshot.

        Returns:
            A nonblank prompt and serializable bounded page context.

        Raises:
            ValueError: If the prompt or page snapshot exceeds request boundaries.
        """
        if not self.prompt.strip() or len(self.prompt.encode()) > MAX_HISTORY_BYTES:
            raise ValueError("Provide a bounded nonblank prompt")
        try:
            if (
                self.context
                and len(dumps(self.context.model_dump(mode="json")))
                > MAX_PAGE_CONTEXT_BYTES
            ):
                raise ValueError("Page context exceeds the context limit")
        except (TypeError, OverflowError) as exc:
            raise ValueError("Unsupported page context values") from exc
        return self


class AssistantSummary(ContextModel):
    """Validate a bounded summary whose provenance is checked against stored turns."""

    goals: list[str] = Field(default_factory=list, max_length=16)
    constraints: list[str] = Field(default_factory=list, max_length=24)
    decisions: list[str] = Field(default_factory=list, max_length=24)
    open_questions: list[str] = Field(default_factory=list, max_length=16)
    source_turn_ids: list[int] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_size(self) -> Self:
        """Keep the generated checkpoint small enough for future requests.

        Returns:
            A bounded checkpoint with nonblank textual facts.

        Raises:
            ValueError: If generated text is blank or the checkpoint is oversized.
        """
        values = self.goals + self.constraints + self.decisions + self.open_questions
        if not values or any(not item.strip() or len(item) > 1200 for item in values):
            raise ValueError("Invalid summary facts")
        if len(dumps(self.model_dump())) > 16 * 1024:
            raise ValueError("Summary exceeds the checkpoint limit")
        return self
