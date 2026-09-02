import hashlib
from enum import StrEnum, auto
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.core.constants import ENCODING
from app.core.dl.endpoint import Endpoint


class OpenListErrorKind(StrEnum):
    API = auto()
    AUTH = auto()
    TRANSIENT = auto()
    RATE_LIMIT = auto()
    INVALID_RESPONSE = auto()


class OpenListResponse[ResponseData](BaseModel):
    model_config = ConfigDict(strict=True)
    code: int
    message: str
    data: ResponseData


class OpenListTools(RootModel[list[Annotated[str, Field(min_length=1)]]]):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True)


class OpenListSettings(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True)
    version: str = Field(min_length=1)


class OpenListDirectoryAccess(BaseModel):
    model_config = ConfigDict(strict=True)
    write: bool


class RemoteEntry(BaseModel):
    model_config = ConfigDict(strict=True)
    name: str = Field(min_length=1)
    size: int = Field(ge=0)
    is_dir: bool
    hash_info: dict[str, str] = Field(default_factory=dict)

    @field_validator("hash_info", mode="before")
    @classmethod
    def normalize_hash_info(cls, value: object) -> object:
        return {} if value is None else value


class RemoteEntryPage(BaseModel):
    model_config = ConfigDict(strict=True)
    content: list[RemoteEntry]
    total: int = Field(ge=0)

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: object) -> object:
        return [] if value is None else value


class RemoteLink(BaseModel):
    model_config = ConfigDict(strict=True)
    url: str = Field(min_length=1, repr=False)
    headers: dict[str, list[str]] = Field(
        default_factory=dict, validation_alias="header", repr=False
    )

    @field_validator("headers", mode="before")
    @classmethod
    def normalize_headers(cls, value: object) -> object:
        return {} if value is None else value


class RemoteTaskState(StrEnum):
    PENDING = auto()
    RUNNING = auto()
    RETRYING = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELED = auto()


# Numeric values are defined in:
# https://github.com/OpenListTeam/tache/blob/v0.2.2/state.go
_TACHE_TASK_STATES = {
    0: RemoteTaskState.PENDING,  # tache.StatePending
    1: RemoteTaskState.RUNNING,  # tache.StateRunning
    2: RemoteTaskState.SUCCEEDED,  # tache.StateSucceeded
    3: RemoteTaskState.RUNNING,  # tache.StateCanceling
    4: RemoteTaskState.CANCELED,  # tache.StateCanceled
    5: RemoteTaskState.RETRYING,  # tache.StateErrored
    6: RemoteTaskState.RETRYING,  # tache.StateFailing
    7: RemoteTaskState.FAILED,  # tache.StateFailed
    8: RemoteTaskState.RETRYING,  # tache.StateWaitingRetry
    9: RemoteTaskState.RETRYING,  # tache.StateBeforeRetry
}


class RemoteTask(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True)
    id: str = Field(min_length=1)
    name: str
    state: RemoteTaskState
    progress: float = Field(ge=0, le=100)
    total_bytes: int = Field(ge=0)
    error: str = ""

    @field_validator("state", mode="before")
    @classmethod
    def map_tache_state(cls, value: object) -> RemoteTaskState:
        if isinstance(value, RemoteTaskState):
            return value
        if type(value) is not int or value not in _TACHE_TASK_STATES:
            raise ValueError("Unknown OpenList task state")
        return _TACHE_TASK_STATES[value]

    def matches_submission(
        self, remote_directory: str, source_fingerprint: str
    ) -> bool:
        # `DownloadTask.GetName()` includes the source and exact destination:
        # https://github.com/OpenListTeam/OpenList/blob/a4ae2acb65792f59b2ab0f24a0f68430e1f1fd81/internal/offline_download/tool/download.go
        prefix = "download "
        suffix = f" to ({remote_directory})"
        if not self.name.startswith(prefix) or not self.name.endswith(suffix):
            return False
        source = self.name[len(prefix) : -len(suffix)]
        fingerprint = hashlib.sha256(source.encode(ENCODING)).hexdigest()
        return fingerprint == source_fingerprint


class RemoteTasks(RootModel[list[RemoteTask]]):
    model_config = ConfigDict(strict=True)


class OpenListTaskReference(BaseModel):
    model_config = ConfigDict(strict=True, str_strip_whitespace=True)
    id: str = Field(min_length=1)


class OpenListSubmitResult(BaseModel):
    model_config = ConfigDict(strict=True)
    tasks: list[OpenListTaskReference]


class RemoteCleanupPolicy(StrEnum):
    KEEP = auto()
    DELETE_ON_SUCCESS = auto()


class OpenListAuth(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    token: SecretStr = Field(min_length=1)


class OpenListConfig(Endpoint):
    driver: Literal["openlist"] = "openlist"
    name: str | None = None
    path: str = "/api"
    auth: OpenListAuth
    tool: str = Field(min_length=1)
    remote_root: str = "/Kaloscope"
    remote_cleanup: RemoteCleanupPolicy = RemoteCleanupPolicy.KEEP
    poll_interval: int = Field(default=10, ge=5)
    poll_max_interval: int = Field(default=60, ge=5, validate_default=True)
    pull_concurrency: int = Field(default=2, ge=1)

    @field_validator("path")
    @classmethod
    def validate_api_path(cls, value: str) -> str:
        if not value.endswith("/api"):
            raise ValueError("OpenList path must end with /api")
        return value

    @field_validator("remote_root")
    @classmethod
    def normalize_remote_root(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or "\0" in value:
            raise ValueError("OpenList remote root must be an absolute POSIX path")
        return path.as_posix()

    @field_validator("poll_max_interval")
    @classmethod
    def validate_poll_max_interval(cls, value: int, info: ValidationInfo) -> int:
        if value < info.data.get("poll_interval", value):
            raise ValueError("Maximum poll interval cannot be smaller than the base")
        return value

    @model_validator(mode="after")
    def apply_name_default(self) -> Self:
        if not self.name:
            self.name = f"OpenList · {self.tool}"
        return self
