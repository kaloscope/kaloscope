from datetime import UTC, datetime
from enum import StrEnum, auto
from typing import Any, Self, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FutureDatetime,
    NonNegativeInt,
    PositiveInt,
    field_serializer,
    model_validator,
)
from sanic.request.form import File
from tortoise.fields import (
    CASCADE,
    SET_NULL,
    BackwardOneToOneRelation,
    BigIntField,
    BooleanField,
    CharEnumField,
    CharField,
    DatetimeField,
    FloatField,
    ForeignKeyField,
    ForeignKeyNullableRelation,
    ForeignKeyRelation,
    IntField,
    JSONField,
    OneToOneField,
    OneToOneRelation,
    ReverseRelation,
    TextField,
)

from app.core.dl.driver import DownloadState
from app.models.base import IDs, Pageable, RequestFilesMixin, TortoiseModel
from app.models.flow import FlowGraph
from app.models.media import MediaLib
from app.utils.datetime import duration
from app.utils.disk import format_bytes


# -------------------- Enumerations --------------------
class TransferMethod(StrEnum):
    """Transfer completed files by hardlink, symlink, move or copy."""

    HARDLINK = auto()
    SYMLINK = auto()
    MOVE = auto()
    COPY = auto()


class OfflineDownloadErrorKind(StrEnum):
    """Classify stored OpenList failures and uncertain submissions.

    `submit_unknown`: Submission outcome is unconfirmed; avoid duplicate submissions.
    `instance_auth`: OpenList instance authentication or authorization failed.
    `instance_rate_limit`: The instance rejected requests due to rate limits.
    `instance_transient`: The instance encountered a temporary service failure.
    `remote_task_missing`: A previously tracked remote task cannot be found.
    `remote_failed`: The remote offline download failed.
    `transfer_failed`: A remote storage transfer failed.
    `manifest_invalid`: The remote file manifest is invalid.
    `direct_link_unavailable`: A usable direct file URL could not be obtained.
    `local_path_invalid`: The local destination path is invalid.
    `local_file_conflict`: An existing local file conflicts with the download.
    `pull_failed`: Transferring remote files to local storage failed.
    `verify_failed`: Local files failed final verification.
    `cleanup_failed`: Remote cleanup failed after local transfer.

    These describe download jobs, not failures to query their status.
    """

    SUBMIT_UNKNOWN = auto()
    INSTANCE_AUTH = auto()
    INSTANCE_RATE_LIMIT = auto()
    INSTANCE_TRANSIENT = auto()
    REMOTE_TASK_MISSING = auto()
    REMOTE_FAILED = auto()
    TRANSFER_FAILED = auto()
    MANIFEST_INVALID = auto()
    DIRECT_LINK_UNAVAILABLE = auto()
    LOCAL_PATH_INVALID = auto()
    LOCAL_FILE_CONFLICT = auto()
    PULL_FAILED = auto()
    VERIFY_FAILED = auto()
    CLEANUP_FAILED = auto()


# -------------------- ORM Models --------------------
class Downloader(TortoiseModel):
    preset = CharField(max_length=16, null=True)
    config = TextField()
    name = CharField(max_length=64, unique=True)
    host = CharField(max_length=255, null=True)
    port = IntField(null=True)
    version = CharField(max_length=32, null=True)
    priority = IntField(unique=True)
    # relational fields
    plans: ReverseRelation["DownloadPlan"]
    tasks: ReverseRelation["DownloadTask"]

    class Meta:
        table = "downloader"
        ordering = ["priority"]

    class PydanticMeta:
        exclude = ("plans", "tasks")


class DownloadDir(TortoiseModel):
    path = CharField(max_length=4096, unique=True)
    last_used = DatetimeField()

    class Meta:
        table = "download_dir"
        ordering = ["-last_used"]


class DownloadPlan(TortoiseModel):
    # durable creation receipt; existing plans keep their original scheduling behavior
    creation_key = CharField(max_length=64, unique=True, null=True)
    creation_receipt = JSONField[dict[str, Any] | None](null=True)
    graph_id: int
    graph: ForeignKeyRelation[FlowGraph] = ForeignKeyField(
        "models.FlowGraph", related_name="plans", db_index=True
    )
    downloader_id: int
    downloader: ForeignKeyRelation[Downloader] = ForeignKeyField(
        "models.Downloader", related_name="plans", db_index=True
    )
    dir = CharField(max_length=4096)
    keyword = CharField(max_length=4096)
    filters = JSONField[dict[str, Any] | None](null=True)
    interval_num = IntField(default=1)
    interval_start = DatetimeField(null=True)
    interval_end = DatetimeField(null=True)
    batch_limit = IntField(default=10)
    total_limit = IntField(null=True)
    total_count = IntField(default=0)
    last_exec = DatetimeField(null=True)
    transfer_lib_id: int | None
    transfer_lib: ForeignKeyNullableRelation[MediaLib] = ForeignKeyField(
        "models.MediaLib",
        related_name="plans",
        db_index=True,
        null=True,
        on_delete=SET_NULL,
    )
    transfer_method = CharEnumField(max_length=16, enum_type=TransferMethod, null=True)
    sub_pattern = CharField(max_length=4096, null=True)
    sub_repl = CharField(max_length=4096, null=True)
    # relational fields
    histories: ReverseRelation["DownloadPlanHistory"]

    def inactive(self) -> bool:
        """Check whether the plan is currently inactive.

        Returns:
            `True` when scheduling limits prevent execution, otherwise `False`.
        """
        now = datetime.now(UTC)
        if self.interval_start is not None and now < self.interval_start:
            return True
        if self.interval_end is not None and now > self.interval_end:
            return True
        return self.total_limit is not None and self.total_count >= self.total_limit

    class Meta:
        table = "download_plan"
        ordering = ["-created_at"]

    class PydanticMeta:
        exclude = (
            "graph",
            "downloader",
            "transfer_lib",
            "histories",
            "creation_key",
            "creation_receipt",
        )
        computed = ("inactive",)


class DownloadPlanHistory(TortoiseModel):
    plan_id: int
    plan: ForeignKeyRelation[DownloadPlan] = ForeignKeyField(
        "models.DownloadPlan", related_name="histories", db_index=True
    )
    info_hash = CharField(max_length=40, null=True)
    info_hash_v2 = CharField(max_length=68, null=True)

    class Meta:
        table = "download_plan_history"
        indexes = (
            ("plan_id", "info_hash"),
            ("plan_id", "info_hash_v2"),
        )


class DownloadTask(TortoiseModel):
    downloader_id: int
    downloader: ForeignKeyRelation[Downloader] = ForeignKeyField(
        "models.Downloader", related_name="tasks", db_index=True
    )
    dir = CharField(max_length=4096)
    name = CharField(max_length=255)
    unique_id = CharField(max_length=255, db_index=True, null=True)
    info_hash = CharField(max_length=40, unique=True, null=True)
    info_hash_v2 = CharField(max_length=68, unique=True, null=True)
    magnet_link = TextField(null=True)
    state = CharEnumField(max_length=16, enum_type=DownloadState)
    raw_state = CharField(max_length=32, null=True)
    error_msg: str | None = TextField(null=True)
    up_speed = BigIntField(null=True)
    dl_speed = BigIntField(null=True)
    percentage = FloatField(null=True)
    total_size = BigIntField(null=True)
    completed_size = BigIntField(null=True)
    completed_at = DatetimeField(null=True)
    files = JSONField[list[str] | None](null=True)
    transfer_lib_id: int | None
    transfer_lib: ForeignKeyNullableRelation[MediaLib] = ForeignKeyField(
        "models.MediaLib",
        related_name="tasks",
        db_index=True,
        null=True,
        on_delete=SET_NULL,
    )
    transfer_method = CharEnumField(max_length=16, enum_type=TransferMethod, null=True)
    sub_pattern = CharField(max_length=4096, null=True)
    sub_repl = CharField(max_length=4096, null=True)
    transfer_targets = JSONField[dict[str, str] | None](null=True)
    transfer_pending = BooleanField(default=False, db_default=False)
    offline_job: BackwardOneToOneRelation["OfflineDownloadJob"]

    def ratio(self) -> str:
        """Calculate the ratio of completed size to total size.

        Returns:
            Formatted byte ratio and optional percentage.
        """
        size = ""
        if self.completed_size is None or self.total_size is None:
            return size
        size = f"{format_bytes(self.completed_size)} / {format_bytes(self.total_size)}"
        if self.percentage is None:
            return size
        percentage = f"{round(self.percentage)}%"
        return f"{size} ({percentage})"

    def estimate(self) -> str:
        """Estimate the download speed and time remaining.

        Returns:
            Formatted speed and optional remaining duration.
        """
        speed = ""
        if self.dl_speed is None:
            return speed
        speed = format_bytes(self.dl_speed) + "/s"
        if not self.dl_speed or not self.completed_size or not self.total_size:
            return speed
        eta = duration(
            (self.total_size - self.completed_size) / self.dl_speed, unit="seconds"
        )
        return f"{speed} ({eta})"

    class Meta:
        table = "download_task"
        ordering = ["-created_at"]
        indexes = (("state", "created_at"),)

    class PydanticMeta:
        exclude = (
            "downloader",
            "transfer_lib",
            "offline_job",
            "transfer_targets",
            "transfer_pending",
        )
        computed = ("ratio", "estimate")


class OfflineDownloadJob(TortoiseModel):
    download_id: int
    download: OneToOneRelation[DownloadTask] = OneToOneField(
        "models.DownloadTask",
        related_name="offline_job",
        on_delete=CASCADE,
    )
    job_uuid = CharField(max_length=32, unique=True)
    source_fingerprint = CharField(max_length=64)
    remote_dir = CharField(max_length=4096, unique=True)
    manifest = JSONField[list[dict[str, Any]] | None](null=True)
    manifest_fingerprint = CharField(max_length=64, null=True)
    manifest_changed_at = DatetimeField(null=True)
    next_poll_at = DatetimeField(null=True, db_index=True)
    completion_due_at = DatetimeField(null=True)
    delete_due_at = DatetimeField(null=True, db_index=True)
    delete_local = BooleanField(default=False, db_default=False)
    unchanged_count = IntField(default=0)
    retry_count = IntField(default=0)
    # preserve the nullable value annotation missing from `CharEnumField` overloads
    last_error_kind: OfflineDownloadErrorKind | None = cast(
        OfflineDownloadErrorKind | None,
        CharEnumField(max_length=32, enum_type=OfflineDownloadErrorKind, null=True),
    )

    class Meta:
        table = "offline_download_job"

    class PydanticMeta:
        exclude = ("download",)


# -------------------- Pydantic Models --------------------
class DownloaderUpsert(BaseModel):
    id: PositiveInt | None = None
    preset: str | None = Field(max_length=16, default=None)
    config: str


class DownloadQuery(Pageable):
    downloader_id: NonNegativeInt | None = None
    name: str | None = None
    state: DownloadState | None = None
    states: list[DownloadState] | None = None


class DownloadAdd(BaseModel, RequestFilesMixin):
    downloader_id: PositiveInt
    dir: str = Field(min_length=1, max_length=4096)
    link: str | None = Field(pattern=r"^[^\n]*$", default=None)
    torrent: File | None = None
    pause: bool = False
    transfer_lib_id: PositiveInt | None = None
    transfer_method: TransferMethod | None = None
    sub_pattern: str | None = Field(max_length=4096, default=None)
    sub_repl: str | None = Field(max_length=4096, default=None)

    @model_validator(mode="after")
    def check_link_or_torrent(self) -> Self:
        """Require at least one download source.

        Returns:
            Validated submission model.

        Raises:
            ValueError: If both the link and torrent are absent.
        """
        link = self.link.strip() if self.link else None
        if not link and not self.torrent:
            raise ValueError("either link or torrent must be provided")
        return self

    @field_serializer("torrent")
    def serialize_torrent(self, torrent: File | None) -> tuple | None:
        """Convert a torrent upload to an HTTP multipart file tuple.

        Args:
            torrent: Uploaded torrent, or `None` when absent.

        Returns:
            File name, contents and media type, or `None` when absent.
        """
        if torrent:
            # convert to `HTTPX` multipart file encoding
            # https://www.python-httpx.org/advanced/clients/#multipart-file-encoding
            return (torrent.name, torrent.body, torrent.type)
        return None


class DownloadDel(IDs):
    local: bool = False


class DownloadStats(BaseModel):
    downloading: int = 0
    completed: int = 0
    up_speed: int = 0
    dl_speed: int = 0

    @field_serializer("downloading", "completed")
    def serialize_count(self, count: int) -> str:
        return f" ({count})" if count else ""

    @field_serializer("up_speed", "dl_speed")
    def serialize_speed(self, speed: int) -> str:
        return format_bytes(speed)


class DownloadPlanQuery(Pageable):
    graph_id: NonNegativeInt | None = None
    keyword: str | None = None


class DownloadPlanUpsert(BaseModel):
    id: PositiveInt | None = None
    graph_id: PositiveInt
    downloader_id: PositiveInt
    dir: str = Field(min_length=1, max_length=4096)
    keyword: str = Field(min_length=0, max_length=4096)
    filters: dict[str, Any] | None = None
    interval_num: PositiveInt
    interval_start: datetime | None = None
    interval_end: FutureDatetime | None = None
    batch_limit: PositiveInt
    total_limit: PositiveInt | None = None
    transfer_lib_id: PositiveInt | None = None
    transfer_method: TransferMethod | None = None
    sub_pattern: str | None = Field(max_length=4096, default=None)
    sub_repl: str | None = Field(max_length=4096, default=None)

    @model_validator(mode="after")
    def validate_interval_fields(self) -> Self:
        """Require the plan start time to precede its end time.

        Returns:
            Validated plan settings.

        Raises:
            ValueError: If the configured time range is empty or reversed.
        """
        if (
            self.interval_start is not None
            and self.interval_end is not None
            and (self.interval_start.tzinfo is None)
            == (self.interval_end.tzinfo is None)
            and self.interval_start >= self.interval_end
        ):
            raise ValueError("interval_start must be before interval_end")
        return self


class DownloadPlanSettings(DownloadPlanUpsert):
    """Describe a new plan using the existing editor's fields.

    Expiry is checked when creating, after looking up an idempotent replay, so an
    old request can still recover a previously created plan.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: None = None
    interval_end: datetime | None = None


class DownloadPlanCheck(BaseModel):
    """Check explicit plan settings and an optional search-result fixture."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    plan: DownloadPlanSettings
    sample: dict[str, Any] | None = Field(
        default=None,
        description="Optional page-1 search result; page_size=batch_limit.",
    )

    @model_validator(mode="after")
    def bound_input(self) -> Self:
        """Bound settings and fixtures before processing them.

        Returns:
            The validated request.

        Raises:
            ValueError: If the input exceeds 32 KiB.
        """
        from app.utils.json import dumps

        if len(dumps(self.model_dump(mode="json"))) > 32768:
            raise ValueError("Plan input exceeds 32 KiB")
        return self


class DownloadPlanCreate(BaseModel):
    """Create once using a stable client request ID; active plans run promptly."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    request_id: UUID
    plan: DownloadPlanSettings
    confirmed: bool = Field(
        default=False,
        description="True only after the user approves these final settings.",
    )


class IndexerSearch(BaseModel):
    """Search one selected published indexer without submitting downloads."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    workflow_id: PositiveInt
    keyword: str = Field(max_length=4096)
    filters: dict[str, Any] | None = None
    page_num: PositiveInt = 1
    page_size: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def bound_input(self) -> Self:
        """Bound the explicit search inputs.

        Returns:
            The validated search request.

        Raises:
            ValueError: If the request exceeds 8 KiB.
        """
        from app.utils.json import dumps

        if len(dumps(self.model_dump(mode="json"))) > 8192:
            raise ValueError("Search input exceeds 8 KiB")
        return self
