"""Check authored indexer and movie results without executing workflow nodes."""

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from app.core.media.common import ContentError
from app.core.media.shelver import TEMPLATES_PATH, render_nfo
from app.models.workflow_authoring import OutputIssue

type Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
type Count = Annotated[int, Field(ge=0)]


class Resource(BaseModel):
    """Check fields consumed by search cards and resource viewers, without coercion."""

    model_config = ConfigDict(strict=True, extra="allow")

    id: Text | None = None
    title: Text | None = None
    cover: str | None = None
    ranking: float | None = None
    rating: float | None = None
    link: str | None = None
    size: str | None = None
    misc: str | None = None
    category: str | None = None
    uploader: str | None = None
    uploaded_at: str | None = None


class SearchItem(Resource):
    """Require a stable remote identity and a usable title for authored search hits."""

    id: Text
    title: Text


class SearchOutput(BaseModel):
    """Accept the page's exact-total, page-count and unknown-total variants."""

    model_config = ConfigDict(strict=True, extra="allow")

    items: list[SearchItem]
    total: Count | None = None
    totalPages: Count | None = None


class Chapter(BaseModel):
    """Describe a directory entry that can be opened by remote ID or direct URL."""

    model_config = ConfigDict(strict=True, extra="allow")

    id: Text | None = None
    url: Text | None = None
    title: str | None = None
    volume: str | None = None


class Definition(BaseModel):
    """Describe an alternate playback source."""

    model_config = ConfigDict(strict=True, extra="allow")

    url: Text
    definition: str | int


class Danmaku(BaseModel):
    """Describe one timed player comment in milliseconds."""

    model_config = ConfigDict(strict=True, extra="allow")

    id: str | None = None
    text: str
    mode: Literal["scroll", "top", "bottom"] | None = None
    color: str | None = None
    start: Annotated[float, Field(ge=0)] | None = None
    duration: Annotated[float, Field(ge=0)] | None = None


class DetailsOutput(Resource):
    """Check content or a chapter directory accepted by the resource page."""

    media_type: Literal["video", "audio", "image", "text"] | None = None
    url: str | None = None
    video_type: Literal["mp4", "flv", "hls", "dash"] | None = None
    text: str | list[str] | None = None
    images: list[str] | None = None
    image_count: Count | None = None
    chapters: list[Chapter] | None = None
    definitions: list[Definition] | None = None
    danmakus: list[Danmaku] | None = None


class Actor(BaseModel):
    """Check fields used by the existing movie NFO template."""

    model_config = ConfigDict(strict=True, extra="allow")

    name: Text
    role: str | None = None
    thumb: str | None = None


class MovieOutput(BaseModel):
    """Check authored candidates before the existing NFO render/read-back check."""

    model_config = ConfigDict(strict=True, extra="allow")

    title: Text
    id: str | None = None
    site: str | None = None
    originaltitle: str | None = None
    tagline: str | None = None
    plot: str | None = None
    year: Annotated[int, Field(ge=1, le=9999)] | None = None
    rating: Annotated[float, Field(ge=0, le=10)] | None = None
    premiered: str | None = None
    country: str | None = None
    mpaa: str | None = None
    tags: list[str] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    studios: list[str] = Field(default_factory=list)
    directors: list[str] = Field(default_factory=list)
    writers: list[str] = Field(default_factory=list)
    credits: list[str] = Field(default_factory=list)
    actors: list[Actor] = Field(default_factory=list)
    poster: str | None = None
    backdrop: str | None = None
    lockdata: bool | None = None

    @field_validator("premiered")
    @classmethod
    def check_premiered(cls, value: str | None) -> str | None:
        """Check the documented NFO date without guessing or normalizing it.

        Args:
            value: Optional release date in the candidate.

        Returns:
            The original date or absent value.

        Raises:
            ValueError: If a supplied date is not a real `YYYY-MM-DD` date.
        """
        if value and (
            len(value) != 10 or date.fromisoformat(value).isoformat() != value
        ):
            raise ValueError("Use YYYY-MM-DD for premiered")
        return value


def check_output(
    mode: Literal["search", "details", "movie"], value: Any, context: dict
) -> tuple[list[OutputIssue], list[str]]:
    """Check supplied business output, retaining original values for node parity.

    Authored search hits need nonblank string IDs/titles, and movie numeric fields
    must be JSON numbers. These authoring checks do not alter legacy node behavior.

    Args:
        mode: End-node contract to check.
        value: Parsed JSONC output of the proposed end-node template.
        context: Explicit fixture variables, including pagination or media defaults.

    Returns:
        At most twelve field errors and compact sample limitations. No workflow,
        filesystem target, database record or scheduler is accessed or changed.
    """
    issues = []
    warnings = []
    try:
        if mode == "search":
            page = SearchOutput.model_validate(value)
            ids = [item.id for item in page.items]
            if len(ids) != len(set(ids)):
                issues.append(
                    OutputIssue(path="items", message="Use unique remote IDs.")
                )
            number, size = context.get("page_num"), context.get("page_size")
            for name, number_value in (("page_num", number), ("page_size", size)):
                if number_value is not None and (
                    type(number_value) is not int or number_value < 1
                ):
                    issues.append(
                        OutputIssue(
                            path=f"context.{name}", message="Use a positive integer."
                        )
                    )
            if type(size) is int and size > 0:
                if page.total is not None and page.totalPages is not None:
                    expected = (page.total + size - 1) // size
                    if page.totalPages != expected and not (
                        page.total == 0 and page.totalPages == 1
                    ):
                        issues.append(
                            OutputIssue(
                                path="totalPages",
                                message="Must agree with total and page_size.",
                            )
                        )
                if len(page.items) > size:
                    issues.append(
                        OutputIssue(path="items", message="Page exceeds page_size.")
                    )
                if (
                    type(number) is int
                    and number > 0
                    and not page.items
                    and page.total is not None
                    and (number - 1) * size < page.total
                ):
                    issues.append(
                        OutputIssue(
                            path="items",
                            message="Empty page before the reported total; "
                            "check item extraction and pagination.",
                        )
                    )
                if type(number) is int and number > 0 and page.items:
                    if page.total is not None and page.total < (
                        number - 1
                    ) * size + len(page.items):
                        issues.append(
                            OutputIssue(
                                path="total",
                                message="Count all matches, including earlier pages.",
                            )
                        )
                    if page.totalPages is not None and page.totalPages < number:
                        issues.append(
                            OutputIssue(
                                path="totalPages",
                                message="Cannot precede a nonempty requested page.",
                            )
                        )
            if page.total is not None and page.total < len(page.items):
                issues.append(
                    OutputIssue(
                        path="total",
                        message="Cannot be smaller than this page's item count.",
                    )
                )
            if page.items and page.totalPages == 0:
                issues.append(
                    OutputIssue(
                        path="totalPages",
                        message="Cannot be zero for nonempty results.",
                    )
                )
            if page.total is None and not page.totalPages:
                warnings.append(
                    "Total is unknown; the page uses simple pagination. "
                    "Do not invent a total."
                )
            warnings.append(
                "One page cannot prove pagination stability or a remote total; "
                "check first/next/empty samples."
            )
        elif mode == "details":
            if value is None:
                return [], [
                    "Null means not found; it does not demonstrate usable details."
                ]
            details = DetailsOutput.model_validate(value)
            media = details.media_type or context.get("media_type")
            if media not in ("video", "audio", "image", "text"):
                issues.append(
                    OutputIssue(
                        path="media_type",
                        message="Supply media_type in output or fixture defaults.",
                    )
                )
            chapters = details.chapters or []
            images = [item for item in details.images or [] if item.strip()]
            for index, chapter in enumerate(chapters):
                if not chapter.id and not chapter.url:
                    issues.append(
                        OutputIssue(
                            path=f"chapters[{index}]",
                            message="Provide a remote id or a direct url.",
                        )
                    )
            has_content = bool(
                (media in ("video", "audio") and details.url and details.url.strip())
                or (media == "image" and images)
                or (
                    media == "text"
                    and any(
                        item.strip()
                        for item in (
                            [details.text]
                            if isinstance(details.text, str)
                            else details.text or []
                        )
                    )
                )
            )
            if not has_content and not chapters:
                issues.append(
                    OutputIssue(
                        path="$",
                        message="Return content or a chapter directory; "
                        "use null for not found.",
                    )
                )
            elif not has_content:
                warnings.append(
                    "Directory only: also validate a chapter lookup; "
                    "a directory cannot prove playback/reading."
                )
            if media == "audio":
                warnings.append(
                    "The current resource page has no audio viewer; "
                    "use supported media before claiming playback."
                )
            if details.image_count is not None and details.image_count < len(images):
                issues.append(
                    OutputIssue(
                        path="image_count",
                        message="Cannot be smaller than the returned image count.",
                    )
                )
            warnings.append(
                "URLs are not fetched; this check does not prove content is reachable."
            )
        else:
            candidates = TypeAdapter(list[MovieOutput]).validate_python(
                value, strict=True
            )
            template = (TEMPLATES_PATH / "movie.nfo").read_text(encoding="utf-8-sig")
            for index, candidate in enumerate(candidates):
                try:
                    render_nfo("movie", template, value[index])
                except ContentError:
                    issues.append(
                        OutputIssue(
                            path=f"[{index}]",
                            message="Candidate fails NFO render/read-back; "
                            "check field lengths, dates and XML characters.",
                        )
                    )
                unknown = set(candidate.model_extra or {})
                if unknown:
                    warnings.append(
                        f"[{index}]: Unknown movie fields are not mapped by "
                        "the NFO template; use the movie node example."
                    )
            if not candidates:
                warnings.append(
                    "Empty candidates mean no match; test a matching HTML sample too."
                )
            warnings.append(
                "Only sample metadata was checked. Automatic ingestion writes "
                "the first candidate; no NFO was written."
            )
    except ValidationError as exc:
        issues = [
            OutputIssue(
                path=".".join(str(part) for part in error["loc"]) or "$",
                message=error["msg"],
            )
            for error in exc.errors(include_input=False, include_url=False)[:12]
        ]
        if exc.error_count() > 12:
            warnings.insert(
                0, "Additional field errors omitted; fix these and recheck."
            )
    if len(issues) > 12:
        warnings.insert(0, "Additional field errors omitted; fix these and recheck.")
    return issues[:12], list(dict.fromkeys(warnings))[:4]
