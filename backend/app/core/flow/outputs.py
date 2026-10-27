"""Check authored indexer and movie results without executing workflow nodes."""

import re
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from app.core.flow.nodes.base import parse_start_config
from app.core.media.common import ContentError
from app.core.media.shelver import TEMPLATES_PATH, render_nfo
from app.models.workflow_authoring import OutputIssue
from app.utils.bittorrent import (
    get_magnet_hash,
    get_magnet_hash_v2,
    is_info_hash,
    is_info_hash_v2,
)

type Text = Annotated[str, Field(min_length=1, pattern=r"\S")]


def download_link_kind(link: Any) -> Literal["hash", "http"] | None:
    """Classify a search result's download link by shape, without contacting it.

    Args:
        link: Value of a search result's `link` field.

    Returns:
        `hash` for a BT hash or a magnet carrying one, `http` for an absolute
        HTTP(S) URL whose torrent content is unproven, otherwise `None`.
    """
    if not isinstance(link, str):
        return None
    link = link.strip()
    if (
        is_info_hash(link)
        or is_info_hash_v2(link)
        or (
            link.startswith("magnet:")
            and (get_magnet_hash(link) or get_magnet_hash_v2(link))
        )
    ):
        return "hash"
    try:
        url = urlsplit(link)
    except ValueError:
        return None
    return "http" if url.scheme in {"http", "https"} and url.netloc else None


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


class AuthOutput(BaseModel):
    """Check a successful login result that `auth_end` saves as `$auth`."""

    model_config = ConfigDict(strict=True, extra="allow")

    name: Text
    token: Text | None = None
    expires_in: Annotated[int, Field(gt=0)] | None = None


def _config_issue(config: str) -> OutputIssue | None:
    """Report YAML that runtime would ignore, without expanding aliases.

    Args:
        config: Raw `auth_start` YAML configuration.

    Returns:
        A located issue for invalid YAML or anchors/aliases, otherwise `None`. Blank
        configuration is left to the missing-login check.
    """
    try:
        # parse events reference aliases once instead of expanding them like a load
        for event in yaml.parse(config, Loader=yaml.SafeLoader):
            if getattr(event, "anchor", None) is not None:
                return OutputIssue(
                    path="config", message="Remove YAML anchors and aliases."
                )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f" at line {mark.line + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or "invalid syntax"
        return OutputIssue(
            path="config",
            message=f"Invalid YAML{line}: {problem}; runtime ignores all of it.",
        )
    return None


def _check_cookie(cookie: Any) -> tuple[list[OutputIssue], list[str], bool]:
    """Check a configured session cookie against the key cookie jars store.

    Args:
        cookie: Parsed `config.cookie` value.

    Returns:
        Located issues, storage warnings and whether a complete cookie identifies the
        login. Absent cookies and the default block with only `path: /` are inactive.
    """
    if cookie is None:
        return [], [], False
    if not isinstance(cookie, dict):
        return (
            [
                OutputIssue(
                    path="config.cookie",
                    message="Use a mapping with domain, path and name.",
                )
            ],
            [],
            False,
        )
    domain, path, name = (cookie.get(key) for key in ("domain", "path", "name"))
    if domain in (None, "") and name in (None, "") and path in (None, "", "/"):
        return [], [], False
    issues = []
    if (
        not isinstance(domain, str)
        or not domain.strip()
        or domain != domain.lower()
        or re.search(r"[\s/]|^[^:]+:\d+$", domain)
    ):
        issues.append(
            OutputIssue(
                path="config.cookie.domain",
                message="Use the stored domain: `.d` for `Domain=d`, otherwise the "
                "lowercase login host without port.",
            )
        )
    if not isinstance(path, str) or not path.startswith("/") or re.search(r"\s", path):
        issues.append(
            OutputIssue(
                path="config.cookie.path",
                message="Use the stored path: `Path`, otherwise the login URL path "
                "before its last `/` (`/a/login` -> `/a`, `/login` -> `/`).",
            )
        )
    if not isinstance(name, str) or not re.fullmatch(r"[^\s=;,]+", name):
        issues.append(
            OutputIssue(
                path="config.cookie.name", message="Use the session cookie's name."
            )
        )
    warnings = []
    if (
        not issues
        and isinstance(domain, str)
        and "." not in domain.strip(".")
        and ":" not in domain
    ):
        host = domain.strip(".")[:100]
        warnings.append(
            f"HTTPX stores dotless host {host} as {host}.local; curl_cffi keeps {host}."
        )
    return issues, warnings, not issues


def check_auth(
    value: Any, config: str
) -> tuple[
    list[OutputIssue], list[str], Literal["authenticated", "unauthenticated"] | None
]:
    """Check an `auth_end` result against the `auth_start` configuration it uses.

    Authoring requires an object with a string `name`, an optional nonblank string
    `token` and positive integer `expires_in` seconds. Legacy runtime saves any
    truthy name and ignores non-integer `expires_in`; this check does not change it.

    Args:
        value: Parsed JSONC output of the proposed `auth_end` template.
        config: Exact `auth_start` YAML configuration, parsed as runtime reads it.
            Invalid YAML and anchors/aliases are reported instead of being parsed.

    Returns:
        At most twelve field issues, compact runtime consequences and, when valid,
        the outcome: `authenticated` replaces workflow-local `$auth`, while
        `unauthenticated` (empty or absent name) keeps any earlier login. No node
        runs and no authentication variable or cookie is read or saved.
    """
    issues = []
    config_issue = _config_issue(config)
    settings = {} if config_issue else parse_start_config(config)
    login = settings.get("login")
    if config_issue:
        issues.append(config_issue)
    elif not isinstance(login, dict):
        issues.append(
            OutputIssue(
                path="config.login",
                message="Use a mapping such as {mode: password, required: true} "
                "to show the login form.",
            )
        )
    else:
        if login.get("mode") not in (None, "password"):
            issues.append(
                OutputIssue(
                    path="config.login.mode",
                    message="Only password login is supported.",
                )
            )
        if not isinstance(login.get("required", False), bool):
            issues.append(
                OutputIssue(path="config.login.required", message="Use true or false.")
            )
    cookie_issues, warnings, cookie_login = _check_cookie(settings.get("cookie"))
    issues.extend(cookie_issues)

    outcome = None
    if not isinstance(value, dict):
        issues.append(
            OutputIssue(
                path="$", message='Return an object; use {"name": ""} on failure.'
            )
        )
    elif value.get("name") in (None, ""):
        if any(value.get(key) not in (None, "") for key in ("token", "expires_in")):
            issues.append(
                OutputIssue(
                    path="name",
                    message="Empty name discards token/expires_in; map the display "
                    "name, or omit them for a failed login.",
                )
            )
        else:
            outcome = "unauthenticated"
            warnings.append("Failed login: $auth is unchanged; an earlier login stays.")
            if cookie_login:
                warnings.append(
                    "The login page still reports a login while the configured "
                    "cookie exists; choose a cookie set only after login."
                )
    else:
        try:
            auth = AuthOutput.model_validate(value)
        except ValidationError as exc:
            issues.extend(
                OutputIssue(
                    path=".".join(str(part) for part in error["loc"]),
                    message=error["msg"],
                )
                for error in exc.errors(include_input=False, include_url=False)
            )
        else:
            outcome = "authenticated"
            if auth.token is None and not cookie_login and not config_issue:
                issues.append(
                    OutputIssue(
                        path="token",
                        message="Map the login token, or configure config.cookie "
                        "for a cookie session.",
                    )
                )
            if auth.expires_in is not None:
                warnings.append("$auth expires after expires_in seconds (not saved).")
            elif cookie_login:
                warnings.append(
                    "Without expires_in, $auth copies the configured cookie's saved "
                    "expiry. Cookies without Expires/Max-Age are not saved: $auth "
                    "then never expires but the cookie ends on restart."
                )
            else:
                warnings.append(
                    "Without expires_in or a configured cookie, $auth stays until "
                    "logout or the next successful login."
                )
            warnings.append(
                "Validate a failure sample too; HTTP 200 alone is not success."
            )
    if issues:
        outcome = None
    if len(issues) > 12:
        warnings.insert(0, "Additional field errors omitted; fix these and recheck.")
    return issues[:12], list(dict.fromkeys(warnings))[:4], outcome


def _download_issues(
    page: SearchOutput, context: dict, warnings: list[str]
) -> list[OutputIssue]:
    """Check what download plans and pagination need from a search page fixture.

    Args:
        page: Validated search page.
        context: Fixtures; `downloads: true` requires a link on every item and
            `previous_ids` lists the prior page's IDs.
        warnings: Sample limitations to extend.

    Returns:
        Located issues for missing or unusable links and unchanged later pages.
    """
    issues = []
    number = context.get("page_num")
    ids = [item.id for item in page.items]
    previous = context.get("previous_ids")
    if isinstance(previous, list) and type(number) is int and number > 1 and ids:
        repeated = set(ids) & {str(item) for item in previous}
        if repeated == set(ids):
            issues.append(
                OutputIssue(
                    path="items",
                    message="Same IDs as the previous page; apply the page parameter.",
                )
            )
        elif repeated:
            warnings.append("Some IDs repeat the previous page; check pagination.")
    offset = (number - 1) * len(ids) if type(number) is int and number > 0 else 0
    if len(ids) > 1 and ids in (
        [str(index + offset) for index in range(len(ids))],
        [str(index + offset + 1) for index in range(len(ids))],
    ):
        warnings.append("IDs look like positions; use the source's own identifiers.")
    if context.get("downloads") is not True:
        return issues
    for index, item in enumerate(page.items):
        kind = download_link_kind(item.link)
        if kind is None:
            issues.append(
                OutputIssue(
                    path=f"items.{index}.link",
                    message="Map the observed magnet, BT hash or absolute torrent "
                    "URL; never a detail or playback page.",
                )
            )
        elif kind == "http":
            warnings.append(
                "HTTP links are unproven: a detail or player page is not a torrent; "
                "URL shape does not prove torrent content."
            )
    return issues


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
            if (
                type(number) is int
                and number > 0
                and not page.items
                and page.total is None
                and page.totalPages is not None
                # one reported page may describe zero matches
                and page.totalPages > 1
                and number <= page.totalPages
            ):
                issues.append(
                    OutputIssue(
                        path="items",
                        message="Empty page before the reported totalPages; "
                        "check item extraction and pagination.",
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
            elif number == 1 and page.items and page.total == len(page.items) == size:
                warnings.append(
                    "total equals this full page's count; omit it unless the source "
                    "reports all matches."
                )
            issues.extend(_download_issues(page, context, warnings))
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
