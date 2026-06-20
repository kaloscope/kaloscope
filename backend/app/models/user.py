from datetime import datetime
from enum import StrEnum, auto
from typing import Annotated, Any

from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt
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

from app.models.base import Pageable, RequestFilesMixin, TortoiseModel
from app.models.flow import FlowGraph


# -------------------- Enumerations --------------------
class UserRole(StrEnum):
    USER = auto()
    ADMIN = auto()


class HistoryType(StrEnum):
    SEARCH = auto()
    VIDEO = auto()


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


class FavoriteQuery(Pageable):
    indexer_id: PositiveInt | None = None
    rsrc_ids: list[PositiveInt | Annotated[str, Field(min_length=1)]] | None = Field(
        min_length=1, max_length=999, default=None
    )


class HistoryQuery(Pageable):
    rel_type: HistoryType


class HistoryEntry(BaseModel):
    rel_type: HistoryType
    rel_id: NonNegativeInt
    keyword: str | None = Field(max_length=4096, default=None)
    position: NonNegativeInt | None = None
    percentage: int | None = Field(ge=0, le=100, default=None)
