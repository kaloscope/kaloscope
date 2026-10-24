"""JSON serialization and parsing utilities."""

import decimal
import re
from functools import partial
from pathlib import Path
from typing import Any

import orjson
from sanic.log import Colors, logger

from app.core.constants import ENCODING

type JSONType = dict[str, JSONType] | list[JSONType] | str | int | float | bool | None

COMMENTS_PATTERN = re.compile(r'//.*?$|/\*.*?\*/|"(?:\\.|[^\\"])*"', re.S | re.M)
TRAILING_COMMAS_PATTERN = re.compile(r',\s*([\]}])|"(?:\\.|[^\\"])*"', re.S)


def default(obj):
    """Default JSON serializer for objects not serializable by orjson.

    See https://github.com/ijl/orjson?tab=readme-ov-file#default for more details.

    Args:
        obj: Object to serialize.

    Returns:
        Serialized object.
    """
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, Path):
        return str(obj.resolve())
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    return None


dumps = partial(orjson.dumps, default=default)
loads = orjson.loads


def loads_jsonc(string: str) -> JSONType:
    """Parse JSON with comments and trailing commas while retaining parse errors.

    Args:
        string: JSONC document to parse.

    Returns:
        Parsed JSON value, including explicit `null` as `None`.

    Raises:
        orjson.JSONDecodeError: If the document is invalid.
    """
    string = COMMENTS_PATTERN.sub(
        lambda m: "" if (s := m.group(0)).startswith("/") else s, string
    )
    string = TRAILING_COMMAS_PATTERN.sub(
        lambda m: m.group(1) if (s := m.group(0)).startswith(",") else s, string
    )
    return loads(string)


def try_loads(string: str, *, with_comments: bool = False) -> JSONType:
    """Parse a JSON value, returning `None` for invalid input or explicit null.

    Args:
        string: JSON document to parse.
        with_comments: Whether to allow comments and trailing commas.

    Returns:
        Parsed value, or `None` if parsing fails.
    """

    try:
        return loads_jsonc(string) if with_comments else loads(string)
    except orjson.JSONDecodeError:
        logger.debug(
            f"Failed to deserialize the JSON string: {Colors.RED}%s{Colors.END}", string
        )
        return None


def escape(string: str) -> str:
    """Escape special characters in a string.

    Args:
        string: String to escape.

    Returns:
        Escaped string.
    """
    return dumps(string).decode(ENCODING)[1:-1]


def pretty(obj: Any) -> str:
    """Serialize an object to a pretty-printed JSON string.

    Args:
        obj: Object to serialize.

    Returns:
        Pretty-printed JSON string.
    """
    return dumps(obj, option=orjson.OPT_INDENT_2).decode(ENCODING)
