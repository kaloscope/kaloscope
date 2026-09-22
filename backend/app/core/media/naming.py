"""Small, deliberately restricted filename templates for media libraries."""

import re
from pathlib import PurePosixPath
from typing import Any

FIELDS = frozenset(
    {
        "title",
        "originaltitle",
        "year",
        "season",
        "episode",
        "episode_code",
        "show_title",
        "show_year",
        "unique_id",
        "nfo_source",
    }
)
MOVIE_FIELDS = FIELDS - {"season", "episode", "episode_code", "show_title", "show_year"}
PARENT_FIELDS = FIELDS - {"title", "originaltitle", "episode", "episode_code"}
_TOKEN = re.compile(r"{{\s*([a-z_]+)\s*}}")
_UNSAFE = re.compile(r'[<>:"\\|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def validate_template(template: str, lib_type: str | None = None) -> str | None:
    """Validate a relative path containing only literal text and named tokens.

    Args:
        template: The relative path template to validate.
        lib_type: The library type used to restrict path depth and available
            placeholders, or `None` to apply only the shared template rules.

    Returns:
        The stripped template, or None if it is empty.

    Raises:
        ValueError: If the template exceeds the length or path depth limits,
            contains an unsafe component, or uses an unsupported placeholder.
    """
    template = template.strip()
    if not template:
        return None
    if len(template) > 1024:
        raise ValueError("rename template exceeds 1024 characters")
    parts = template.split("/")
    if lib_type == "movie" and len(parts) not in (1, 2):
        raise ValueError("movie templates require one or two path components")
    if lib_type == "tv_show" and len(parts) not in (2, 3):
        raise ValueError("TV templates require two or three path components")
    if len(parts) > 3:
        raise ValueError("rename templates support at most three path components")
    for index, part in enumerate(parts):
        literal = _TOKEN.sub("value", part)
        if (
            not part
            or part in (".", "..")
            or "{" in literal
            or "}" in literal
            or _UNSAFE.search(literal)
            or literal.endswith((".", " "))
            or _RESERVED.match(literal)
        ):
            raise ValueError("rename template contains an unsafe path component")
        if lib_type == "movie":
            allowed = MOVIE_FIELDS
        elif lib_type == "tv_show" and index < len(parts) - 1:
            allowed = PARENT_FIELDS
        else:
            allowed = FIELDS
        for name in _TOKEN.findall(part):
            if name not in allowed:
                raise ValueError(f"unsupported rename placeholder: {name}")
    return template


def _render_component(component: str, context: dict[str, Any]) -> str:
    """Render and validate a single filename or directory component.

    Args:
        component: The path component containing literal text and placeholders.
        context: The NFO field values used to replace placeholders.

    Returns:
        The rendered component with unsafe metadata characters replaced.

    Raises:
        ValueError: If a required field is missing, a numeric field is invalid,
            or the rendered component is empty, reserved, or too long.
    """

    def replace(match: re.Match) -> str:
        """Replace a placeholder with a sanitized NFO field value.

        Args:
            match: The placeholder match with the field name in its first group.

        Returns:
            The formatted field value with unsafe path characters replaced.

        Raises:
            ValueError: If the field is missing or a numeric field is invalid.
        """
        name = match[1]
        value = context.get(name)
        if name == "episode_code":
            season, episode = context.get("season"), context.get("episode")
            if season is not None and episode is not None:
                value = f"S{int(season):02d}E{int(episode):02d}"
        if value is None or not str(value).strip():
            raise ValueError(f"missing NFO field: {name}")
        if name in ("season", "episode"):
            value = f"{int(value):02d}"
        # treat metadata as plain text instead of a path or template expression
        return _UNSAFE.sub("_", str(value)).replace("/", "_").strip()

    result = _TOKEN.sub(replace, component).strip().rstrip(". ")
    if not result or result in (".", "..") or _RESERVED.match(result):
        raise ValueError("rendered filename is empty or reserved")
    if len(result.encode("utf-8")) > 240:
        raise ValueError("rendered filename component is too long")
    return result


def render_path(template: str, context: dict[str, Any], lib_type: str) -> PurePosixPath:
    """Render a relative path without a media extension.

    Args:
        template: The relative path template to render.
        context: The NFO field values used to replace placeholders.
        lib_type: The library type used to validate the template.

    Returns:
        The rendered path relative to the media library root.

    Raises:
        ValueError: If the template is empty or invalid, a required field is
            missing or invalid, or a rendered component is unsafe or too long.
    """
    template = validate_template(template, lib_type)
    if template is None:
        raise ValueError("rename template is empty")
    return PurePosixPath(
        *(_render_component(part, context) for part in template.split("/"))
    )


def render_directory(
    template: str, context: dict[str, Any], lib_type: str
) -> PurePosixPath:
    """Render only shared parent components, independently of episode metadata.

    Args:
        template: The relative path template whose parent components are rendered.
        context: The NFO field values used to replace parent placeholders.
        lib_type: The library type used to validate the template.

    Returns:
        The parent path relative to the media library root, or the current
        directory when the template has no parent components.

    Raises:
        ValueError: If the template is empty or invalid, a required parent field
            is missing or invalid, or a rendered component is unsafe or too long.
    """
    template = validate_template(template, lib_type)
    if template is None:
        raise ValueError("rename template is empty")
    return PurePosixPath(
        *(_render_component(part, context) for part in template.split("/")[:-1])
    )


def render_filename(template: str, context: dict[str, Any], lib_type: str) -> str:
    """Render the filename independently from the shared parent context.

    Args:
        template: The relative path template whose final component is rendered.
        context: The NFO field values used to replace filename placeholders.
        lib_type: The library type used to validate the template.

    Returns:
        The rendered filename without a media extension.

    Raises:
        ValueError: If the template is empty or invalid, a required filename field
            is missing or invalid, or the filename is unsafe or too long.
    """
    template = validate_template(template, lib_type)
    if template is None:
        raise ValueError("rename template is empty")
    return _render_component(template.split("/")[-1], context)
