"""Expose bounded, permission-checked workflow, media, and download tools."""

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from openai.types.chat import ChatCompletionFunctionToolParam
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    WithJsonSchema,
    model_validator,
)
from tortoise.transactions import in_transaction

from app.core.ai.documents import (
    GraphProposal,
    GraphReference,
    apply_changes,
    tool_facts,
    value_summary,
)
from app.core.ai.queries import (
    download_plans,
    download_task_details,
    download_tasks,
    log_detail,
    media_item_details,
    media_items,
    media_libraries,
    trial_run,
    value_page,
    workflow_details,
    workflow_jobs,
    workflow_list,
    workflow_logs,
)
from app.core.dl.driver import DownloadState
from app.core.exceptions import ErrorCode, KaloscopeException, NotFoundException
from app.core.flow.reference import get_workflow_reference
from app.models.assistant import (
    DOWNLOAD_PATHS,
    MEDIA_PATH,
    WORKFLOW_JOBS_PATH,
    WORKFLOW_PATH,
    AssistantToolCall,
    AssistantTurn,
    DownloadOrdering,
    DownloadPlanOrdering,
    ResourceID,
    WorkflowJobOrdering,
    WorkflowOrdering,
    media_resource,
)
from app.models.download import (
    DownloadPlanApply,
    DownloadPlanChange,
    DownloadPlanCheck,
    DownloadPlanCreate,
    DownloadPlanQuery,
    DownloadQuery,
    DownloadTaskCheck,
    DownloadTaskCreate,
    IndexerSearch,
    PlanAction,
    TransferMethod,
)
from app.models.flow import (
    GraphCategory,
    GraphDocument,
    GraphDraftCreate,
    GraphQuery,
    GraphState,
    GraphWrite,
    IntervalUnit,
    JobQuery,
    JobState,
    JobTrigger,
    ScheduleAction,
    ScheduleApply,
    ScheduleChange,
)
from app.models.mcp import NodeTypesResult
from app.models.media import MediaQuery
from app.models.user import UserInfo, UserRole
from app.models.workflow_authoring import HTTPProbe, TemplateValidation
from app.services.download import DownloadPlanService, DownloadTaskService
from app.services.flow import FlowGraphService, FlowJobService, FlowRunService
from app.services.media import MediaItemService, MediaLibService
from app.services.workflow_authoring import (
    probe_http_request,
    validate_workflow_template,
)
from app.utils.json import JSONType, dumps, loads

MAX_TOOL_RESULT_BYTES = 32 * 1024
TOOL_TIMEOUT = 15
CREATE_TOOL = "create_workflow_draft"
SCHEDULE_TOOLS = frozenset({"review_workflow_schedule", "apply_workflow_schedule"})
PLAN_CHANGE_TOOLS = frozenset(
    {"review_download_plan_change", "apply_download_plan_change"}
)
SAFE_INTEGER = 2**53 - 1


def plain_enum(enum: type[StrEnum], description: str | None = None) -> WithJsonSchema:
    """Describe enum values without repeating the enum's full documentation.

    Args:
        enum: String enum still used to validate the argument.
        description: Short replacement for the enum's documentation, if needed.

    Returns:
        A schema override listing the accepted values.
    """
    schema: dict[str, Any] = {"type": "string", "enum": [item.value for item in enum]}
    if description:
        schema["description"] = description
    return WithJsonSchema(schema)


# the creation tool documents categories once; other tools only list the values
type CategoryValue = Annotated[GraphCategory, plain_enum(GraphCategory)]


class ToolArguments(BaseModel):
    """Reject unknown arguments without echoing model-generated content."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class WorkflowList(ToolArguments):
    """Bound workflow searches and their result pages."""

    name: str | None = Field(default=None, max_length=255)
    category: CategoryValue | None = None
    state: (
        Annotated[
            GraphState,
            plain_enum(
                GraphState,
                "draft: never published; modified: published with newer draft "
                "changes; published: draft matches. Not execution status.",
            ),
        ]
        | None
    ) = None
    ordering: WorkflowOrdering = ""
    page_num: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=10, ge=1, le=100, strict=True)


class WorkflowJobList(ToolArguments):
    """Bound schedule queries using the existing page filters and sort fields."""

    name: str | None = Field(default=None, max_length=255)
    state: Annotated[JobState, plain_enum(JobState)] | None = None
    trigger: Annotated[JobTrigger, plain_enum(JobTrigger)] | None = None
    ordering: WorkflowJobOrdering = ""
    page_num: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=10, ge=1, le=25, strict=True)


class WorkflowID(ToolArguments):
    """Identify a workflow from the account's accessible list."""

    workflow_id: ResourceID


class WorkflowLogQuery(WorkflowID):
    """Limit the number of recent execution groups returned."""

    limit: int = Field(default=5, ge=1, le=20, strict=True)


class NodeQuery(ToolArguments):
    """Filter registered node schemas by workflow category and node type."""

    category: CategoryValue | None = None
    node_type: str | None = Field(default=None, min_length=1, max_length=255)
    include_examples: bool = True


class PlanOptions(ToolArguments):
    """Read published search settings and paginated downloader choices."""

    workflow_id: int = Field(gt=0)
    page_num: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class PlanID(ToolArguments):
    """Read one download plan and its recorded first-attempt outcome."""

    plan_id: int = Field(gt=0)


class LibraryIngestion(ToolArguments):
    """Inspect one library's ingestion bindings before planning automatic ingestion."""

    library_id: ResourceID


class PlanList(ToolArguments):
    """Page accessible plans with the plan page's filters and sort fields."""

    workflow_id: ResourceID | None = None
    keyword: str | None = Field(default=None, max_length=255)
    ordering: DownloadPlanOrdering = ""
    page_num: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=10, ge=1, le=25, strict=True)


class ReferenceQuery(ToolArguments):
    """Select a bundled workflow authoring reference."""

    topic: Literal[
        "context",
        "templates",
        "json_indexer",
        "html_indexer",
        "indexer_auth",
        "movie_scraper",
        "tv_scraper",
    ]


class WorkflowValidation(GraphReference):
    """Validate an exact saved version or immutable captured canvas."""


class WorkflowPreview(GraphProposal):
    """Apply bounded edits and preview them against the original saved version."""


class WorkflowRun(GraphReference):
    """Run an exact reviewed saved version or snapshot once from one entry."""

    entry: str = Field(max_length=64, description="Start node type, e.g. search_start.")
    params: dict[str, JSONType] = Field(
        default_factory=dict, description="Fields as in its request example."
    )


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
        description="Authored node.data field (url/headers), never data; "
        "omit for node.",
    )
    path: list[str | int] = Field(
        default_factory=list,
        max_length=10,
        description="Nested keys; integers index arrays, e.g. [0, 'value'].",
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


class WorkflowCreate(ToolArguments):
    """Describe a new workflow without accepting any graph content."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=64)
    category: GraphCategory
    description: str = Field(
        min_length=1, max_length=512, description="Purpose; saved as the description."
    )


class MediaLibraryList(ToolArguments):
    """List accessible media libraries without filters."""


class MediaItemList(ToolArguments):
    """Bound media searches within accessible libraries."""

    library_id: ResourceID | None = None
    keyword: str | None = Field(default=None, max_length=255)
    path: str | None = Field(
        default=None, max_length=4096, description="Exact saved item path"
    )
    page_num: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=10, ge=1, le=50, strict=True)


class MediaItemQuery(ToolArguments):
    """Select one accessible item and an optional bounded detail page."""

    item_id: ResourceID
    section: Literal["summary", "metadata", "children"] = "summary"
    field: str | None = Field(default=None, max_length=128, description="Metadata key")
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=20, ge=1, le=2000, strict=True)


class DownloadList(ToolArguments):
    """Bound shared download searches and their result pages."""

    scope: Literal["all", "active", "completed"] = "all"
    state: Annotated[DownloadState, plain_enum(DownloadState)] | None = None
    name: str | None = Field(default=None, max_length=255)
    downloader_id: ResourceID | None = None
    ordering: DownloadOrdering = ""
    page_num: int = Field(default=1, ge=1, strict=True)
    page_size: int = Field(default=10, ge=1, le=25, strict=True)


class DownloadDetails(ToolArguments):
    """Read one shared task and a character page of its error message."""

    task_id: ResourceID
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=2000, ge=1, le=2000, strict=True)


class TaskCreate(ToolArguments):
    """Submit a result validated in an earlier turn of this conversation."""

    request_id: UUID
    confirmed: bool = Field(
        default=False,
        description="True only after the user approves this result and settings.",
    )


class PlanCreate(ToolArguments):
    """Create settings retained by successful validation in this conversation."""

    request_id: UUID
    confirmed: bool = Field(
        default=False,
        description="True only after the user approves these final settings.",
    )


class ScheduleReview(ScheduleChange):
    """Review one schedule change without saving it."""

    action: Annotated[ScheduleAction, plain_enum(ScheduleAction)]
    trigger: Annotated[JobTrigger, plain_enum(JobTrigger)] | None = None
    interval_unit: Annotated[IntervalUnit, plain_enum(IntervalUnit)] | None = None


class ScheduleConfirm(ToolArguments):
    """Apply a schedule review from an earlier turn of this conversation."""

    request_id: UUID
    confirmed: bool = Field(
        default=False,
        description="True only after the user approves this review.",
    )


class PlanChangeReview(DownloadPlanChange):
    """Review one plan update or deletion without saving it."""

    action: Annotated[PlanAction, plain_enum(PlanAction)]
    transfer_method: Annotated[TransferMethod, plain_enum(TransferMethod)] | None = None


class PlanChangeConfirm(ToolArguments):
    """Apply a plan review from an earlier turn of this conversation."""

    request_id: UUID
    confirmed: bool = Field(
        default=False,
        description="True only after the user approves this review.",
    )


class NavigationTarget(ToolArguments):
    """Select a registered application destination without accepting URLs."""

    page: Literal[
        "workflows",
        "workflow_jobs",
        "workflow_designer",
        "media_library",
        "media_item",
        "active_downloads",
        "completed_downloads",
        "download_plans",
    ]
    resource_id: ResourceID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        """Require a resource for workflow designer and media destinations only.

        Returns:
            A complete registered navigation target.

        Raises:
            ValueError: If the resource does not match the destination type.
        """
        listed = (
            self.page in {"workflows", "workflow_jobs", "download_plans"}
            or self.page in DOWNLOAD_PATHS
        )
        if listed != (self.resource_id is None):
            raise ValueError("Resource does not match the destination")
        return self


class NavigationResult(BaseModel):
    """Return a checked destination for an explicit user navigation action."""

    target: NavigationTarget
    path: str
    # media destinations name their library for history permission checks
    library_id: int | None = None


@dataclass(frozen=True)
class AssistantTool:
    """Define a server-owned tool grant and its JSON argument schema."""

    name: str
    description: str
    arguments: type[BaseModel]
    admin: bool = False


TOOLS = (
    AssistantTool(
        "search_indexer",
        "Run selected published search (10s/page<=20); metadata only, no "
        "downloads. Requires user indexer choice.",
        IndexerSearch,
        admin=True,
    ),
    AssistantTool(
        "get_download_plan_options",
        "Read published indexer filters/downloaders; unlock plan validation/creation.",
        PlanOptions,
        admin=True,
    ),
    AssistantTool(
        "validate_download_plan",
        "Check plan/sample without execution; return editor settings/request_id.",
        DownloadPlanCheck,
        admin=True,
    ),
    AssistantTool(
        "create_download_plan",
        "Create a prior turn's reviewed settings after final user "
        "confirmation; reuse request_id. "
        "Active plans queue real search/downloads; future starts wait.",
        PlanCreate,
        admin=True,
    ),
    AssistantTool(
        "validate_download_task",
        "Check a chosen search result's sealed source and settings for a one-time "
        "download. No submission.",
        DownloadTaskCheck,
        admin=True,
    ),
    AssistantTool(
        "create_download_task",
        "Submit a prior turn's validated result once after final confirmation; "
        "reuse request_id. Returns task state without waiting.",
        TaskCreate,
        admin=True,
    ),
    AssistantTool(
        "get_download_plan",
        "Read plan and first-attempt outcome; not live download success.",
        PlanID,
    ),
    AssistantTool(
        "get_library_ingestion",
        "Read a transfer library's ingestion bindings, compatible workflows and setup "
        "rules; no changes. Bindings apply to the whole library.",
        LibraryIngestion,
        admin=True,
    ),
    AssistantTool(
        "list_download_plans",
        "Page accessible plans; all indexers unless workflow_id; keyword=substring. "
        "plan_id != workflow_id. schedule=settings, not execution; total_count/"
        "last_exec/first_execution=recorded attempts. Full plan/changes: "
        "get_download_plan.",
        PlanList,
    ),
    AssistantTool(
        "list_workflows",
        "Page accessible workflow metadata/IDs without graphs.",
        WorkflowList,
    ),
    AssistantTool(
        "get_workflow",
        "Read saved workflow facts/base. Originals: read_workflow_snapshot. "
        "Unsaved edits use page snapshots.",
        WorkflowID,
    ),
    AssistantTool(
        "list_node_types",
        "List nodes; node_type reads fields/handles. include_examples=false omits "
        "read-only guides.",
        NodeQuery,
    ),
    AssistantTool(
        "get_workflow_reference",
        "Read context/template rules or recipes.",
        ReferenceQuery,
    ),
    AssistantTool(
        "probe_http_request",
        "HTTP-node probe; workflow_id reuses login. No login/cache/retry/save. "
        "2000-char sample; object extraction <=256KiB; known secrets hidden.",
        HTTPProbe,
        admin=True,
    ),
    AssistantTool(
        "validate_workflow_template",
        "Check Jinja fixtures: expression=native, json/object/array=JSONC, "
        "end-node modes=business output. strict rejects undefined. "
        "No nodes execute; Python internals blocked.",
        TemplateValidation,
    ),
    AssistantTool(
        "get_workflow_logs",
        "Read execution counts/errors/samples; page originals with read_workflow_log. "
        "Logged failures concern the workflow, not this tool.",
        WorkflowLogQuery,
    ),
    AssistantTool(
        "validate_workflow",
        "Validate exact-base structure/fields/readiness; never executes. Valid "
        "unlocks run_workflow.",
        WorkflowValidation,
        admin=True,
    ),
    AssistantTool(
        "preview_workflow_draft",
        "Preview exact-base edits; changes=[] reads unchanged. Designer: Keep/Revert; "
        "elsewhere reference only. Never saves/publishes. On conflict, revert and "
        "load the latest draft before editing. Remove edges before their nodes.",
        WorkflowPreview,
        admin=True,
    ),
    AssistantTool(
        "run_workflow",
        "Really run a validated version/snapshot once, only on user request; never "
        "saves/publishes. Reports effects, status, output checks.",
        WorkflowRun,
        admin=True,
    ),
    AssistantTool(
        CREATE_TOOL,
        "Create one empty unpublished draft: unique name, category, purpose. "
        "Retries replay it; name_already_exists creates nothing.",
        WorkflowCreate,
        admin=True,
    ),
    AssistantTool(
        "read_workflow_log",
        "Page a log field/path; IDs come from get_workflow_logs.",
        LogDetailQuery,
    ),
    AssistantTool(
        "read_workflow_snapshot",
        "Read exact-base graph originals. Select scripts via node_id/field_id/path; "
        "paginate long text.",
        SnapshotQuery,
    ),
    AssistantTool(
        "read_conversation_history",
        "Read originals by turn_id/tool_call_id; omit both for index pages via "
        "before. Page text with offset/limit. source_facts are summaries.",
        HistoryQuery,
    ),
    AssistantTool(
        "list_media_libraries",
        "List accessible libraries: type, scan state, ingest bindings. No scan.",
        MediaLibraryList,
    ),
    AssistantTool(
        "list_media_items",
        "Page visible media in library order; keyword filters titles (omit for all). "
        "No path=top-level works. total=all matches; page_counts=this page.",
        MediaItemList,
    ),
    AssistantTool(
        "get_media_item",
        "Read item facts, metadata and child samples. Page metadata/children via "
        "offset/limit (items; text characters, limit=2000). No scan/scrape/playback.",
        MediaItemQuery,
    ),
    AssistantTool(
        "list_download_tasks",
        "Page shared stored tasks; active excludes completed. UI 50-row page N="
        "2N-1/2N here. completed=complete; submit_unknown=unconfirmed. "
        "Details/errors: get_download_task.",
        DownloadList,
    ),
    AssistantTool(
        "get_download_task",
        "Read stored task status, bytes, bytes/s, progress 0-100, update time/error. "
        "Null=unknown. Error pages: character offset/limit, next_offset. "
        "No polling/control.",
        DownloadDetails,
    ),
    AssistantTool(
        "list_workflow_jobs",
        "Page schedules by workflow name; newest first; unlocks changes. job_id "
        "!= workflow_id. Times/cron UTC. running=enabled, not executing; "
        "repeatable=concurrent identical parameters; recoverable=recovery; "
        "neither sets timing. Never infer next run or success.",
        WorkflowJobList,
    ),
    AssistantTool(
        "review_workflow_schedule",
        "Review a schedule change; saves nothing. Use listed IDs; ask if ambiguous. "
        "Times need offsets. Summarize changes, defaults, first_run and notes in "
        "the user's language, not JSON. End the turn for confirmation.",
        ScheduleReview,
        admin=True,
    ),
    AssistantTool(
        "apply_workflow_schedule",
        "Apply a prior turn's valid review after the user confirms it; returns at "
        "once. Repeat the same request_id after errors; never re-review to retry.",
        ScheduleConfirm,
        admin=True,
    ),
    AssistantTool(
        "review_download_plan_change",
        "Review an update/delete of a plan read via get_download_plan; saves "
        "nothing. Omitted fields keep values, null clears; the indexer is fixed. "
        "Summarize changes, timing and notes in the user's language, not JSON. "
        "End the turn for confirmation.",
        PlanChangeReview,
        admin=True,
    ),
    AssistantTool(
        "apply_download_plan_change",
        "Apply a prior turn's valid plan review after the user confirms it; starts "
        "no attempt. Repeat the same request_id after errors; never re-review to "
        "retry.",
        PlanChangeConfirm,
        admin=True,
    ),
    AssistantTool(
        "navigate",
        "Offer a user-clicked page button with unsaved-edit checks. "
        "Designer/media: accessible resource_id; lists: omit it.",
        NavigationTarget,
    ),
)


def available_tools(user: UserInfo) -> dict[str, AssistantTool]:
    """Select tools granted to the current account by the server.

    Args:
        user: Authenticated account with current resource permissions.

    Returns:
        Read and preview tools, plus draft and download-plan creation and
        user-requested trial runs for editors. Canvas saves, publication and
        unrequested execution remain user actions.
    """
    return {
        tool.name: tool
        for tool in TOOLS
        if not tool.admin or user.role == UserRole.ADMIN
    }


def compact_schema(value: Any, definitions: dict | None = None) -> Any:
    """Drop generated schema annotations that cost context without guiding calls.

    Generated titles repeat property names, discriminator mappings repeat each
    variant's constant `op`, resource IDs repeat the JavaScript safe-integer bound,
    constant values imply their type, nullable primitive unions can share one
    type list, and the recursive `JSONType` accepts every JSON value. Null defaults
    restate that an optional field may be omitted. Small or singly referenced
    definitions can be inlined. Object definition summaries repeat the named
    operations and fields; field descriptions and enum meanings remain. Execution
    validates against the complete argument models with every constraint intact.

    Args:
        value: JSON schema node generated by Pydantic.
        definitions: Nonrecursive definitions selected for inline expansion.

    Returns:
        An equivalent schema node for the model request.
    """
    if isinstance(value, dict):
        if value == {"$ref": "#/$defs/JSONType"}:
            return {}
        name = value.get("$ref", "").removeprefix("#/$defs/")
        if definitions and name in definitions:
            value = {
                **definitions[name],
                **{key: item for key, item in value.items() if key != "$ref"},
            }
        result = {
            key: compact_schema(item, definitions)
            for key, item in value.items()
            # property schemas are dicts, so fields with these names are kept
            if not (key == "title" and isinstance(item, str))
            and not (key == "type" and isinstance(item, str) and "const" in value)
            and not (key == "discriminator" and "propertyName" in item)
            and not (key == "maximum" and item == SAFE_INTEGER)
            and not (key == "default" and item is None)
        }
        if "$defs" in result:
            result["$defs"] = {
                key: {
                    field: description
                    for field, description in item.items()
                    if field != "description"
                }
                if item.get("type") == "object"
                else item
                for key, item in result["$defs"].items()
                if key != "JSONType" and key not in (definitions or {})
            }
            if not result["$defs"]:
                result.pop("$defs")
        variants = result.get("anyOf")
        if (
            isinstance(variants, list)
            and len(variants) == 2
            and variants[1] == {"type": "null"}
            and isinstance(variants[0].get("type"), str)
        ):
            nullable = variants[0]
            result.pop("anyOf")
            result.update(nullable)
            result["type"] = [nullable["type"], "null"]
            if "enum" in nullable:
                result["enum"] = [*nullable["enum"], None]
        return result
    if isinstance(value, list):
        return [compact_schema(item, definitions) for item in value]
    return value


def tool_schemas(
    user: UserInfo,
    *,
    authoring: bool = False,
    plan_authoring: bool = False,
    plan_only: bool = False,
    ingestion: bool = False,
    trial: bool = False,
    schedule: bool = False,
    plan_change: bool = False,
) -> list[ChatCompletionFunctionToolParam]:
    """Describe currently authorized tools using their Pydantic argument models.

    Args:
        user: Account whose tool catalog is being requested.
        authoring: Focus this recipe turn on workflow construction. Other tools
            return next turn; this changes model context, never authorization.
        plan_authoring: Expose plan tools after reading the source workflows.
        plan_only: Focus a selected indexer's plan conversation on relevant tools.
        ingestion: Allow A1 scraper creation after inspecting a movie/TV library.
        trial: Offer trial runs after a successful static validation this turn.
        schedule: Offer schedule changes after listing schedules this turn or
            after a review in the previous turn.
        plan_change: Offer plan changes after reading a plan this turn or after
            a review in the previous turn.

    Returns:
        Chat Completions function definitions, validated again on execution. The
        argument model's own summary is omitted because the tool description
        already states its purpose.
    """
    schemas = []
    for tool in available_tools(user).values():
        if tool.name == "run_workflow" and not trial:
            continue
        if tool.name in SCHEDULE_TOOLS and not schedule:
            continue
        if tool.name in PLAN_CHANGE_TOOLS and not plan_change:
            continue
        if tool.name in {
            "search_indexer",
            "get_download_plan_options",
            "validate_download_plan",
            "create_download_plan",
            "get_download_plan",
            "list_download_plans",
            "get_library_ingestion",
            "validate_download_task",
            "create_download_task",
        } and not (plan_authoring or plan_only):
            continue
        if (
            plan_only
            and tool.name
            not in {
                "list_workflows",
                "get_workflow",
                "get_download_plan_options",
                "search_indexer",
                "validate_download_plan",
                "create_download_plan",
                "get_download_plan",
                "list_download_plans",
                "get_library_ingestion",
                "validate_download_task",
                "create_download_task",
                "list_media_libraries",
                "list_media_items",
                "get_workflow_logs",
                "read_workflow_log",
                "read_conversation_history",
                "navigate",
                *PLAN_CHANGE_TOOLS,
            }
            and not (
                ingestion
                and tool.name in {"create_workflow_draft", "get_workflow_reference"}
            )
        ):
            continue
        if (
            authoring
            and not (
                trial
                and tool.name
                in {"run_workflow", "get_workflow_logs", "read_workflow_log"}
            )
            and tool.name
            not in {
                "list_workflows",
                "get_workflow",
                "list_node_types",
                "get_workflow_reference",
                "probe_http_request",
                "validate_workflow_template",
                "validate_workflow",
                "preview_workflow_draft",
                "create_workflow_draft",
                "read_workflow_snapshot",
                "read_conversation_history",
                "navigate",
            }
        ):
            continue
        parameters = compact_schema(tool.arguments.model_json_schema())
        parameters.pop("description", None)
        encoded = dumps(parameters)
        definitions = {
            name: value
            for name, value in parameters.get("$defs", {}).items()
            if value.get("type") in {"string", "integer", "number", "boolean"}
            or encoded.count(dumps(f"#/$defs/{name}")) == 1
        }
        parameters = compact_schema(parameters, definitions)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": parameters,
                },
            }
        )
    return schemas


def tool_error(code: ErrorCode, guidance: str) -> dict[str, Any]:
    """Build a recoverable tool failure without upstream exception details.

    Args:
        code: Stable application error code.
        guidance: Server-authored recovery instructions for the model.

    Returns:
        Failed tool result suitable for the model and the event stream.
    """
    return {"ok": False, "error": {"code": code, "guidance": guidance}}


def media_resources(name: str, arguments: dict, data: dict) -> list[str]:
    """Name the media libraries whose data a successful tool result revealed.

    Args:
        name: Executed tool name.
        arguments: Parsed model arguments for that call.
        data: Successful result data.

    Returns:
        Typed library references for later history permission checks; empty for
        tools that return no media data.
    """
    ids: list[int] = []
    if name == "list_media_libraries":
        ids = [item["id"] for item in data["items"]]
    elif name == "list_media_items":
        ids = [item["lib_id"] for item in data["items"] if item["lib_id"]]
        if isinstance(arguments.get("library_id"), int):
            ids.append(arguments["library_id"])
    elif name == "get_media_item" or (name == "navigate" and data.get("library_id")):
        ids = [data["library_id"]]
    return [media_resource(library_id) for library_id in ids]


async def execute_tool(
    name: str,
    arguments: str,
    user: UserInfo,
    *,
    conversation_id: int,
    call_id: str,
    canvas: dict[str, Any] | None = None,
    canvas_snapshot_id: int | None = None,
) -> dict[str, Any]:
    """Validate and invoke a registered tool using the current account's services.

    Args:
        name: Model-selected function name, checked against the server catalog.
        arguments: Complete JSON arguments assembled by the SDK.
        user: Authenticated caller; identity never comes from model arguments.
        conversation_id: Server-owned conversation scope for immutable references.
        call_id: Server-generated call identifier for this conversation's tool call.
        canvas: Current turn's editable designer context, never historical context.
        canvas_snapshot_id: Latest successful canvas snapshot in this turn.

    Returns:
        Bounded JSON data or a sanitized recoverable tool error.
    """
    tool = available_tools(user).get(name)
    if tool is None:
        return tool_error(ErrorCode.FORBIDDEN, "Use only the tools provided to you.")
    try:
        args = tool.arguments.model_validate_json(arguments)
    except ValidationError as exc:
        # explicit model rules state requirements a schema cannot, such as pairing
        rules = [
            error["msg"].removeprefix("Value error, ")[:200]
            for error in exc.errors(include_input=False, include_url=False)
            if error["type"] == "value_error"
        ]
        return tool_error(
            ErrorCode.BAD_REQUEST,
            " ".join(["Correct the arguments to match the tool schema.", *rules[:2]]),
        )

    try:
        async with asyncio.timeout(TOOL_TIMEOUT):
            match args:
                case IndexerSearch():
                    result = await DownloadPlanService.search_indexer(args, user)
                case PlanOptions():
                    result = await DownloadPlanService.authoring_options(
                        **args.model_dump(), user=user
                    )
                case DownloadPlanCheck():
                    result = await DownloadPlanService.validate_creation(args, user)
                case PlanCreate():
                    result = await create_plan(args, conversation_id, call_id, user)
                case PlanID():
                    result = await DownloadPlanService.get_creation(args.plan_id, user)
                case LibraryIngestion():
                    result = await MediaLibService.ingestion_bindings(
                        args.library_id, user
                    )
                case DownloadTaskCheck():
                    result = await DownloadTaskService.validate_request(args, user)
                case TaskCreate():
                    result = await create_task(args, conversation_id, call_id, user)
                case ScheduleChange():
                    result = await FlowJobService.review_change(args, user)
                case ScheduleConfirm():
                    result = await apply_schedule(args, conversation_id, user)
                case DownloadPlanChange():
                    result = await DownloadPlanService.review_change(args, user)
                case PlanChangeConfirm():
                    result = await apply_plan_change(args, conversation_id, user)
                case PlanList():
                    result = await download_plans(
                        DownloadPlanQuery(
                            graph_id=args.workflow_id,
                            keyword=args.keyword,
                            ordering=args.ordering or None,
                            page_num=args.page_num,
                            page_size=args.page_size,
                        ),
                        user,
                    )
                case NavigationTarget(page="media_library", resource_id=int(lib_id)):
                    library = await MediaLibService.get_accessible(lib_id, user)
                    result = NavigationResult(
                        target=args,
                        path=f"{MEDIA_PATH}/{library.id}",
                        library_id=library.id,
                    )
                case NavigationTarget(page="media_item", resource_id=int(item_id)):
                    item = await MediaItemService.get_accessible(item_id, user)
                    result = NavigationResult(
                        target=args,
                        path=f"{MEDIA_PATH}/{item.lib_id}/{item.id}",
                        library_id=item.lib_id,
                    )
                case NavigationTarget(page="active_downloads" | "completed_downloads"):
                    result = NavigationResult(
                        target=args, path=DOWNLOAD_PATHS[args.page]
                    )
                case NavigationTarget(page="workflow_jobs"):
                    result = NavigationResult(target=args, path=WORKFLOW_JOBS_PATH)
                case NavigationTarget(page="download_plans"):
                    result = NavigationResult(target=args, path="/downloads/plans")
                case NavigationTarget():
                    path = WORKFLOW_PATH
                    if args.resource_id is not None:
                        graph = await FlowGraphService.get_accessible(
                            args.resource_id, user
                        )
                        readonly = user.role != UserRole.ADMIN or not graph.editable
                        path += f"/{'r/' if readonly else ''}{graph.id}"
                    result = NavigationResult(target=args, path=path)
                case MediaLibraryList():
                    result = await media_libraries(user)
                case MediaItemList():
                    result = await media_items(
                        MediaQuery(
                            lib_id=args.library_id,
                            keyword=args.keyword,
                            path=args.path,
                            page_num=args.page_num,
                            page_size=args.page_size,
                        ),
                        user,
                    )
                case MediaItemQuery():
                    result = await media_item_details(**args.model_dump(), user=user)
                case DownloadList():
                    result = await download_tasks(
                        DownloadQuery(**args.model_dump(exclude={"scope"})), args.scope
                    )
                case DownloadDetails():
                    result = await download_task_details(**args.model_dump())
                case WorkflowCreate():
                    result = await create_workflow(args, conversation_id, call_id, user)
                case WorkflowList():
                    result = await workflow_list(GraphQuery(**args.model_dump()), user)
                case WorkflowJobList():
                    result = await workflow_jobs(JobQuery(**args.model_dump()), user)
                case LogDetailQuery():
                    result = await log_detail(**args.model_dump(), user=user)
                case WorkflowLogQuery():
                    result = await workflow_logs(args.workflow_id, args.limit, user)
                case WorkflowRun():
                    result = await run_reviewed(
                        args, conversation_id, call_id, user, canvas, canvas_snapshot_id
                    )
                    if result.get("ok") is False:
                        # a canvas mismatch is a recoverable tool failure
                        return result
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
                        if canvas is not None:
                            from app.services.assistant_proposals import (
                                AssistantProposalService,
                            )

                            proposal_record = await AssistantProposalService.create(
                                call_id,
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
                case ReferenceQuery():
                    result = get_workflow_reference(args.topic)
                case HTTPProbe():
                    result = await probe_http_request(args, user)
                case TemplateValidation():
                    result = validate_workflow_template(args)
                case NodeQuery():
                    schemas = FlowGraphService.get_node_schemas(
                        args.category,
                        args.node_type,
                        include_examples=args.include_examples,
                    )
                    result = NodeTypesResult.model_validate(
                        loads(dumps(schemas))
                    ).model_dump(mode="json")
                    if args.node_type is None:
                        result = {
                            group: [
                                {
                                    "node_type": node["node_type"],
                                    "categories": node["categories"],
                                }
                                for node in nodes
                            ]
                            for group, nodes in result.items()
                        }
                    else:
                        # omit editor decoration, retaining field constraints and widths
                        for nodes in result.values():
                            for node in nodes:
                                node.pop("icon")
                                node.pop("order")
                                node.pop("name")
                                node.pop("group")
                                if node["width"] is None:
                                    node.pop("width")
                                for handle in node["handles"]:
                                    handle.pop("style", None)
                                    handle.pop("position", None)
                                    if handle.get("tag") is None:
                                        handle.pop("tag", None)
                                for field in node["fields"]:
                                    for key in (
                                        "span",
                                        "tabsize",
                                        "lineLength",
                                        "collapse",
                                        "darkmode",
                                        "copier",
                                        "resetter",
                                        "formatter",
                                        "label",
                                        "tooltip",
                                    ):
                                        field.pop(key, None)
                                    for key, value in list(field.items()):
                                        if value is None and key != "default":
                                            field.pop(key)
                case HistoryQuery():
                    result = await read_history(args, conversation_id, user)
        encoded = dumps(
            result.model_dump(mode="json") if isinstance(result, BaseModel) else result
        )
        if len(encoded) > MAX_TOOL_RESULT_BYTES:
            return tool_error(
                ErrorCode.ASSISTANT_RESULT_LIMIT,
                "The result is too large. Narrow the query or reduce the page size.",
            )
        return {"ok": True, "data": loads(encoded)}
    except KaloscopeException as exc:
        code = (
            ErrorCode(exc.message)
            if exc.message in ErrorCode
            else ErrorCode.INTERNAL_SERVER_ERROR
        )
        if code == ErrorCode.NAME_ALREADY_EXISTS:
            return tool_error(
                code,
                "Nothing was created. Another workflow already uses this name and "
                "is not the requested one. Ask the user for a different name; do "
                "not add suffixes or reuse the existing workflow.",
            )
        return tool_error(
            code,
            "For denied access, stop; permissions must change before retrying. "
            "For a missing resource or version conflict, read the latest accessible "
            "list or record before retrying. Do not guess IDs or versions.",
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
    reference: GraphReference, conversation_id: int, user: UserInfo
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


async def retry_chain(turn_id: int | None, conversation_id: int) -> list[int]:
    """List a turn and the turns it retried, back to the original request.

    Args:
        turn_id: Turn whose retry chain is followed.
        conversation_id: Server-owned conversation containing the turns.

    Returns:
        Turn IDs from the given turn to the original one; the last is the root.
    """
    turn_ids: list[int] = []
    while turn_id is not None and turn_id not in turn_ids:
        turn_ids.append(turn_id)
        turn = await AssistantTurn.get_or_none(
            id=turn_id, conversation_id=conversation_id
        )
        turn_id = turn.retry_of if turn else None
    return turn_ids


async def run_reviewed(
    args: WorkflowRun,
    conversation_id: int,
    call_id: str,
    user: UserInfo,
    canvas: dict[str, Any] | None,
    canvas_snapshot_id: int | None,
) -> dict:
    """Run exact reviewed content once per request, within the repair limit.

    A request is the original turn and its retries: identical calls in it replay
    the same run. It allows the initial run and two repair runs, and no other
    run after one with side effects or an unknown outcome, including a call that
    was interrupted or failed after it may have dispatched.

    Args:
        args: Exact saved version or owned snapshot, entry and parameters.
        conversation_id: Server-owned conversation.
        call_id: Server-owned call record.
        user: Current account whose administrator rights are checked again.
        canvas: Current designer context, whose canvas must be the one that runs.
        canvas_snapshot_id: Latest canvas or proposal snapshot in this turn.

    Returns:
        Shared trial result, a refusal, or a canvas mismatch error.

    Raises:
        NotFoundException: If the call or snapshot is unavailable.
        KaloscopeException: If the saved version has changed.
    """
    import hashlib

    from app.services.assistant_sessions import AssistantSessionService

    call = await AssistantToolCall.get_or_none(
        call_id=call_id, turn__conversation_id=conversation_id
    )
    if call is None:
        raise NotFoundException
    if (
        canvas is not None
        and args.workflow_id == canvas["resource_id"]
        and (
            args.draft_version != canvas["draft_version"]
            or args.snapshot_id != canvas_snapshot_id
        )
    ):
        return tool_error(
            ErrorCode.ASSISTANT_CANVAS_CHANGED,
            "Run the current canvas: its workflow/version and the latest "
            "proposal_snapshot_id, or the page snapshot before any edit.",
        )
    graph = await FlowGraphService.get_accessible(args.workflow_id, user)
    FlowGraphService._check_version(graph, args.draft_version)
    source = "saved_draft"
    document = graph.draft or {"nodes": [], "edges": []}
    if args.snapshot_id is not None:
        snapshot = await AssistantSessionService.read_snapshot(
            conversation_id, args.snapshot_id, user
        )
        if (
            snapshot.workflow_id != args.workflow_id
            or snapshot.draft_version != args.draft_version
        ):
            raise NotFoundException
        source, document = snapshot.origin, snapshot.document
    turn_ids = await retry_chain(call.turn_id, conversation_id)
    digest = FlowRunService.digest(document, args.entry, args.params)
    key = hashlib.sha256(
        f"{user.id}:{conversation_id}:{turn_ids[-1]}:{args.workflow_id}:{digest}".encode()
    ).hexdigest()[:16]
    keys: set[str] = set()
    blocked = False
    for record in (
        await AssistantToolCall.filter(turn_id__in=turn_ids, name="run_workflow")
        .exclude(id=call.id)
        .order_by("id")
    ):
        result = record.result or {}
        data = result.get("data") or {}
        try:
            other = WorkflowRun.model_validate_json(record.arguments)
        except ValidationError:
            # rejected arguments never reached a run
            continue
        if other.workflow_id != args.workflow_id:
            continue
        if data.get("executed"):
            keys.add(data["run_key"])
            blocked |= data.get("rerun") == "ask" or data.get("status") in {
                "running",
                "unknown",
            }
        elif other != args and (
            record.status in {"running", "interrupted"}
            or (result.get("error") or {}).get("code")
            in {ErrorCode.AI_TIMEOUT, ErrorCode.INTERNAL_SERVER_ERROR}
            or data.get("status") == "submitting"
        ):
            # an identical call reads the same run; another may follow an unseen one
            blocked = True
    if key not in keys:
        message = None
        if len(keys) >= 3:
            message = (
                "Repair limit reached: report the remaining failure and missing "
                "evidence instead of running again."
            )
        elif blocked:
            message = (
                "An earlier run in this request had effects or an unknown outcome; "
                "ask the user before running again."
            )
        if message:
            return {
                "workflow_id": args.workflow_id,
                "executed": False,
                "status": "refused",
                "message": message,
            }
    return await trial_run(
        args.workflow_id,
        document,
        target={
            "workflow_id": args.workflow_id,
            "draft_version": args.draft_version,
            "snapshot_id": args.snapshot_id,
            "source": source,
        },
        entry=args.entry,
        params=args.params,
        key=key,
        user=user,
        repairs_left=max(0, 3 - len(keys | {key})),
    )


async def create_plan(
    args: PlanCreate, conversation_id: int, call_id: str, user: UserInfo
) -> dict:
    """Recover a plan across retries and create only a reviewed request identity.

    Args:
        args: Validated request ID and explicit approval of its final settings.
        conversation_id: Server-owned conversation.
        call_id: Server-owned call record.
        user: Current account whose administrator rights are checked by the service.

    Returns:
        Shared creation result, replaying the first committed plan in this request.

    Raises:
        NotFoundException: If the call or validation record is unavailable.
    """
    import hashlib

    from app.models.download import DownloadPlan

    call = await AssistantToolCall.get_or_none(
        call_id=call_id, turn__conversation_id=conversation_id
    )
    if call is None:
        raise NotFoundException
    turn_ids = await retry_chain(call.turn_id, conversation_id)
    for earlier in (
        await AssistantToolCall.filter(
            turn_id__in=turn_ids, name="create_download_plan"
        )
        .exclude(id=call.id)
        .order_by("id")
    ):
        try:
            request = PlanCreate.model_validate_json(earlier.arguments)
        except ValidationError:
            continue
        key = hashlib.sha256(f"{user.id}:{request.request_id}".encode()).hexdigest()
        plan = await DownloadPlan.get_or_none(creation_key=key)
        if plan is not None:
            return await DownloadPlanService.creation_result(plan, user, replayed=True)
    records = (
        await AssistantToolCall.filter(
            turn__conversation_id=conversation_id,
            name="validate_download_plan",
        )
        .order_by("-id")
        .limit(30)
    )
    for index, record in enumerate(records):
        data = (record.result or {}).get("data") or {}
        if data.get("valid") and data.get("request_id") == str(args.request_id):
            # an editor may already have created this reviewed proposal
            key = hashlib.sha256(f"{user.id}:{args.request_id}".encode()).hexdigest()
            plan = await DownloadPlan.get_or_none(creation_key=key)
            if plan is not None:
                return await DownloadPlanService.creation_result(
                    plan, user, replayed=True
                )
            reviewed_turn = await AssistantTurn.get(id=record.turn_id)
            if (
                index
                or record.turn_id in turn_ids
                or reviewed_turn.status != "complete"
            ):
                return {
                    **data,
                    "created": False,
                    "confirmation_required": True,
                    "message": (
                        "Show the latest validated settings and defaults, finish the "
                        "turn, and wait for the user's final confirmation before "
                        "creating."
                    ),
                }
            review = data.get("ingestion")
            return await DownloadPlanService.create_once(
                DownloadPlanCreate(
                    request_id=args.request_id,
                    plan=data["plan"],
                    # the reviewed binding is applied with the plan it was shown for
                    ingestion={
                        "workflow_id": review["workflow_id"],
                        "revision": review["revision"],
                    }
                    if review
                    else None,
                    confirmed=args.confirmed,
                ),
                user,
            )
    raise NotFoundException


async def create_task(
    args: TaskCreate, conversation_id: int, call_id: str, user: UserInfo
) -> dict:
    """Submit a reviewed result once, recovering earlier submissions of the request.

    Args:
        args: Validated request ID and explicit approval of its result and settings.
        conversation_id: Server-owned conversation.
        call_id: Server-owned call record.
        user: Current account whose administrator rights are checked by the service.

    Returns:
        Shared submission result; repeated confirmations replay the same task.

    Raises:
        NotFoundException: If the call or validation record is unavailable.
    """
    call = await AssistantToolCall.get_or_none(
        call_id=call_id, turn__conversation_id=conversation_id
    )
    if call is None:
        raise NotFoundException
    records = (
        await AssistantToolCall.filter(
            turn__conversation_id=conversation_id, name="validate_download_task"
        )
        .order_by("-id")
        .limit(30)
    )
    for index, record in enumerate(records):
        data = (record.result or {}).get("data") or {}
        if not (data.get("valid") and data.get("request_id") == str(args.request_id)):
            continue
        reviewed = await AssistantTurn.get(id=record.turn_id)
        # the service replays a committed request before any other check
        replayable = index == 0 and (
            record.turn_id != call.turn_id and reviewed.status == "complete"
        )
        checked = DownloadTaskCheck.model_validate_json(record.arguments)
        review = data.get("ingestion")
        body = DownloadTaskCreate(
            request_id=args.request_id,
            info_hash=data["download"]["info_hash"],
            **checked.model_dump(exclude={"ingest_workflow_id", "transfer_samples"}),
            ingestion={
                "workflow_id": review["workflow_id"],
                "revision": review["revision"],
            }
            if review
            else None,
            confirmed=args.confirmed and replayable,
        )
        result = await DownloadTaskService.create_once(body, user)
        if not replayable and not result.get("created"):
            return {
                **result,
                "created": False,
                "confirmation_required": True,
                "message": "Show the latest validated result and settings, finish "
                "the turn, and wait for the user's final confirmation.",
            }
        return result
    raise NotFoundException


async def apply_schedule(
    args: ScheduleConfirm, conversation_id: int, user: UserInfo
) -> dict:
    """Apply a schedule review the user confirmed after it was shown.

    Only the newest review of a schedule (or of a new schedule for a workflow)
    may be applied, and it must be valid and from a completed earlier turn. The
    active turn is never complete, so its own reviews wait. A committed request
    replays first, so retries never repeat it.

    Args:
        args: Reviewed request ID and the user's confirmation.
        conversation_id: Server-owned conversation.
        user: Current account whose rights the service checks again.

    Returns:
        Shared outcome, or a confirmation request for a review that is not ready.

    Raises:
        NotFoundException: If the review record is unavailable.
    """
    reviews = []
    for record in (
        await AssistantToolCall.filter(
            turn__conversation_id=conversation_id, name="review_workflow_schedule"
        )
        .order_by("-id")
        .limit(30)
    ):
        data = (record.result or {}).get("data") or {}
        if "valid" in data:
            target = (data["job_id"], None if data["job_id"] else data["workflow_id"])
            reviews.append((record, data, target))
    for index, (record, data, target) in enumerate(reviews):
        if data.get("request_id") != str(args.request_id):
            continue
        reviewed = await AssistantTurn.get(id=record.turn_id)
        superseded = any(other == target for _, _, other in reviews[:index])
        ready = not superseded and reviewed.status == "complete"
        result = await FlowJobService.apply_change(
            ScheduleApply(
                request_id=args.request_id,
                change=ScheduleChange.model_validate_json(record.arguments),
                revision=data["revision"],
                confirmed=args.confirmed and ready,
            ),
            user,
        )
        if not ready and not result.get("replayed"):
            return {
                **result,
                "applied": False,
                "confirmation_required": True,
                "message": "A newer review replaced this one; confirm that one."
                if superseded
                else "Show this review, finish the turn and wait for the user's "
                "confirmation.",
            }
        return result
    raise NotFoundException


async def apply_plan_change(
    args: PlanChangeConfirm, conversation_id: int, user: UserInfo
) -> dict:
    """Apply a plan review the user confirmed after it was shown.

    Only the newest review of a plan may be applied, and it must be valid and from
    a completed earlier turn; a newer review, even an invalid one, replaces it.
    The active turn is never complete, so its own reviews wait. A committed
    request replays first, so retries never repeat it.

    Args:
        args: Reviewed request ID and the user's confirmation.
        conversation_id: Server-owned conversation.
        user: Current account whose rights the service checks again.

    Returns:
        Shared outcome, or a confirmation request for a review that is not ready.

    Raises:
        NotFoundException: If the review record is unavailable.
    """
    reviews = [
        (record, data)
        for record in (
            await AssistantToolCall.filter(
                turn__conversation_id=conversation_id,
                name="review_download_plan_change",
            )
            .order_by("-id")
            .limit(30)
        )
        if "valid" in (data := (record.result or {}).get("data") or {})
    ]
    for index, (record, data) in enumerate(reviews):
        if data.get("request_id") != str(args.request_id):
            continue
        reviewed = await AssistantTurn.get(id=record.turn_id)
        superseded = any(
            other["plan_id"] == data["plan_id"] for _, other in reviews[:index]
        )
        ready = not superseded and reviewed.status == "complete"
        result = await DownloadPlanService.apply_change(
            DownloadPlanApply(
                request_id=args.request_id,
                change=DownloadPlanChange.model_validate_json(record.arguments),
                revision=data["revision"],
                confirmed=args.confirmed and ready,
            ),
            user,
        )
        if not ready and not result.get("replayed"):
            return {
                **result,
                "applied": False,
                "confirmation_required": True,
                "message": "A newer review replaced this one; confirm that one."
                if superseded
                else "Show this review, finish the turn and wait for the user's "
                "confirmation.",
            }
        return result
    raise NotFoundException


async def create_workflow(
    args: WorkflowCreate, conversation_id: int, call_id: str, user: UserInfo
) -> dict:
    """Create at most one empty draft per request and record it with its tool call.

    The workflow and the call's successful result commit in one transaction. A
    repeated call or a retry of the same request finds that record and returns the
    same workflow; without the record nothing was created, even when the original
    response was lost.

    Args:
        args: Validated name, category, and purpose.
        conversation_id: Server-owned conversation of the current turn.
        call_id: Server-generated identifier of this tool call.
        user: Account whose editor rights the creation service checks.

    Returns:
        Workflow identity, its empty unpublished draft state, and designer path;
        `replayed` marks a workflow created earlier for this request.

    Raises:
        NotFoundException: If the call record or the recorded workflow is missing.
        ForbiddenException: If the account cannot edit workflows.
        KaloscopeException: If the name already exists.
    """
    call = await AssistantToolCall.get_or_none(
        call_id=call_id, turn__conversation_id=conversation_id
    )
    if call is None:
        raise NotFoundException
    FlowGraphService.require_editor(user)
    turn_ids: list[int] = []
    turn_id = call.turn_id
    # a retry repeats its original request, so include every earlier attempt
    while turn_id is not None and turn_id not in turn_ids:
        turn_ids.append(turn_id)
        turn = await AssistantTurn.get_or_none(
            id=turn_id, conversation_id=conversation_id
        )
        turn_id = turn.retry_of if turn else None
    earlier = (
        await AssistantToolCall.filter(
            turn_id__in=turn_ids, name=CREATE_TOOL, status="complete"
        )
        .exclude(id=call.id)
        .order_by("id")
        .first()
    )
    if earlier is not None and earlier.result:
        data = earlier.result["data"]
        await FlowGraphService.get_accessible(data["workflow_id"], user)
        return {**data, "replayed": True}
    async with in_transaction():
        graph = await FlowGraphService.create_draft(
            GraphDraftCreate(
                name=args.name,
                description=args.description,
                category=args.category,
                document=GraphDocument(nodes=[], edges=[]),
            ),
            user,
        )
        data = {
            "workflow_id": graph.id,
            "name": graph.name,
            "category": str(graph.category),
            "description": graph.description,
            "state": str(graph.state),
            "draft_version": graph.draft_version,
            "node_count": 0,
            "editor_path": f"{WORKFLOW_PATH}/{graph.id}",
            "published": False,
            "executed": False,
            "replayed": False,
        }
        await AssistantToolCall.filter(id=call.id).update(
            result={"id": call_id, "name": CREATE_TOOL, "ok": True, "data": data},
            status="complete",
        )
    return data


async def read_history(
    args: HistoryQuery, conversation_id: int, user: UserInfo
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
    from app.services.assistant_sessions import AssistantSessionService

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
