from enum import StrEnum, auto
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, PositiveInt, model_validator
from tortoise.fields import (
    BigIntField,
    BooleanField,
    CharEnumField,
    CharField,
    DatetimeField,
    DecimalField,
    ForeignKeyField,
    ForeignKeyNullableRelation,
    ForeignKeyRelation,
    IntField,
    JSONField,
    ReverseRelation,
)

from app.models.base import Pageable, TortoiseModel
from app.models.flow import GraphRef
from app.utils.disk import is_directory


# -------------------- Enumerations --------------------
class LibType(StrEnum):
    MOVIE = auto()
    TV_SHOW = auto()


class MediaType(StrEnum):
    VIDEO = auto()
    AUDIO = auto()
    IMAGE = auto()
    TEXT = auto()


class NFOType(StrEnum):
    MOVIE = "movie"
    TV_SHOW = "tvshow"
    EPISODE = "episode"


class Language(StrEnum):
    EN_US = "en-US"
    ZH_CN = "zh-CN"


# -------------------- ORM Models --------------------
class MediaLib(TortoiseModel):
    lib_type = CharEnumField(max_length=16, enum_type=LibType)
    dir = CharField(max_length=4096, unique=True)
    name = CharField(max_length=64, unique=True)
    language = CharEnumField(max_length=16, enum_type=Language, null=True)
    priority = IntField(unique=True)
    danmaku_server = CharField(max_length=255, null=True)
    danmaku_ttl = IntField(default=24)
    # relational fields
    items: ReverseRelation["MediaItem"]
    events: ReverseRelation["MediaEvent"]
    plans: ReverseRelation
    tasks: ReverseRelation

    class Meta:
        table = "media_lib"
        ordering = ["priority"]

    class PydanticMeta:
        exclude = ("items", "events", "plans", "tasks")


class MediaItem(TortoiseModel):
    lib_id: int
    lib: ForeignKeyRelation[MediaLib] = ForeignKeyField(
        "models.MediaLib", related_name="items", db_index=True
    )
    parent_id: int | None
    parent: ForeignKeyNullableRelation["MediaItem"] = ForeignKeyField(
        "models.MediaItem", related_name="children", db_index=True, null=True
    )
    dir = CharField(max_length=4096)
    path = CharField(max_length=4096)
    name = CharField(max_length=255)
    hash = CharField(max_length=32, null=True)
    size = BigIntField(null=True)
    visible = BooleanField(default=True)
    nfo_path = CharField(max_length=4096, null=True)
    nfo_mtime = DatetimeField(null=True)
    nfo_source = CharField(max_length=64, null=True)
    danmaku_path = CharField(max_length=4096, null=True)
    danmaku_meta = JSONField[dict[str, Any] | None](null=True)
    unique_id = CharField(max_length=255, null=True)
    title = CharField(max_length=255, null=True)
    year = IntField(null=True)
    aired = CharField(max_length=64, null=True)
    season = IntField(null=True)
    episode = IntField(null=True)
    poster = CharField(max_length=255, null=True)
    backdrop = CharField(max_length=255, null=True)
    rating = DecimalField(max_digits=4, decimal_places=2, null=True)
    # relational fields
    children: ReverseRelation["MediaItem"]

    class Meta:
        table = "media_item"
        ordering = ["-created_at"]
        unique_together = (("lib", "path"),)


class MediaEvent(TortoiseModel):
    lib_id: int
    lib: ForeignKeyRelation[MediaLib] = ForeignKeyField(
        "models.MediaLib", related_name="events", db_index=True
    )
    src_path = CharField(max_length=4096)
    dest_path = CharField(max_length=4096, null=True)
    event_type = CharField(max_length=16)
    is_directory = BooleanField(default=False)

    class Meta:
        table = "media_event"
        ordering = ["created_at"]


# -------------------- Pydantic Models --------------------
class MediaLibUpsert(BaseModel):
    id: PositiveInt | None = None
    lib_type: LibType | None = None
    dir: str | None = Field(min_length=1, max_length=4096, default=None)
    name: str = Field(min_length=1, max_length=64)
    language: str | None = None
    danmaku_server: str | None = Field(max_length=255, default=None)
    danmaku_ttl: int | None = Field(ge=0, le=8760, default=None)
    triggers: list[GraphRef] | None = None

    @model_validator(mode="after")
    def check_dir(self) -> Self:
        if not self.id and (not self.dir or not is_directory(self.dir)):
            raise ValueError(f"invalid directory: {self.dir}")
        return self


class MediaQuery(Pageable):
    lib_id: PositiveInt | None = None
    keyword: str | None = None
    path: str | None = None


class MediaMetadata(BaseModel):
    graph_id: PositiveInt
    metadata: dict


class MediaResource(BaseModel):
    path: str


class TranscodeQuery(MediaResource):
    """Query parameters for the media stream endpoint with optional transcoding.

    When `transcode` is `False`, the endpoint serves the raw file directly
    with HTTP Range support.

    When `transcode` is `True`, the server transcodes the video in real-time
    using ffmpeg to HLS (M3U8 + MPEG-TS segments).
    """

    transcode: bool = False
    """Whether to transcode the video."""

    quality: Literal["low", "medium", "high"] | None = None
    """Transcode quality preset."""

    resolution: Literal["original", "1080p", "720p", "480p"] | None = None
    """Output resolution limit."""

    hwaccel: (
        Literal["amf", "qsv", "nvenc", "v4l2m2m", "vaapi", "videotoolbox", "rkmpp"]
        | None
    ) = None
    """Hardware acceleration types."""
