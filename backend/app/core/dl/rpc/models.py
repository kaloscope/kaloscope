from functools import cached_property
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

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


class APIResponse(BaseModel):
    each: str | None = None
    mappings: dict[str, str] | None = None
    expected: dict[str, str] | str | None = None
    unexpected: dict[str, str] | str | None = None


class API(BaseModel):
    get: str | None = None
    post: str | None = None
    headers: dict[str, str] | None = None
    body: dict[str, Any] | str | None = None
    form: dict[str, Any] | None = None
    json_: JSONType = Field(alias="json", default=None)
    response: APIResponse | None = None


class RpcConfig(Endpoint):
    driver: Literal["rpc"] = "rpc"
    name: str
    csrf: CSRF | None = None
    auth: Authentication | None = None
    methods: dict[Method, API]
    response: APIResponse | None = None

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
