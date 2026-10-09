from datetime import datetime
from enum import StrEnum, auto
from typing import Annotated, Any, Self

from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt, model_validator
from sanic.request.form import File
from tortoise.fields import (
    CharEnumField,
    CharField,
    ForeignKeyField,
    ForeignKeyRelation,
    IntField,
    JSONField,
    ReverseRelation,
)

from app.models.base import KVPair, Pageable, RequestFilesMixin, TortoiseModel
from app.models.flow import FlowGraph


# -------------------- Enumerations --------------------
class UserRole(StrEnum):
    USER = auto()
    ADMIN = auto()


class HistoryType(StrEnum):
    SEARCH = auto()
    VIDEO = auto()
    TEXT = auto()
    IMAGE = auto()


class PermType(StrEnum):
    INDEXER = auto()
    MEDIA_LIB = auto()


# -------------------- ORM Models --------------------
class User(TortoiseModel):
    username = CharField(max_length=64, unique=True)
    password = CharField(max_length=64)
    avatar = CharField(max_length=255, null=True)
    role = CharEnumField(max_length=16, enum_type=UserRole)
    preferences = JSONField[dict[str, Any] | None](null=True)
    # relational fields
    favorites: ReverseRelation["UserFavorite"]
    histories: ReverseRelation["UserHistory"]
    permissions: ReverseRelation["UserPermission"]
    notifications: ReverseRelation

    class Meta:
        table = "user"
        ordering = ["role", "-created_at"]

    class PydanticMeta:
        exclude = ("password", "favorites", "histories", "permissions", "notifications")


class UserSession(TortoiseModel):
    session_id = CharField(max_length=32)
    user_info = JSONField["UserInfo"]()

    class Meta:
        table = "user_session"


class UserFavorite(TortoiseModel):
    user_id: int
    user: ForeignKeyRelation[User] = ForeignKeyField(
        "models.User", related_name="favorites", db_index=True
    )
    indexer_id: int
    indexer: ForeignKeyRelation[FlowGraph] = ForeignKeyField(
        "models.FlowGraph", related_name="favorites", db_index=True
    )
    rsrc_id = CharField(max_length=255)
    rsrc = JSONField[dict[str, Any]]()
    url = CharField(max_length=255, null=True)

    class Meta:
        table = "user_favorite"
        ordering = ["-created_at"]

    class PydanticMeta:
        exclude = ("user", "indexer")


class UserHistory(TortoiseModel):
    user_id: int
    user: ForeignKeyRelation[User] = ForeignKeyField(
        "models.User", related_name="histories", db_index=True
    )
    rel_type = CharEnumField(max_length=16, enum_type=HistoryType)
    rel_id = IntField()
    repetitions = IntField(default=0)
    keyword = CharField(max_length=4096, null=True)
    position = IntField(null=True)
    percentage = IntField(null=True)
    locator = JSONField[dict[str, Any] | None](null=True)

    class Meta:
        table = "user_history"
        ordering = ["-updated_at"]

    class PydanticMeta:
        exclude = ("user",)


class UserPermission(TortoiseModel):
    user_id: int
    user: ForeignKeyRelation[User] = ForeignKeyField(
        "models.User", related_name="permissions", db_index=True
    )
    rel_type = CharEnumField(max_length=16, enum_type=PermType)
    rel_id = IntField()

    class Meta:
        table = "user_permission"
        ordering = ["-created_at"]

    class PydanticMeta:
        exclude = ("user",)


# -------------------- Pydantic Models --------------------
class Permissions(BaseModel):
    indexer_ids: list[PositiveInt] = Field(max_length=999, default_factory=list)
    media_lib_ids: list[PositiveInt] = Field(max_length=999, default_factory=list)


class UserInfo(BaseModel):
    id: PositiveInt
    login_id: str
    username: str
    avatar: str | None
    role: UserRole
    preferences: dict
    user_agent: str | None = None
    client_ip: str
    login_at: datetime
    expire_at: datetime
    last_activity: datetime
    perms: Permissions | None = None


class UserLogin(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=64)


class UserQuery(Pageable):
    username: str | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    confirm_pwd: str = Field(min_length=6, max_length=64)


class UserPwd(BaseModel):
    cur_pwd: str = Field(min_length=6, max_length=64)
    new_pwd: str = Field(min_length=6, max_length=64)
    confirm_pwd: str = Field(min_length=6, max_length=64)


class UserAvatar(BaseModel, RequestFilesMixin):
    avatar: File | None = None


class UserPreference(KVPair):
    @model_validator(mode="before")
    @classmethod
    def check_retention(cls, data: Any) -> Any:
        """Validate history retention before value coercion.

        Args:
            data: The submitted preference data.

        Returns:
            The unchanged data for normal preference validation.

        Raises:
            ValueError: If history retention is not an integer of at least -1.
        """
        if isinstance(data, dict) and data.get("key") in (
            "search_records",
            "watch_records",
            "read_records",
        ):
            value = data.get("value")
            if type(value) is not int or value < -1:
                raise ValueError("history retention must be an integer of at least -1")
        return data


class FavoriteQuery(Pageable):
    indexer_id: PositiveInt | None = None
    rsrc_ids: list[PositiveInt | Annotated[str, Field(min_length=1)]] | None = Field(
        min_length=1, max_length=999, default=None
    )


class TextLocator(BaseModel, extra="forbid", strict=True):
    """Locate a paragraph or EPUB block within one published novel chapter."""

    version: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    paragraph: NonNegativeInt | None = None
    block_id: str | None = Field(pattern=r"^[0-9a-f]{32}$", default=None)
    offset: float = Field(ge=0, le=1, default=0)

    @model_validator(mode="after")
    def check_anchor(self) -> Self:
        """Require exactly one paragraph or block anchor.

        Returns:
            The validated locator.

        Raises:
            ValueError: If both anchors are present or absent.
        """
        if (self.paragraph is None) == (self.block_id is None):
            raise ValueError("exactly one paragraph or block anchor is required")
        return self


class ImageLocator(BaseModel, extra="forbid", strict=True):
    """Locate a comic page with a relative offset inside it."""

    version: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter_item_id: PositiveInt | None = None
    page_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    offset: float = Field(ge=0, le=1, default=0)


class HistoryQuery(Pageable):
    rel_type: HistoryType
    rel_id: NonNegativeInt | None = None


class HistoryEntry(BaseModel):
    rel_type: HistoryType
    rel_id: NonNegativeInt
    keyword: str | None = Field(max_length=4096, default=None)
    position: NonNegativeInt | None = None
    percentage: int | None = Field(ge=0, le=100, default=None)
    locator: TextLocator | ImageLocator | None = None

    @model_validator(mode="after")
    def check_locator(self) -> Self:
        """Keep reading anchors separate from search terms and video seconds.

        Returns:
            The entry with a locator matching its history type.

        Raises:
            ValueError: If reading fields are missing or used with another type.
        """
        if self.rel_type in (HistoryType.TEXT, HistoryType.IMAGE):
            expected = (
                TextLocator if self.rel_type == HistoryType.TEXT else ImageLocator
            )
            if (
                not isinstance(self.locator, expected)
                or self.rel_id == 0
                or self.position is not None
                or self.keyword is not None
            ):
                raise ValueError(
                    "reading history requires a work ID and matching locator"
                )
        elif self.locator is not None:
            raise ValueError("only reading history accepts a locator")
        return self
