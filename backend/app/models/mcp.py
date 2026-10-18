"""Describe MCP results without changing the shared services' JSON payloads."""

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    RootModel,
    SerializerFunctionWrapHandler,
    model_serializer,
)

from app.core.dl.driver import DownloadAction, DownloadState
from app.models.ai import AIProtocol, AIProviderPreset
from app.models.download import OfflineDownloadErrorKind
from app.models.flow import (
    GraphCategory,
    GraphRef,
    GraphState,
    IntervalUnit,
    JobState,
    JobTrigger,
)
from app.models.media import IndexState, Language, LibType, MediaFormat, MediaType
from app.utils.json import JSONType


class Result(BaseModel):
    """Preserve service fields and distinguish omitted keys from explicit nulls."""

    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    __pydantic_extra__: dict[str, JSONType] = Field(init=False)

    @model_serializer(mode="wrap")
    def serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Serialize only fields supplied by the business service.

        Args:
            handler: Pydantic serializer for declared and additional JSON fields.

        Returns:
            Original field set, including explicit nulls and service extensions.
        """
        return {
            key: value
            for key, value in handler(self).items()
            if key in self.model_fields_set
        }


class ItemsResult[T](Result):
    """Return an unpaginated collection in the existing `items` object."""

    items: list[T] = Field(description="Records returned by this query.")


class PageResult[T](ItemsResult[T]):
    """Return the total matching count and the requested page of items."""

    total: int = Field(ge=0, description="Total matching records before pagination.")


class RecordResult(Result):
    """Preserve record IDs and timestamps already encoded by the shared JSON utility."""

    id: int = Field(description="Kaloscope record ID, scoped to this resource type.")
    created_at: str | None = Field(
        description="Record creation time in ISO 8601 format; null when unavailable."
    )
    updated_at: str | None = Field(
        description="Last record update in ISO 8601 format; null when unavailable."
    )


class WorkflowResult(RecordResult):
    """Describe workflow metadata and computed execution statistics."""

    name: str
    icon: str | None
    description: str | None
    category: GraphCategory = Field(
        description="Workflow purpose: search indexer, media ingest, or scheduled task."
    )
    revision: int | None = Field(
        description="Saved workflow revision; null when no revision is assigned."
    )
    state: GraphState = Field(
        description="Publication state: `draft` is unpublished, `modified` has "
        "changes to a published workflow, and `published` has no pending draft "
        "changes."
    )
    editable: bool = Field(
        description="Whether the workflow itself allows editing; account permissions "
        "are checked separately."
    )
    tmpl: dict[str, JSONType] | None = Field(
        description="Linked workflow template and repository metadata; null when "
        "unlinked."
    )
    newest_tmpl: dict[str, JSONType] | None = Field(
        default=None,
        description="Available replacement template; null when no update is found. "
        "Omitted when template updates were not checked.",
    )
    success_rate: float | None = Field(
        description="Fraction of retained execution log records with a completion "
        "time, from 0 to 1; null when no records are available."
    )
    average_time: float | None = Field(
        description="Average duration of completed executions in retained logs, in "
        "milliseconds; null when no completed records are available."
    )
    last_exec: str | None = Field(
        description="Latest retained execution start in ISO 8601 format; null when "
        "unknown."
    )
    node_types: list[str] = Field(
        description="Node type identifiers extracted from the saved definition, such "
        "as `ai`; may contain duplicates."
    )
    only_preview: bool = Field(
        description="Whether the published search-start configuration enables preview "
        "only; false when that setting is absent."
    )


class WorkflowDetails(WorkflowResult):
    """Include saved definitions with their existing expanded node schemas."""

    draft: dict[str, JSONType] | None = Field(
        description="Saved editor draft with nodes, edges, and expanded node schemas; "
        "may differ from the published definition. Null when no draft is saved."
    )
    definition: dict[str, JSONType] | None = Field(
        description="Published graph used for execution, with expanded node schemas; "
        "null when no published definition exists."
    )
    logs: list[dict[str, JSONType]] = Field(
        description="Retained execution summaries; use `get_workflow_logs` for "
        "grouped entries with inputs, errors, and return values."
    )


class NodeResult(Result):
    """Describe a node while preserving field and handle subtype properties."""

    node_type: str = Field(
        description="Stable node type identifier referenced by saved graph schemas."
    )
    name: str = Field(description="Registered Python node class name.")
    icon: str
    group: str = Field(
        description="Designer palette group: `start`, `general`, `control`, or `end`."
    )
    order: int = Field(description="Display order within the palette group, ascending.")
    width: str | None = Field(
        description="Node content width for the designer; null uses the default width."
    )
    fields: list[dict[str, JSONType]] = Field(
        description="Editor field definitions with IDs, field types, defaults, and "
        "constraints. Labels and tooltips may be translation keys."
    )
    handles: list[dict[str, JSONType]] = Field(
        description="Input and output connection handle definitions; their IDs are "
        "referenced by graph edges."
    )
    categories: list[GraphCategory] = Field(
        description="Workflow categories that allow this node; an empty list allows "
        "all."
    )


class NodeTypesResult(RootModel[dict[str, list[NodeResult]]]):
    """Map designer palette groups to node schemas; unmatched queries return `{}`."""


class LogEntry(Result):
    """Describe start, node, and completion entries without changing log documents."""

    at: str | None = Field(
        description="Entry timestamp in ISO 8601 format; null when unavailable."
    )
    type: str | None = Field(
        description="`start` for execution start, `end` for completion, or the failed "
        "node type; null when the node type was not recorded."
    )
    data: JSONType = Field(
        description="Saved node data for failure entries; null for start and end "
        "entries."
    )
    document: JSONType = Field(
        description="Startup parameters for `start`, return value for `end`, or "
        "exception details for a failed node. May be null when no details were "
        "recorded."
    )
    error: JSONType = Field(
        default=None,
        description="Node error code from `data.$error`, when recorded. Usually "
        "omitted for start/end entries; may be null for failures without an error "
        "code.",
    )


class WorkflowLogs(Result):
    """Return retained execution groups with the newest run first."""

    runs: list[list[LogEntry]] = Field(
        description="Retained executions, newest first; each contains its ordered log "
        "entries. An empty list means no retained executions."
    )


class LibraryTrigger(GraphRef):
    """Extend an existing graph reference with its saved trigger identity."""

    graph_id: PositiveInt = Field(
        description="Bound workflow ID, usable with workflow tools if access is "
        "permitted."
    )
    asynchronous: bool = Field(
        default=False, description="Whether this ingest trigger runs asynchronously."
    )
    id: int = Field(description="Trigger binding ID, distinct from the workflow ID.")
    graph_name: str = Field(description="Display name of the bound ingest workflow.")


class LibraryResult(RecordResult):
    """Describe persisted media library settings."""

    lib_type: LibType = Field(
        description="Library content kind: movies, TV shows, novels, or comics."
    )
    dir: str = Field(description="Library root directory in the server's filesystem.")
    name: str
    language: Language | None = Field(
        description="Preferred library language; null when unset."
    )
    priority: int = Field(description="Library display order; lower values come first.")
    scan_on_startup: bool = Field(
        description="Whether the application scans this library at startup."
    )
    danmaku_server: str | None = Field(
        description="Configured overlay-comment server URL; null when unset."
    )
    danmaku_ttl: int = Field(
        description="Overlay-comment cache lifetime in hours; zero expires immediately."
    )
    rename_template: str | None = Field(
        description="Template used to rename organized media; null when unset."
    )


class LibraryStatus(LibraryResult):
    """Include the current scan status and bound ingest workflows."""

    scanning: bool = Field(
        description="Whether a scan is active or a non-failed reconciliation task is "
        "queued."
    )
    triggers: list[LibraryTrigger] = Field(
        description="Bound ingest workflows in configured trigger order."
    )


class MediaResult(RecordResult):
    """Describe indexed media using the existing list and relation field shapes."""

    lib_id: int | None = Field(
        default=None,
        description="Owning library ID; may be omitted when the library relation is "
        "expanded.",
    )
    parent_id: int | None = Field(
        default=None,
        description="Parent media item ID; null for top-level works. May be omitted "
        "when the parent relation is expanded.",
    )
    dir: str = Field(description="Media directory in the server's filesystem.")
    path: str = Field(
        description="Saved file or directory path in the server's filesystem; usable "
        "as the exact `path` filter in `list_media_items`."
    )
    name: str = Field(
        description="Saved file or directory name, independent of metadata title."
    )
    hash: str | None = Field(
        description="Saved media file fingerprint; null when unavailable."
    )
    size: int | None = Field(description="File size in bytes; null when unknown.")
    original: dict[str, JSONType] | None = Field(
        description="Original file information captured when indexed; null when "
        "unavailable. This snapshot may differ from the current file."
    )
    visible: bool = Field(
        description="Whether the indexed item is visible in the library."
    )
    format: MediaFormat | None = Field(
        description="Reading source format, such as EPUB or a comic directory; null "
        "when unspecified or inapplicable."
    )
    index_version: str | None = Field(
        description="Saved source version used by reading-content indexing; null when "
        "unavailable."
    )
    index_state: IndexState | None = Field(
        description="Reading-content index state: pending, ready, empty, or error; "
        "null when not tracked. Separate from metadata read status."
    )
    index_error: str | None = Field(
        description="Reading-content indexing error code; null when none is recorded."
    )
    nfo_path: str | None = Field(
        description="Saved video NFO metadata file path on the server; null when "
        "unavailable."
    )
    nfo_mtime: str | None = Field(
        description="Recorded NFO file modification time in ISO 8601 format; null "
        "when unknown."
    )
    nfo_source: str | None = Field(
        description="Metadata source site recorded from the NFO; null when unknown."
    )
    danmaku_path: str | None = Field(
        description="Overlay-comment file path on the server; null when unavailable."
    )
    danmaku_meta: dict[str, JSONType] | None = Field(
        description="Matched overlay-comment series and episode metadata; null when "
        "unmatched."
    )
    unique_id: str | None = Field(
        description="External metadata identifier; distinct from the Kaloscope item "
        "ID. Null when unknown."
    )
    title: str | None = Field(description="Media display title; null when unavailable.")
    year: int | None = Field(
        description="Release or publication year; null when unknown."
    )
    aired: str | None = Field(
        description="Recorded release or air date; null when unknown."
    )
    season: int | None = Field(
        description="Season number; null when unknown or inapplicable."
    )
    episode: int | None = Field(
        description="Episode number; null when unknown or inapplicable."
    )
    poster: str | None = Field(
        description="Poster or cover image reference; null when unavailable."
    )
    backdrop: str | None = Field(
        description="Backdrop image reference; null when unavailable."
    )
    rating: str | int | float | None = Field(
        description="Metadata rating; stored decimal values may be strings while "
        "detail metadata may use numbers. Null when unknown."
    )


class MediaDetails(MediaResult):
    """Include current metadata and visible relatives for all supported media kinds."""

    lib: LibraryResult = Field(
        description="Settings of the item's owning media library."
    )
    parent: MediaResult | None = Field(
        description="Visible parent item in the same library; null for top-level works."
    )
    children: list[MediaResult] = Field(
        description="Visible direct children in the same library, such as episodes or "
        "chapters."
    )
    lib_type: LibType = Field(description="Content kind of the owning library.")
    media_type: MediaType = Field(
        description="Viewer content kind, such as video, image, or text."
    )
    metadata: dict[str, JSONType] | None = Field(
        description="Current local NFO, OPF, or ComicInfo metadata, possibly "
        "including fallback values; inspect `metadata_state` and `metadata_issues` "
        "for read problems."
    )
    metadata_state: str = Field(
        description="Current metadata read status: `ready`, `missing`, or `error`. "
        "Fallback fields can still be present when sources are missing or invalid."
    )
    metadata_issues: list[dict[str, JSONType]] = Field(
        description="Metadata source problems with `source`, `error`, and "
        "`invalid_fields`; an empty list means no reported problems."
    )
    role: str | None = Field(
        default=None,
        description="Reading-item role: `book`, `collection`, or `chapter`; omitted "
        "for video items.",
    )
    metadata_task: dict[str, JSONType] | None = Field(
        default=None,
        description="Reading metadata task with `state` (`pending` or `error`) and "
        "nullable `error` code; null when no task exists, omitted for video items.",
    )


class DownloadResult(RecordResult):
    """Describe the existing download summary without connection or source secrets."""

    name: str
    downloader_id: int = Field(
        description="Configured downloader ID that owns this task."
    )
    state: DownloadState = Field(
        description="Last stored Kaloscope download state, including remote download, "
        "local pulling, and verification phases; not refreshed by this tool."
    )
    raw_state: str | None = Field(
        description="Downloader-specific status before normalization; null when "
        "unavailable."
    )
    error_msg: str | None = Field(
        description="Last stored download error message; null when none is recorded."
    )
    up_speed: int | None = Field(
        description="Last stored upload speed in bytes per second; null when unknown."
    )
    dl_speed: int | None = Field(
        description="Last stored download speed in bytes per second; null when unknown."
    )
    percentage: float | None = Field(
        description="Download progress on a 0 to 100 percentage scale; null when "
        "unknown."
    )
    total_size: int | None = Field(
        description="Total download size in bytes; null when unknown."
    )
    completed_size: int | None = Field(
        description="Downloaded size in bytes; null when unknown."
    )
    completed_at: str | None = Field(
        description="Stored completion time in ISO 8601 format; null when not recorded."
    )
    transfer_lib_id: int | None = Field(
        description="Destination media library ID for organization; null when unset."
    )
    ratio: str = Field(
        description="Formatted download progress, such as '25 MB / 100 MB (25%)'; "
        "empty when sizes are unknown. This is not a torrent share ratio."
    )
    estimate: str = Field(
        description="Formatted download speed with an optional remaining duration; "
        "empty when speed is unknown."
    )
    capabilities: list[DownloadAction] = Field(
        description="Actions supported for this task by its downloader and current "
        "state; these are not operations exposed by the read-only MCP tools."
    )
    error_kind: OfflineDownloadErrorKind | None = Field(
        description="Offline-download failure category; null when no categorized "
        "offline-job error is recorded."
    )


class WorkflowJob(RecordResult):
    """Describe a saved workflow schedule and its current state."""

    graph_id: int = Field(description="Workflow ID executed by this schedule.")
    graph_name: str | None = Field(
        description="Current workflow name; null when the related workflow is "
        "unavailable."
    )
    bootparams: dict[str, JSONType] | None = Field(
        description="Startup parameters passed to each execution; null when unset."
    )
    repeatable: bool = Field(
        description="Whether runs with the same workflow and startup parameters may "
        "execute concurrently; independent of schedule recurrence."
    )
    recoverable: bool = Field(
        description="Whether executions persist progress for recovery after "
        "interruption."
    )
    state: JobState = Field(
        description="Saved schedule state: pending, running, or paused; not the "
        "workflow's publication state."
    )
    trigger: JobTrigger = Field(
        description="Schedule mode: one-time `date`, five-field `cron`, or recurring "
        "`interval`."
    )
    run_date: str | None = Field(
        description="One-time execution date in ISO 8601 format, used by `date` "
        "triggers."
    )
    cron_expr: str | None = Field(
        description="Five-field UTC cron expression used by `cron` triggers."
    )
    interval_num: int | None = Field(
        description="Recurrence interval amount, interpreted with `interval_unit`."
    )
    interval_unit: IntervalUnit | None = Field(
        description="Time unit for `interval_num`, used by `interval` triggers."
    )
    interval_start: str | None = Field(
        description="Optional interval schedule start in ISO 8601 format; null when "
        "unset."
    )
    interval_end: str | None = Field(
        description="Optional interval schedule end in ISO 8601 format; null means no "
        "end date."
    )


class ProviderResult(RecordResult):
    """Describe saved model settings and credential presence without the credential."""

    name: str
    preset: AIProviderPreset | None = Field(
        description="Built-in provider preset; null identifies a custom provider."
    )
    protocol: AIProtocol = Field(description="Configured model API protocol.")
    base_url: str = Field(
        description="Configured API base URL, including any version prefix."
    )
    model_ids: list[str] = Field(
        description="Model IDs saved for this provider; not a live list or proof of "
        "availability."
    )
    enabled: bool = Field(
        description="Whether Kaloscope permits this provider to be used by AI nodes; "
        "does not indicate connection health."
    )
    has_api_key: bool = Field(
        description="Whether an encrypted API key is stored; does not verify "
        "credential validity or connectivity. False can be valid for unauthenticated "
        "local services."
    )
