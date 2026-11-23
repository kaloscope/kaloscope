"""Declare assistant tool catalog profiles, unlock rules, and turn transitions."""

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Literal

from app.core.flow.reference import SELF_CONTAINED_RECIPES

type ActionDomain = Literal[
    "questions", "personal", "workspace", "search", "downloads", "workflows", "media"
]

SCHEDULE_TOOLS = frozenset({"review_workflow_schedule", "apply_workflow_schedule"})
PLAN_CHANGE_TOOLS = frozenset(
    {
        "review_download_plan_change",
        "apply_download_plan_change",
        "review_plan_replacement",
    }
)
TASK_ACTION_TOOLS = frozenset(
    {"review_download_task_action", "apply_download_task_action"}
)
MEDIA_REVIEW_TOOLS = frozenset(
    {
        "review_library_scan",
        "review_media_rescrape",
        "review_library_config",
        "review_media_batch",
    }
)
MEDIA_CHANGE_TOOLS = MEDIA_REVIEW_TOOLS | {"apply_media_change"}
BATCH_CONTROL_TOOLS = frozenset({"get_media_batch", "cancel_media_batch"})

PLAN_AUTHORING_TOOLS = frozenset(
    {
        "get_download_plan_options",
        "validate_download_plan",
        "create_download_plan",
        "get_library_ingestion",
        "validate_download_task",
        "create_download_task",
        "search_indexer",
    }
)
PLAN_RESUME_TOOLS = frozenset(
    {
        "get_download_plan_options",
        "search_indexer",
        "validate_download_plan",
        "validate_download_task",
        "create_download_plan",
        "create_download_task",
    }
)
PREPARING_DOWNLOAD_TOOLS = frozenset(
    {
        "get_download_plan_options",
        "validate_download_plan",
        "validate_download_task",
    }
)
PLAN_FOCUS_TRIGGERS = frozenset(
    {
        "get_download_plan_options",
        "get_library_ingestion",
        "list_download_plans",
    }
)
RECIPE_TOPICS = frozenset(
    {
        "json_indexer",
        "html_indexer",
        "indexer_auth",
        "movie_scraper",
        "tv_scraper",
    }
)
NODE_TRIM_TOOLS = frozenset({"get_download_plan", "list_download_plans"})
CANVAS_TRIM_TOOLS = frozenset({"list_workflows", "create_workflow_draft", "navigate"})
SELF_CONTAINED_TRIM_TOOLS = frozenset({"list_node_types", "get_workflow"})

DOMAIN_COMMON = frozenset({"discover_actions", "navigate", "ask_user"})
DOMAIN_TOOLS: dict[ActionDomain, frozenset[str]] = {
    "questions": frozenset({"ask_user"}),
    "personal": frozenset({"get_personal_preferences", "set_personal_preference"}),
    "workspace": frozenset(
        {
            "inspect_workspace",
            "review_workspace_change",
            "get_media_batch",
            "cancel_media_batch",
        }
    ),
    "workflows": frozenset(
        {
            "list_workflows",
            "get_workflow",
            "create_workflow_draft",
            "get_workflow_reference",
            "list_workflow_jobs",
        }
    ),
    "media": frozenset(
        {
            "list_media_libraries",
            "list_media_items",
            "get_media_item",
            "review_media_batch",
        }
    ),
    "search": frozenset(
        {
            "list_workflows",
            "search_indexer",
            "list_indexer_favorites",
            "set_indexer_favorite",
        }
    ),
    "downloads": frozenset(
        {
            "list_download_plans",
            "get_download_plan",
            "review_download_plan_change",
            "list_download_tasks",
            "get_download_task",
            "diagnose_download_ingestion",
            "get_plan_replacement",
            "review_plan_replacement",
        }
    ),
}
DOMAIN_ONLY_TOOLS = DOMAIN_TOOLS["personal"] | frozenset(
    {"set_indexer_favorite", "list_indexer_favorites"}
)

WORKSPACE_FOCUS = frozenset(
    {
        "discover_actions",
        "inspect_workspace",
        "review_workspace_change",
        "get_media_batch",
        "cancel_media_batch",
        "ask_user",
        "get_workflow",
        "list_workflows",
        "list_media_libraries",
        "get_media_item",
        "navigate",
    }
)
# a file conversation keeps library management available without unrelated domains
BATCH_FOCUS = MEDIA_CHANGE_TOOLS | frozenset(
    {
        "ask_user",
        "discover_actions",
        "get_media_batch",
        "cancel_media_batch",
        "list_media_libraries",
        "list_media_items",
        "get_media_item",
        "list_workflows",
        "get_workflow",
        "read_conversation_history",
        "navigate",
    }
)
PLAN_FOCUS = PLAN_CHANGE_TOOLS | frozenset(
    {
        "discover_actions",
        "ask_user",
        "diagnose_download_ingestion",
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
        "create_workflow_draft",
        "navigate",
        "get_plan_replacement",
    }
)
TRIAL_TOOLS = frozenset({"run_workflow", "get_workflow_logs", "read_workflow_log"})
AUTHORING_FOCUS = frozenset(
    {
        "discover_actions",
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
)


@dataclass(frozen=True)
class CatalogProfile:
    """Describe the active domain, focus modes, and unlocked tool capabilities."""

    domain: ActionDomain | None = None
    authoring: bool = False
    plan_authoring: bool = False
    plan_only: bool = False
    ingestion: bool = False
    trial: bool = False
    schedule: bool = False
    plan_change: bool = False
    task_action: bool = False
    media_change: bool | Collection[str] = False
    batch: bool = False
    workspace: bool = False

    def __post_init__(self) -> None:
        """Freeze mutable `media_change` collections while keeping boolean flags."""
        if not isinstance(self.media_change, (bool, frozenset)):
            object.__setattr__(self, "media_change", frozenset(self.media_change))


type _UnlockRule = tuple[frozenset[str], Callable[[CatalogProfile], bool]]

# Global gates apply even when an explicit domain catalog is active.
GLOBAL_UNLOCK_RULES: tuple[_UnlockRule, ...] = (
    (
        frozenset({"review_workspace_change"}),
        lambda p: p.workspace or p.domain == "workspace",
    ),
    (
        BATCH_CONTROL_TOOLS,
        lambda p: p.batch or p.workspace or p.domain == "workspace",
    ),
)

# Default-mode gates hide specialized tools until their prerequisite state is reached.
DEFAULT_UNLOCK_RULES: tuple[_UnlockRule, ...] = (
    (DOMAIN_ONLY_TOOLS, lambda _p: False),
    (frozenset({"inspect_workspace"}), lambda p: p.workspace),
    (
        frozenset({"ask_user", "get_plan_replacement"}),
        lambda p: bool(
            p.plan_only or p.plan_change or p.task_action or p.batch or p.workspace
        ),
    ),
    # re-enter graph editing through its reference recipe when switching domains
    (
        frozenset({"preview_workflow_draft"}),
        lambda p: (
            not (
                not p.authoring
                and (
                    p.plan_authoring
                    or p.task_action
                    or p.schedule
                    or p.media_change is True
                )
            )
        ),
    ),
    (frozenset({"run_workflow"}), lambda p: p.trial),
    (
        frozenset({"diagnose_download_ingestion"}),
        lambda p: p.plan_change or p.task_action,
    ),
    (SCHEDULE_TOOLS, lambda p: p.schedule),
    (PLAN_CHANGE_TOOLS, lambda p: p.plan_change),
    (TASK_ACTION_TOOLS, lambda p: p.task_action),
    (PLAN_AUTHORING_TOOLS, lambda p: p.plan_authoring or p.plan_only),
)


def is_tool_visible(name: str, profile: CatalogProfile) -> bool:
    """Evaluate whether a permitted tool belongs in the given catalog profile.

    Args:
        name: Registered tool name.
        profile: Declarative domain, focus, and unlock state.

    Returns:
        Whether the tool schema should be included in the model request.
    """
    if profile.workspace and name not in WORKSPACE_FOCUS:
        return False
    for group, allowed in GLOBAL_UNLOCK_RULES:
        if name in group and not allowed(profile):
            return False

    if profile.domain is not None:
        return name in DOMAIN_TOOLS[profile.domain] or name in DOMAIN_COMMON

    for group, allowed in DEFAULT_UNLOCK_RULES:
        if name in group and not allowed(profile):
            return False

    if (
        not profile.batch
        and name in MEDIA_CHANGE_TOOLS
        and (
            not profile.media_change
            or (profile.media_change is not True and name not in profile.media_change)
        )
    ):
        return False

    if profile.batch and name not in BATCH_FOCUS:
        return False
    if (
        profile.plan_only
        and name not in PLAN_FOCUS
        and not (
            (profile.ingestion or profile.plan_change)
            and name == "get_workflow_reference"
        )
    ):
        return False
    return not (
        profile.authoring
        and name not in AUTHORING_FOCUS
        and not (profile.trial and name in TRIAL_TOOLS)
    )


@dataclass
class AssistantCatalog:
    """Manage one assistant turn's catalog state and tool-driven unlock transitions."""

    page_type: str | None = None
    authoring: bool = False
    self_contained: bool = False
    plan_authoring: bool = False
    plan_only: bool = False
    preparing_download: bool = False
    ingestion: bool = False
    trial: bool = False
    schedule: bool = False
    plan_change: bool = False
    task_action: bool = False
    media_change: bool | frozenset[str] = False
    workspace: bool = False
    batch: bool = False
    initial_domain: ActionDomain | None = None

    @classmethod
    def for_turn(
        cls,
        *,
        reviewed: set[str],
        context: dict[str, Any] | None,
        direct_download: bool,
        input_budget: int,
        cloud_input_tokens: int,
    ) -> "AssistantCatalog":
        """Build the starting catalog state from turn context and previous reviews.

        Args:
            reviewed: Tool names that already produced review cards in the prior turn.
            context: Active page context captured for this turn, if any.
            direct_download: Whether the prompt directly targets a subscription or
                download flow.
            input_budget: Input token budget for the active model.
            cloud_input_tokens: Threshold above which batch review is offered early.

        Returns:
            Initialized turn catalog state.
        """
        page_type = (context or {}).get("page_type")
        schedule = "review_workflow_schedule" in reviewed
        plan_change = "review_download_plan_change" in reviewed
        task_action = "review_download_task_action" in reviewed
        # a follow-up turn keeps room for history by offering only its own kind
        media_reviews = reviewed & MEDIA_REVIEW_TOOLS
        media_change: bool | frozenset[str] = (
            frozenset(media_reviews | {"apply_media_change"})
            if media_reviews
            else False
        )
        batch = "review_media_batch" in reviewed and not direct_download
        plan_only = (
            plan_change or direct_download or bool(reviewed & PLAN_RESUME_TOOLS)
        ) and page_type != "workflow_designer"
        search_page = page_type == "web_search"
        if search_page and not direct_download and reviewed <= {"search_indexer"}:
            plan_only = False
        preparing_download = direct_download or bool(
            reviewed & PREPARING_DOWNLOAD_TOOLS
        )
        if (
            media_change is False
            and not plan_only
            and input_budget >= cloud_input_tokens
        ):
            # the model decides at turn start whether library files can change,
            # and a batch review only lists or plans; the floor budget keeps
            # it until media is read, for room
            media_change = frozenset({"review_media_batch"})

        return cls(
            page_type=page_type,
            plan_authoring=plan_only,
            plan_only=plan_only,
            preparing_download=preparing_download,
            schedule=schedule,
            plan_change=plan_change,
            task_action=task_action,
            media_change=media_change,
            batch=batch,
            initial_domain="search" if search_page and not plan_only else None,
        )

    def initial_profile(self) -> CatalogProfile:
        """Return the catalog profile used before the first model turn.

        Returns:
            Profile encoding turn-start domain, focus, and review unlocks.
        """
        return CatalogProfile(
            domain=self.initial_domain,
            plan_authoring=self.plan_authoring,
            plan_only=self.plan_only,
            schedule=self.schedule,
            plan_change=self.plan_change,
            task_action=self.task_action,
            media_change=self.media_change,
            batch=self.batch,
        )

    def _plan_profile(self) -> CatalogProfile:
        """Build the plan-authoring profile with current prerequisite flags.

        Returns:
            Profile enabling plan authoring alongside active unlocks.
        """
        return CatalogProfile(
            plan_authoring=True,
            plan_only=self.plan_only,
            ingestion=self.ingestion,
            trial=self.trial,
            schedule=self.schedule,
            plan_change=self.plan_change,
            task_action=self.task_action,
            media_change=self.media_change,
        )

    def _active_profile(self) -> CatalogProfile:
        """Build the general active profile from all current unlock flags.

        Returns:
            Profile reflecting every unlocked capability in this turn.
        """
        return CatalogProfile(
            authoring=self.authoring,
            plan_authoring=self.plan_authoring,
            plan_only=self.plan_only,
            ingestion=self.ingestion,
            trial=self.trial,
            schedule=self.schedule,
            plan_change=self.plan_change,
            task_action=self.task_action,
            media_change=self.media_change,
        )

    def _authoring_profile(self) -> CatalogProfile:
        """Build the recipe-authoring profile focused on workflow construction.

        Returns:
            Profile focused on authoring while keeping non-plan unlocks.
        """
        return CatalogProfile(
            authoring=True,
            trial=self.trial,
            schedule=self.schedule,
            plan_change=self.plan_change,
            task_action=self.task_action,
            media_change=self.media_change,
        )

    def _triggers_plan_authoring(self, name: str, result: dict[str, Any]) -> bool:
        """Check whether a successful tool result unlocks download plan authoring.

        Args:
            name: Completed tool name.
            result: Successful tool result payload.

        Returns:
            Whether plan authoring tools should be unlocked.
        """
        if self.authoring:
            return False
        data = result["data"]
        return (
            name in PLAN_FOCUS_TRIGGERS
            or (
                self.preparing_download
                and self.plan_only
                and name in {"list_media_items", "search_indexer"}
            )
            or (
                name == "list_workflows"
                and any(item.get("downloadable_search") for item in data["items"])
            )
            or (
                name == "get_workflow"
                and data["category"] == "indexer"
                and data["state"] != "draft"
            )
        )

    def on_tool_result(
        self,
        name: str,
        result: dict[str, Any],
        *,
        build: Callable[[CatalogProfile], list[Any]],
        schemas: list[Any],
        answer_only: bool,
        subscription_answer: bool,
        allow_answer_tool: Callable[[str, bool], bool],
    ) -> list[Any]:
        """Advance unlock flags and rebuild tool schemas after a completed tool call.

        Args:
            name: Executed tool name.
            result: Tool result dict returned by `execute_tool`.
            build: Factory turning a `CatalogProfile` into Chat Completions schemas.
            schemas: Current tool schemas before this result.
            answer_only: Whether this turn continues a structured `ask_user` card.
            subscription_answer: Whether `answer_only` continues a subscription card.
            allow_answer_tool: Predicate filtering tools permitted on answer turns.

        Returns:
            Updated list of tool schemas for subsequent model rounds.
        """
        if not result["ok"]:
            if self.workspace:
                return build(CatalogProfile(workspace=True))
            if self.batch:
                return build(CatalogProfile(batch=True))
            return schemas

        data = result["data"]

        if self._triggers_plan_authoring(name, result):
            # plan pages and offered changes need room, as authoring does
            self.plan_only |= (
                self.plan_change
                or self.page_type == "download_plans"
                or name in PLAN_FOCUS_TRIGGERS
            )
            if name == "get_library_ingestion":
                self.ingestion = data["supported"]
            if name == "get_download_plan_options":
                self.preparing_download = True
            self.plan_authoring = True
            schemas = build(self._plan_profile())

        if not self.trial and name == "validate_workflow" and data.get("valid"):
            # trial runs follow a successful static check of the same turn
            self.trial = True
            schemas = build(self._active_profile())

        if not self.schedule and name == "list_workflow_jobs":
            # schedule changes follow a listing of the current schedules
            self.schedule = True
            schemas = build(self._active_profile())

        if not self.authoring and name == "get_download_plan":
            # plan changes follow reading the plan and focus on plan tools
            self.plan_change = self.plan_authoring = self.plan_only = True
            schemas = build(self._plan_profile())

        if not self.task_action and name in {
            "list_download_tasks",
            "get_download_task",
        }:
            # task actions follow reading the current tasks
            self.task_action = True
            schemas = build(self._active_profile())

        if self.media_change is not True and name in {
            "list_media_libraries",
            "list_media_items",
            "get_media_item",
        }:
            # scans and rescrapes follow reading the current media
            self.media_change = True
            schemas = build(self._active_profile())

        if name == "get_workflow_reference" and data.get("topic") in RECIPE_TOPICS:
            # keep full domain catalogs for ordinary turns, focus recipe builds
            self.authoring = True
            self.self_contained |= data["topic"] in SELF_CONTAINED_RECIPES
            schemas = build(self._authoring_profile())

        if name in {"list_node_types", "validate_workflow"}:
            # keep plan discovery out of node-heavy construction and trial turns
            schemas = [
                schema
                for schema in schemas
                if schema["function"]["name"] not in NODE_TRIM_TOOLS
            ]

        if name == "inspect_workspace":
            self.workspace = True
        if name == "review_media_batch":
            self.batch = True
            self.workspace = False
        if self.workspace:
            schemas = build(CatalogProfile(workspace=True))
        elif self.batch:
            # a plan and its samples need the room other tools would take
            schemas = build(CatalogProfile(batch=True))

        if name == "search_indexer" and not self.plan_only:
            schemas = build(CatalogProfile(domain="search"))

        if name == "discover_actions":
            domain = data["domain"]
            self.workspace = domain == "workspace"
            self.batch = False
            if (
                self.preparing_download
                and self.plan_only
                and not self.authoring
                and (
                    domain in {"search", "downloads"}
                    or (subscription_answer and domain == "questions")
                )
            ):
                schemas = build(self._plan_profile())
                data["tools"] = sorted(
                    entry["function"]["name"]
                    for entry in schemas
                    if not answer_only
                    or allow_answer_tool(entry["function"]["name"], subscription_answer)
                )
            else:
                schemas = build(CatalogProfile(domain=domain))
                if answer_only:
                    data["tools"] = [
                        tool_name
                        for tool_name in data["tools"]
                        if allow_answer_tool(tool_name, subscription_answer)
                    ]

        return schemas

    def trim_for_canvas(self, schemas: list[Any], *, has_canvas: bool) -> list[Any]:
        """Drop redundant discovery tools during canvas-backed workflow authoring.

        Args:
            schemas: Active tool schemas at the end of a model round.
            has_canvas: Whether the turn is bound to an editable designer snapshot.

        Returns:
            Filtered tool schemas with canvas-redundant reads removed.
        """
        if not (self.authoring and has_canvas):
            return schemas
        dropped = (
            CANVAS_TRIM_TOOLS | SELF_CONTAINED_TRIM_TOOLS
            if self.self_contained
            else CANVAS_TRIM_TOOLS
        )
        return [
            schema for schema in schemas if schema["function"]["name"] not in dropped
        ]
