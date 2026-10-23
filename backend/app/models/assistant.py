"""Define owned assistant conversations, immutable snapshots, and requests."""

from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

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
from app.utils.json import dumps

MAX_PROMPT_BYTES = 128 * 1024
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
    """Reference the visible canvas independently of the saved workflow draft."""

    page_type: Literal["workflow_designer"]
    route: str = Field(max_length=160)
    resource_id: ResourceID
    draft_version: int = Field(ge=0)
    unsaved: bool
    # the visible designer accepts direct canvas edits; the server narrows it to
    # actual editor rights and workflow editability before storing the turn
    editable: bool = False
    snapshot_id: ResourceID
    selected_node_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=500
    )

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        """Require the route and editing mode to identify this workflow.

        Returns:
            Consistent designer context.

        Raises:
            ValueError: If the route is invalid or a read-only route claims editing.
        """
        editor_route = f"{WORKFLOW_PATH}/{self.resource_id}"
        if self.route not in {
            editor_route,
            f"{WORKFLOW_PATH}/r/{self.resource_id}",
        } or (self.editable and self.route != editor_route):
            raise ValueError("Context does not match the visible workflow")
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

    if TYPE_CHECKING:
        user_id: int

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

    if TYPE_CHECKING:
        conversation_id: int

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

    if TYPE_CHECKING:
        turn_id: int

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

    if TYPE_CHECKING:
        conversation_id: int

    conversation = ForeignKeyField("models.AssistantConversation", related_name=False)
    workflow_id = IntField()
    draft_version = IntField()
    digest = CharField(max_length=64)
    origin = CharField(max_length=16)
    document = JSONField()

    class Meta:
        table = "assistant_snapshot"
        unique_together = (("conversation", "workflow_id", "draft_version", "digest"),)


class AssistantProposal(TortoiseModel):
    """Record the durable save outcome of one immutable assistant canvas edit."""

    if TYPE_CHECKING:
        conversation_id: int
        snapshot_id: int
        base_snapshot_id: int | None

    conversation = ForeignKeyField("models.AssistantConversation", related_name=False)
    call_id = CharField(max_length=64, db_index=True)
    snapshot = ForeignKeyField("models.AssistantSnapshot", related_name=False)
    base_snapshot = ForeignKeyField(
        "models.AssistantSnapshot", related_name=False, null=True
    )
    workflow_id = IntField()
    base_version = IntField()
    digest = CharField(max_length=64)
    status = CharField(max_length=16, default="pending")
    saved_version = IntField(null=True)
    error = CharField(max_length=128, null=True)
    attempts = IntField(default=0)
    resolved_at = DatetimeField(null=True)

    class Meta:
        table = "assistant_proposal"
        ordering = ["id"]


class ConversationCreate(ContextModel):
    """Create an account-owned conversation without accepting owner identifiers."""

    title: str = Field(default="", max_length=120)
    model: AIModelSelection


class ConversationRename(ContextModel):
    """Rename one owned conversation."""

    title: str = Field(min_length=1, max_length=120)


class ConversationDelete(ContextModel):
    """Select a bounded batch of owned conversations for deletion."""

    ids: list[ResourceID] = Field(min_length=1, max_length=999)


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
        if not self.prompt.strip() or len(self.prompt.encode()) > MAX_PROMPT_BYTES:
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


class ProposalDecision(ContextModel):
    """Bind a canvas decision to the exact immutable edit shown to the user."""

    workflow_id: ResourceID
    base_version: int = Field(ge=0, strict=True)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProposalConfirm(ProposalDecision):
    """Bind a save to the original immutable designer snapshot."""

    base_snapshot_id: ResourceID
