"""Define MCP access tokens, grants, and creation requests."""

from enum import StrEnum, auto
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
    """Limit an MCP credential to one operational tool catalog."""

    ALL = auto()
    MEDIA = auto()
    DOWNLOADS = auto()
    WORKFLOWS = auto()
    SYSTEM = auto()


class MCPUserInfo(UserInfo):
    """Carry a verified token grant alongside its owner's current identity."""

    mcp_scope: MCPTokenScope
    mcp_domain: MCPTokenDomain = MCPTokenDomain.ALL
    mcp_library_ids: list[int] | None = None


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
        enum_type=MCPTokenScope, max_length=32, default=MCPTokenScope.READ_ONLY
    )
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
        exclude = ("user", "user_id", "token_hash")
        computed = ("status",)


class MCPAccessTokenCreate(BaseModel):
    """Validate a named token with an explicit grant and bounded lifetime."""

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    name: str = Field(min_length=1, max_length=64)
    expires_in_days: Literal[30, 90, 365] = 90
    scope: MCPTokenScope = MCPTokenScope.READ_ONLY
    domain: MCPTokenDomain = MCPTokenDomain.ALL
    library_ids: list[Annotated[int, Field(strict=True, gt=0)]] | None = Field(
        default=None, min_length=1, max_length=128
    )

    @model_validator(mode="after")
    def validate_libraries(self) -> Self:
        """Bind an explicit library selection to the media domain.

        Returns:
            The validated grant.

        Raises:
            ValueError: If the requested domain or library list is invalid.
        """
        if self.library_ids is not None and (
            self.domain != MCPTokenDomain.MEDIA
            or len(set(self.library_ids)) != len(self.library_ids)
        ):
            raise ValueError("Select distinct libraries for a media-domain token.")
        return self
