"""Define MCP access tokens, grants, and creation requests."""

from enum import StrEnum, auto
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from tortoise import timezone
from tortoise.fields import (
    CharEnumField,
    CharField,
    DatetimeField,
    ForeignKeyField,
    ForeignKeyRelation,
    JSONField,
)

from app.models.base import TortoiseModel
from app.models.user import User, UserInfo


class MCPTokenScope(StrEnum):
    """Identify explicitly granted MCP capabilities."""

    READ_ONLY = auto()
    READ_WRITE = auto()


class MCPTokenDomain(StrEnum):
    """Represent the domain column defined by the released `0011` migration."""

    ALL = auto()
    MEDIA = auto()
    DOWNLOADS = auto()
    WORKFLOWS = auto()
    SYSTEM = auto()


class MCPUserInfo(UserInfo):
    """Carry a verified token grant alongside its owner's current identity."""

    mcp_scope: MCPTokenScope


class MCPAccessToken(TortoiseModel):
    """Store a user-owned MCP credential digest and its fixed lifetime."""

    user_id: int
    user: ForeignKeyRelation[User] = ForeignKeyField(
        "models.User", related_name=False, db_index=True
    )
    name = CharField(max_length=64)
    token_hash = CharField(max_length=64, unique=True)
    prefix = CharField(max_length=17)
    scope = CharEnumField(
        enum_type=MCPTokenScope, max_length=32, default=MCPTokenScope.READ_WRITE
    )
    # retain the columns from `0011` solely to describe the existing database schema
    domain = CharEnumField(
        enum_type=MCPTokenDomain,
        max_length=16,
        default=MCPTokenDomain.ALL,
        db_default=MCPTokenDomain.ALL,
    )
    library_ids = JSONField[list[int] | None](null=True)
    expires_at = DatetimeField()
    last_used_at = DatetimeField(null=True)

    def status(self) -> str:
        """Describe the credential's current usability without exposing its digest.

        Returns:
            `expired` once the fixed lifetime ends, otherwise `active`.
        """
        return "expired" if self.expires_at <= timezone.now() else "active"

    class Meta:
        table = "mcp_access_token"
        ordering = ["-created_at", "-id"]

    class PydanticMeta:
        exclude = ("user", "user_id", "token_hash", "domain", "library_ids")
        computed = ("status",)


class MCPAccessTokenCreate(BaseModel):
    """Validate a named token with an explicit grant and bounded lifetime."""

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    name: str = Field(min_length=1, max_length=64)
    expires_in_days: Literal[30, 90, 365] = 90
    scope: MCPTokenScope = MCPTokenScope.READ_WRITE
