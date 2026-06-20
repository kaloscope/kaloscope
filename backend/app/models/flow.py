from datetime import datetime
from enum import StrEnum, auto
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, Field, FutureDatetime, PositiveInt, model_validator
from sanic.request.form import File
from tortoise.fields import (
    SET_NULL,
    BooleanField,
    CharEnumField,
    CharField,
    DatetimeField,
    ForeignKeyField,
    ForeignKeyNullableRelation,
    ForeignKeyRelation,
    IntField,
    JSONField,
    ReverseRelation,
    TextField,
)

from app.models.base import Pageable, RequestFilesMixin, TortoiseModel


# -------------------- Enumerations --------------------
class GraphCategory(StrEnum):
    INDEXER = auto()
    INGEST = auto()
    SCHEDULE = auto()


class GraphState(StrEnum):
    DRAFT = auto()
    MODIFIED = auto()
    PUBLISHED = auto()


class JobState(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    PAUSED = auto()


class JobTrigger(StrEnum):
    DATE = "date"
    CRON = "cron"
    INTERVAL = "interval"


class IntervalUnit(StrEnum):
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"
    WEEKS = "weeks"


# -------------------- ORM Models --------------------
class FlowRepository(TortoiseModel):
    repo_name = CharField(max_length=255, unique=True)
    repo_url = CharField(max_length=255)
    repo_description = CharField(max_length=512, null=True)
    owner_name = CharField(max_length=64, null=True)
    owner_url = CharField(max_length=255, null=True)
    owner_avatar = CharField(max_length=255, null=True)
    # relational fields
    templates: ReverseRelation["FlowTemplate"]

    class Meta:
        table = "flow_repository"
        ordering = ["created_at"]

    class PydanticMeta:
        exclude = ("templates",)


class FlowTemplate(TortoiseModel):
    repo_id: str
    repo: ForeignKeyRelation[FlowRepository] = ForeignKeyField(
        "models.FlowRepository",
        related_name="templates",
        to_field="repo_name",
        db_index=True,
    )
    path = CharField(max_length=255)
    name = CharField(max_length=64)
    icon = CharField(max_length=255, null=True)
    description = CharField(max_length=255, null=True)
    category = CharEnumField(max_length=16, enum_type=GraphCategory)
    revision = IntField()
    definition = JSONField[dict[str, Any]]()
    newest = BooleanField(default=True)
    # relational fields
    graphs: ReverseRelation["FlowGraph"]

    class Meta:
        table = "flow_template"
        ordering = ["-revision"]
        unique_together = (("repo", "path", "revision"),)


class FlowGraph(TortoiseModel):
    tmpl_id: int | None
    tmpl: ForeignKeyNullableRelation[FlowTemplate] = ForeignKeyField(
        "models.FlowTemplate",
        related_name="graphs",
        db_index=True,
        null=True,
        on_delete=SET_NULL,
    )
    name = CharField(max_length=64, unique=True)
    icon = CharField(max_length=255, null=True)
    description = CharField(max_length=512, null=True)
    category = CharEnumField(max_length=16, enum_type=GraphCategory)
    revision = IntField(null=True)
    state = CharEnumField(max_length=16, enum_type=GraphState)
    draft = JSONField[dict[str, Any] | None](null=True)
    definition = JSONField[dict[str, Any] | None](null=True)
    editable = BooleanField(default=True)
    # relational fields
    logs: ReverseRelation["FlowLog"]
    jobs: ReverseRelation["FlowJob"]
    triggers: ReverseRelation["FlowTrigger"]
    variables: ReverseRelation["FlowVariable"]
    instances: ReverseRelation["FlowInstance"]
    favorites: ReverseRelation
    plans: ReverseRelation

    def success_rate(self) -> float | None:
        """Calculate the success rate of the graph executions."""
        if hasattr(self, "logs") and self.logs:
            success = sum(1 for log in self.logs if log.ended_at)
            return success / len(self.logs)
        return None

    def average_time(self) -> float | None:
        """Calculate the average time taken for executions."""
        if hasattr(self, "logs") and self.logs:
            logs = [log for log in self.logs if log.ended_at]
            if not logs:
                return None
            # calculate the average execution time (in milliseconds)
            total = sum((log.ended_at - log.started_at).total_seconds() for log in logs)
            return total * 1000 / len(logs)
        return None

    def last_exec(self) -> datetime | None:
        """Extract the last execution time from the logs."""
        if hasattr(self, "logs") and self.logs:
            return self.logs[0].started_at
        return None

    def node_types(self) -> list[str]:
        """Extract the node types from the graph definition."""
        nodes = self.definition.get("nodes") if self.definition else None
        if not isinstance(nodes, list):
            return []
        return [
            node_type
            for node in nodes
            if isinstance((node_type := node["data"]["$schema"]), str)
        ]

    def only_preview(self) -> bool:
        """Check if the graph is only for preview."""
        nodes = (self.definition or {}).get("nodes")
        if not isinstance(nodes, list):
            return False
        for node in nodes:
            if node.get("data", {}).get("$schema") == "search_start":
                config = node["data"].get("config", "")
                if isinstance(config, str) and config.strip():
                    try:
                        cfg = yaml.load(config, Loader=yaml.SafeLoader)
                        if isinstance(cfg, dict):
                            return bool(cfg.get("only_preview", False))
                    except yaml.YAMLError:
                        pass
                break
        return False

    class Meta:
        table = "flow_graph"
        ordering = ["-created_at"]

    class PydanticMeta:
        exclude = ("jobs", "triggers", "variables", "instances", "favorites", "plans")
        computed = (
            "success_rate",
            "average_time",
            "last_exec",
            "node_types",
            "only_preview",
        )


class FlowLog(TortoiseModel):
    graph_id: int
    graph: ForeignKeyRelation[FlowGraph] = ForeignKeyField(
        "models.FlowGraph", related_name="logs", db_index=True
    )
    bootparams = JSONField[dict[str, Any] | None](null=True)
    retval = JSONField(null=True)
    started_at = DatetimeField()
    ended_at = DatetimeField(null=True)
    node_id = CharField(max_length=64, null=True)
    node_type = CharField(max_length=64, null=True)
    node_data = JSONField[dict[str, Any] | None](null=True)
    input_id = CharField(max_length=64, null=True)
    exc_info = TextField(null=True)

    class Meta:
        table = "flow_log"
        ordering = ["-started_at", "created_at"]

    class PydanticMeta:
        exclude = ("graph", "bootparams", "retval", "node_data", "exc_info")


class FlowJob(TortoiseModel):
    graph_id: int
    graph: ForeignKeyRelation[FlowGraph] = ForeignKeyField(
        "models.FlowGraph", related_name="jobs", db_index=True
    )
    bootparams = JSONField[dict[str, Any] | None](null=True)
    repeatable = BooleanField(default=False)
    recoverable = BooleanField(default=True)
    state = CharEnumField(max_length=16, enum_type=JobState)
    trigger = CharEnumField(max_length=16, enum_type=JobTrigger)
    run_date = DatetimeField(null=True)
    cron_expr = CharField(max_length=255, null=True)
    interval_num = IntField(null=True)
    interval_unit = CharEnumField(max_length=16, enum_type=IntervalUnit, null=True)
    interval_start = DatetimeField(null=True)
    interval_end = DatetimeField(null=True)

    class Meta:
        table = "flow_job"
        ordering = ["-created_at"]

    class PydanticMeta:
        exclude = ("graph",)


class FlowTrigger(TortoiseModel):
    category = CharEnumField(max_length=16, enum_type=GraphCategory)
    rel_id = IntField()
    priority = IntField()
    asynchronous = BooleanField(default=False)
    graph_id: int
    graph: ForeignKeyRelation[FlowGraph] = ForeignKeyField(
        "models.FlowGraph", related_name="triggers", db_index=True
    )

    class Meta:
        table = "flow_trigger"
        ordering = ["priority"]
        unique_together = (("rel_id", "graph"), ("rel_id", "priority"))


class FlowVariable(TortoiseModel):
    graph_id: int
    graph: ForeignKeyRelation[FlowGraph] = ForeignKeyField(
        "models.FlowGraph", related_name="variables", db_index=True
    )
    key = CharField(max_length=64)
    value = JSONField[dict[str, Any]]()
    expires = IntField(null=True)

    class Meta:
        table = "flow_variable"
        unique_together = (("graph", "key"),)


class FlowInstance(TortoiseModel):
    graph_id: int
    graph: ForeignKeyRelation[FlowGraph] = ForeignKeyField(
        "models.FlowGraph", related_name="instances", db_index=True
    )
    definition = JSONField[dict[str, Any]]()
    bootparams = JSONField[dict[str, Any] | None](null=True)
    context = JSONField[dict[str, Any]]()
    repeatable = BooleanField(default=False)
    asynchronous = BooleanField(default=False)
    prev_id = IntField(null=True)
    # relational fields
    footprints: ReverseRelation["FlowFootprint"]

    class Meta:
        table = "flow_instance"


class FlowFootprint(TortoiseModel):
    inst_id: int
    inst: ForeignKeyRelation[FlowInstance] = ForeignKeyField(
        "models.FlowInstance", related_name="footprints", db_index=True
    )
    node_id = CharField(max_length=64)
    node_type = CharField(max_length=64)
    node_data = JSONField[dict[str, Any]]()
    loop_id = CharField(max_length=64, null=True)
    started_at = DatetimeField()
    ended_at = DatetimeField()

    class Meta:
        table = "flow_footprint"


# -------------------- Pydantic Models --------------------
class RepositoryAdd(BaseModel):
    repo: str = Field(min_length=1, max_length=255)


class TmplQuery(Pageable):
    repo: str | None = Field(max_length=255, default=None)
    name: str | None = None
    newest: bool | None = None
    category: GraphCategory | None = None


class GraphQuery(Pageable):
    name: str | None = None
    state: GraphState | None = None
    states: list[GraphState] | None = None
    category: GraphCategory | None = None


class GraphBasics(BaseModel, RequestFilesMixin):
    id: PositiveInt | None = None
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(max_length=512, default=None)
    icon: str | None = None
    image: File | None = None
    category: GraphCategory | None = None


class GraphImport(BaseModel, RequestFilesMixin):
    zip: File


class GraphRef(BaseModel):
    graph_id: PositiveInt
    asynchronous: bool = False


class JobQuery(Pageable):
    name: str | None = None
    state: JobState | None = None
    trigger: JobTrigger | None = None


class JobUpsert(BaseModel):
    id: PositiveInt | None = None
    graph_id: PositiveInt
    bootparams: dict[str, Any] | None = None
    trigger: JobTrigger
    run_date: FutureDatetime | None = None
    cron_expr: str | None = Field(max_length=255, default=None)
    interval_num: PositiveInt | None = None
    interval_unit: IntervalUnit | None = None
    interval_start: datetime | None = None
    interval_end: FutureDatetime | None = None

    @model_validator(mode="after")
    def validate_trigger_fields(self) -> Self:
        if self.trigger == JobTrigger.DATE:
            if self.run_date is None:
                raise ValueError("run_date required")
        elif self.trigger == JobTrigger.CRON:
            if self.cron_expr is None:
                raise ValueError("cron_expr required")
            if len(self.cron_expr.split()) != 5:
                raise ValueError("cron_expr must have 5 fields")
        elif self.trigger == JobTrigger.INTERVAL:
            if self.interval_num is None or self.interval_unit is None:
                raise ValueError("interval_num and interval_unit required")
            if (
                self.interval_start is not None
                and self.interval_end is not None
                and self.interval_start >= self.interval_end
            ):
                raise ValueError("interval_start must be before interval_end")
        return self


class IndexerResource(BaseModel):
    rsrc_id: PositiveInt | Annotated[str, Field(min_length=1)]
    rsrc: dict | None = None
    url: str | None = Field(max_length=255, default=None)
