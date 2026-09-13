from functools import cached_property
from pathlib import PurePosixPath
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.dl.endpoint import Endpoint
from app.utils.json import JSONType

type Method = Literal[
    "version",
    "login",
    "add_link",
    "add_torrent",
    "list",
    "details",
    "pause",
    "start",
    "delete",
]


class CSRF(BaseModel):
    header: str


class Authentication(BaseModel):
    secret: str | None = None
    username: str | None = None
    password: str | None = None


class LocalFiles(BaseModel):
    """Define rules for expanding downloaded files into absolute paths."""

    path: str
    exclude: list[str] = Field(default_factory=list)
    when: bool | str = True


class APIResponse(BaseModel):
    each: str | None = None
    mappings: dict[str, str] | None = None
    expected: dict[str, str] | str | None = None
    unexpected: dict[str, str] | str | None = None
    check: bool | str | None = None
    transform: Any = None
    local_files: LocalFiles | None = None


class Pagination(BaseModel):
    """Define cursor pagination for an RPC response."""

    next: str
    parameter: str = "page_token"
    max_pages: int = Field(default=100, ge=1, le=1000)


class API(BaseModel):
    get: str | None = None
    post: str | None = None
    put: str | None = None
    patch: str | None = None
    delete: str | None = None
    headers: dict[str, str] | None = None
    params: dict[str, Any] | None = None
    body: dict[str, Any] | str | None = None
    form: dict[str, Any] | None = None
    json_: JSONType = Field(alias="json", default=None)
    response: APIResponse | None = None
    pagination: Pagination | None = None
    steps: list["API"] | None = Field(default=None, min_length=1, max_length=32)
    save_as: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    when: bool | str = True
    require: bool | str = True
    foreach: str | None = None

    @model_validator(mode="after")
    def validate_request(self):
        verbs = [self.get, self.post, self.put, self.patch, self.delete]
        if sum(value is not None for value in verbs) > 1:
            raise ValueError("An RPC request must specify only one HTTP method")
        if self.steps and any(
            value is not None
            for value in (*verbs, self.body, self.form, self.json_, self.params)
        ):
            raise ValueError("RPC steps cannot also contain an HTTP request")
        if self.foreach is not None and not self.steps:
            raise ValueError("RPC foreach requires steps")
        if self.pagination and (
            self.get is None or not self.response or not self.response.each
        ):
            raise ValueError("RPC pagination requires GET and response.each")
        return self

    @property
    def http_method(self) -> tuple[str, str]:
        """Get the configured HTTP method and path, defaulting to POST."""
        for method in ("get", "post", "put", "patch", "delete"):
            if (path := getattr(self, method)) is not None:
                return method.upper(), path
        return "POST", ""

    @property
    def extended(self) -> bool:
        """Check whether this API needs extended request or response processing."""
        return bool(
            self.model_fields_set
            & {
                "steps",
                "pagination",
                "when",
                "require",
                "save_as",
                "foreach",
            }
            or self.response
            and self.response.model_fields_set & {"transform", "check", "local_files"}
        )


class Session(BaseModel):
    """Define session initialization and refresh settings."""

    request: API
    ttl: int = Field(default=600, ge=1, le=86400)
    refresh_statuses: list[int] = Field(default_factory=lambda: [401, 403])

    @field_validator("refresh_statuses")
    @classmethod
    def validate_statuses(cls, value: list[int]) -> list[int]:
        if any(status not in (401, 403) for status in value):
            raise ValueError("Only authentication rejections can refresh a session")
        return value


class RpcConfig(Endpoint):
    driver: Literal["rpc"] = "rpc"
    name: str
    csrf: CSRF | None = None
    auth: Authentication | None = None
    unix_socket: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    session: Session | None = None
    timeout: float = Field(default=30, gt=0, le=300)
    methods: dict[Method, API]
    response: APIResponse | None = None

    @cached_property
    def extended(self) -> bool:
        """Check whether this downloader needs the extended RPC client."""
        return bool(
            self.unix_socket
            or self.session
            or self.response
            and self.response.check is not None
            or any(api.extended for api in self.methods.values())
        )

    @field_validator("unix_socket")
    @classmethod
    def validate_socket(cls, value: str | None) -> str | None:
        if value is not None and not PurePosixPath(value).is_absolute():
            raise ValueError("RPC unix_socket must be absolute")
        return value

    @cached_property
    def csrf_header(self) -> str | None:
        if not self.csrf:
            return None
        return self.csrf.header

    @cached_property
    def explicit_login(self) -> bool:
        if not (self.auth and self.auth.username and self.auth.password):
            return False
        return "login" in self.methods

    @cached_property
    def basic_auth(self) -> httpx.BasicAuth | None:
        if not (self.auth and self.auth.username and self.auth.password):
            return None
        return httpx.BasicAuth(username=self.auth.username, password=self.auth.password)
