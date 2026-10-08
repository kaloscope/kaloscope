from enum import StrEnum, auto
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator
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

from app.core.media.naming import validate_template
from app.core.transcode import (
    HWAccelType,
    QualityLevel,
    ResolutionLimit,
    TranscodeOptions,
)
from app.models.base import IDs, Pageable, TortoiseModel
from app.models.flow import GraphRef
from app.models.general import GlobalConfig
from app.utils.disk import is_directory


# -------------------- Enumerations --------------------
class LibType(StrEnum):
    MOVIE = auto()
    TV_SHOW = auto()
    NOVEL = auto()
    COMIC = auto()


class MediaType(StrEnum):
    VIDEO = auto()
    AUDIO = auto()
    IMAGE = auto()
    TEXT = auto()


class MediaFormat(StrEnum):
    TXT = auto()
    EPUB = auto()
    DIR = auto()
    CBZ = auto()
    ZIP = auto()


class IndexState(StrEnum):
    PENDING = auto()
    READY = auto()
    EMPTY = auto()
    ERROR = auto()


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
    scan_on_startup = BooleanField(default=True, db_default=True)
    danmaku_server = CharField(max_length=255, null=True)
    danmaku_ttl = IntField(default=24)
    rename_template = CharField(max_length=1024, null=True)
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
    lib_id: int  # https://tortoise.github.io/models.html#the-db-backing-field
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
    format = CharEnumField(enum_type=MediaFormat, max_length=16, null=True)
    extra = JSONField[dict[str, Any] | None](null=True)
    index_version = CharField(max_length=64, null=True)
    index_state = CharEnumField(enum_type=IndexState, max_length=16, null=True)
    index_error = CharField(max_length=64, null=True)
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
        indexes = (
            ("path",),
            ("hash",),
            ("lib_id", "parent_id", "visible", "created_at"),
        )

    class PydanticMeta:
        exclude = ("extra",)


class MediaEvent(TortoiseModel):
    lib_id: int
    lib: ForeignKeyRelation[MediaLib] = ForeignKeyField(
        "models.MediaLib", related_name="events", db_index=True
    )
    src_path = CharField(max_length=4096)
    dest_path = CharField(max_length=4096, null=True)
    event_type = CharField(max_length=16)
    is_directory = BooleanField(default=False)
    payload = JSONField[dict[str, Any] | None](null=True)

    class Meta:
        table = "media_event"
        ordering = ["created_at"]
        indexes = (("lib_id", "event_type"),)


# -------------------- Pydantic Models --------------------
class ReadingMetadataSync(BaseModel):
    """Validate item-owned metadata synchronization fields stored in extra."""

    model_config = ConfigDict(extra="forbid", strict=True)

    state: Literal["none", "pending", "ready", "error"]
    format: Literal["opf", "comicinfo"] | None = None
    relative_path: str | None = Field(default=None, min_length=1, max_length=4096)
    file_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: str | None = Field(default=None, min_length=1, max_length=64)


class MediaLibUpsert(BaseModel):
    id: PositiveInt | None = None
    lib_type: LibType | None = None
    dir: str | None = Field(min_length=1, max_length=4096, default=None)
    name: str = Field(min_length=1, max_length=64)
    language: str | None = None
    scan_on_startup: bool = True
    danmaku_server: str | None = Field(max_length=255, default=None)
    danmaku_ttl: int | None = Field(ge=0, le=8760, default=None)
    rename_template: str | None = Field(max_length=1024, default=None)
    triggers: list[GraphRef] | None = None

    @model_validator(mode="after")
    def check_dir(self) -> Self:
        """Validate the library type, directory and rename template.

        Returns:
            The validated library settings.

        Raises:
            ValueError: If the library type, directory or template is invalid.
        """
        if not self.id:
            if self.lib_type is None:
                raise ValueError("library type is required")
            if not self.dir or not is_directory(self.dir):
                raise ValueError(f"invalid directory: {self.dir}")
            if self.lib_type in (LibType.NOVEL, LibType.COMIC):
                self.dir = str(Path(self.dir).resolve())
        if self.rename_template is not None:
            self.rename_template = validate_template(
                self.rename_template, self.lib_type
            )
        return self


class MediaQuery(Pageable):
    lib_id: PositiveInt | None = None
    keyword: str | None = None
    path: str | None = None


class MediaContentQuery(BaseModel):
    """Select a reading section or comic page range from the published version."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str | None = Field(
        default=None, pattern=r"^(?:[0-9a-f]{32}|item:[1-9][0-9]*)$"
    )
    version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class MediaAssetQuery(BaseModel):
    """Require the published version when reading an indexed image resource."""

    model_config = ConfigDict(extra="forbid")

    v: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContentChapter(BaseModel):
    """Expose a reading section without internal cache offsets or source paths."""

    id: str
    title: str | None
    part: int
    volume: None = None


class ContentResponse(BaseModel):
    """Share chapter selection and ownership across reading content responses."""

    item_id: int
    source_item_id: int
    title: str
    version: str
    chapter_id: str
    chapters: list[ContentChapter]


class TextContent(ContentResponse):
    """Return one bounded TXT section as plain-text paragraphs."""

    media_type: Literal["text"] = "text"
    format: Literal["txt"] = "txt"
    content_type: Literal["text"] = "text"
    text: list[str]


class ContentRun(BaseModel):
    """Expose plain text and supported emphasis without source HTML."""

    text: str
    marks: list[Literal["strong", "em"]]


class ContentText(BaseModel):
    """Expose a paragraph, quote or heading from validated reading blocks."""

    id: str
    type: Literal["paragraph", "heading", "quote"]
    runs: list[ContentRun]
    level: int | None = Field(default=None, ge=1, le=6)


class ContentList(BaseModel):
    """Expose an ordered or unordered list of text runs."""

    id: str
    type: Literal["list"]
    ordered: bool
    items: list[list[ContentRun]]
    start: int | None = None


class ContentImage(BaseModel):
    """Expose an opaque image ID and application URL, or a missing-image placeholder."""

    id: str
    type: Literal["image"]
    asset_id: str | None
    url: str | None
    alt: str


class ContentWarning(BaseModel):
    """Associate a controlled reading warning with its displayed block."""

    code: Literal[
        "missing_image",
        "external_image",
        "invalid_image_reference",
        "encrypted_image",
        "unsupported_image",
        "invalid_image",
        "image_limit_exceeded",
        "unsupported_content",
        "simplified_layout",
    ]
    block_id: str


class EpubContent(ContentResponse):
    """Return validated EPUB blocks with application-owned image URLs."""

    media_type: Literal["text"] = "text"
    format: Literal["epub"] = "epub"
    content_type: Literal["blocks"] = "blocks"
    blocks: list[
        Annotated[ContentText | ContentList | ContentImage, Field(discriminator="type")]
    ]
    warnings: list[ContentWarning]


class ImageContent(ContentResponse):
    """Return a bounded page of indexed comic image URLs."""

    media_type: Literal["image"] = "image"
    format: Literal["dir", "cbz", "zip"]
    content_type: Literal["images"] = "images"
    images: list[str]
    image_count: int
    next_offset: int | None


class MediaMetadata(BaseModel):
    graph_id: PositiveInt
    metadata: dict


class MediaDel(IDs):
    local: bool = False


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

    hwaccel: HWAccelType | None = None
    """Hardware acceleration types."""

    quality: QualityLevel | None = None
    """Transcode quality level."""

    resolution: ResolutionLimit | None = None
    """Output resolution limit."""

    async def options(self) -> TranscodeOptions:
        """Convert query parameters to a `TranscodeOptions` instance."""
        kwargs: dict[str, Any] = {}

        async def _option(key: str) -> Any:
            value = getattr(self, key)
            if value is not None:
                kwargs[key] = value
            else:
                config = await GlobalConfig.get_or_none(key=f"transcode.{key}")
                if config and config.value is not None:
                    kwargs[key] = config.value

        await _option("hwaccel")
        await _option("quality")
        await _option("resolution")
        return TranscodeOptions(**kwargs)


class TranscodeTaskQuery(BaseModel):
    state: str | None = None
    keyword: str | None = None
    ordering: str | None = None
