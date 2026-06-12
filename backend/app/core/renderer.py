import base64
import datetime
import re
from functools import partial
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

import jinja2
import opencc
from jinja2.defaults import (
    BLOCK_END_STRING,
    BLOCK_START_STRING,
    VARIABLE_END_STRING,
    VARIABLE_START_STRING,
)
from jsonpath_ng.ext import parse
from lxml import etree

from app.core.constants import ENCODING
from app.utils import json
from app.utils.deep import deep_strip
from app.utils.disk import format_bytes

# create a new Jinja2 environment
# https://jinja.palletsprojects.com/en/stable/api/#jinja2.Environment
ENV = jinja2.Environment(
    block_start_string=BLOCK_START_STRING,
    block_end_string=BLOCK_END_STRING,
    variable_start_string=VARIABLE_START_STRING,
    variable_end_string=VARIABLE_END_STRING,
    trim_blocks=True,
    lstrip_blocks=True,
    finalize=lambda x: x if x is not None else "",
)
ENV.policies["json.dumps_kwargs"] = {"ensure_ascii": False, "default": json.default}

# cache for compiled expressions
JSONPATH_CACHE = {}
XPATH_CACHE = {}
REGEX_CACHE = {}

# create converters for traditional and simplified Chinese
S2T_CONVERTER = opencc.OpenCC("s2t.json")
T2S_CONVERTER = opencc.OpenCC("t2s.json")


def compile_jsonpath(expr: str) -> Any:
    """Compile a JSONPath expression.

    See https://github.com/h2non/jsonpath-ng for more details.

    Args:
        expr: The string expression to compile.

    Returns:
        The compiled JSONPath expression object.
    """
    jsonpath = JSONPATH_CACHE.get(expr)
    if jsonpath is None:
        JSONPATH_CACHE[expr] = jsonpath = parse(expr)
    return jsonpath


def compile_xpath(expr: str) -> Any:
    """Compile an XPath expression.

    See https://lxml.de/xpathxslt.html#xpath for more details.

    Args:
        expr: The string expression to compile.

    Returns:
        The compiled XPath expression object.
    """
    xpath = XPATH_CACHE.get(expr)
    if xpath is None:
        XPATH_CACHE[expr] = xpath = etree.XPath(expr)
    return xpath


def compile_regex(expr: str) -> re.Pattern[str]:
    """Compile a regular expression.

    See https://docs.python.org/3/library/re.html for more details.

    Args:
        expr: The string expression to compile.

    Returns:
        The compiled regular expression object.
    """
    pattern = REGEX_CACHE.get(expr)
    if pattern is None:
        REGEX_CACHE[expr] = pattern = re.compile(expr, re.DOTALL | re.MULTILINE)
    return pattern


def trim(obj: Any, chars: str | None = None) -> Any:
    """Strip leading and trailing characters.

    Args:
        obj: The object to strip.
        chars: The characters to strip. Defaults to whitespace.

    Returns:
        The stripped object.
    """
    return deep_strip(obj, chars=chars)


def ltrim(obj: Any, chars: str | None = None) -> Any:
    """Strip leading characters.

    Args:
        obj: The object to strip.
        chars: The characters to strip. Defaults to whitespace.

    Returns:
        The stripped object.
    """
    return deep_strip(obj, "left", chars)


def rtrim(obj: Any, chars: str | None = None) -> Any:
    """Strip trailing characters.

    Args:
        obj: The object to strip.
        chars: The characters to strip. Defaults to whitespace.

    Returns:
        The stripped object.
    """
    return deep_strip(obj, "right", chars)


def json_escape(string: str) -> str:
    """Escape a string for JSON.

    Args:
        string: The string to escape.

    Returns:
        The escaped string.
    """
    return json.escape(string)


def jsonpath_first(obj: Any, expr: str) -> Any:
    """Find first match in a JSON string or object using a JSONPath expression.

    Args:
        obj: The JSON string or object to search.
        expr: The JSONPath expression.

    Returns:
        The first matched value, or None if not found.
    """
    jsonpath = compile_jsonpath(expr)
    if isinstance(obj, str):
        obj = json.try_loads(obj, with_comments=True)
    matches = jsonpath.find(obj)
    return matches[0].value if matches else None


def jsonpath_all(obj: Any, expr: str) -> list:
    """Find all matches in a JSON string or object using a JSONPath expression.

    Args:
        obj: The JSON string or object to search.
        expr: The JSONPath expression.

    Returns:
        A list of all matched values.
    """
    jsonpath = compile_jsonpath(expr)
    if isinstance(obj, str):
        obj = json.try_loads(obj, with_comments=True)
    matches = jsonpath.find(obj)
    return [match.value for match in matches]


def _cleanup_namespaces(tree: Any) -> Any:
    """Remove XML namespaces from all element tags in place.

    Args:
        tree: The parsed lxml element tree.

    Returns:
        The cleaned-up tree.
    """
    for elem in tree.iter():
        elem.tag = etree.QName(elem).localname
    etree.cleanup_namespaces(tree)
    return tree


def _parse_xml_or_html(string: str) -> Any:
    """Parse an XML or HTML string into an lxml element tree.

    Uses XMLParser for XML content (detected by `<?xml` declaration)
    to preserve element semantics like `<link>` that are void elements
    in HTML but container elements in XML. Falls back to HTMLParser for
    HTML content.

    Args:
        string: The XML/HTML string to parse.

    Returns:
        The parsed lxml element tree.
    """
    content = string.lstrip()
    # bytes are always UTF-8 encoded; force the parser to use UTF-8 so it
    # won't be misled by <meta charset="gbk"> or <?xml encoding="gbk"?>
    if content.startswith("<?xml"):
        tree = etree.fromstring(
            string.encode(ENCODING), parser=etree.XMLParser(encoding=ENCODING)
        )
        return _cleanup_namespaces(tree)
    return etree.fromstring(
        string.encode(ENCODING), parser=etree.HTMLParser(encoding=ENCODING)
    )


def xpath_first(obj: Any, expr: str) -> Any:
    """Find first match in an XML/HTML string or object using an XPath expression.

    Args:
        obj: The XML/HTML string or object to search.
        expr: The XPath expression.

    Returns:
        The first matched value, or None if not found.
    """
    xpath = compile_xpath(expr)
    if isinstance(obj, str):
        obj = _parse_xml_or_html(obj)
    result = xpath(obj)
    if isinstance(result, list):
        return result[0] if result else None
    return result


def xpath_all(obj: Any, expr: str) -> list:
    """Find all matches in an XML/HTML string or object using an XPath expression.

    Args:
        obj: The XML/HTML string or object to search.
        expr: The XPath expression.

    Returns:
        A list of all matched values.
    """
    xpath = compile_xpath(expr)
    if isinstance(obj, str):
        obj = _parse_xml_or_html(obj)
    result = xpath(obj)
    if not isinstance(result, list):
        return [result] if result is not None else []
    return result


def regex_first(string: str, expr: str) -> Any:
    """Find first match in a string using a regular expression.

    Args:
        string: The string to search.
        expr: The regular expression.

    Returns:
        The first captured group, or None if not found.
    """
    pattern = compile_regex(expr)
    match = pattern.search(string)
    return match.group(1) if match else None


def regex_all(string: str, expr: str) -> list:
    """Find all matches in a string using a regular expression.

    Args:
        string: The string to search.
        expr: The regular expression.

    Returns:
        A list of all matched groups.
    """
    pattern = compile_regex(expr)
    return pattern.findall(string)


_FINDERS = {
    "jsonpath_first": jsonpath_first,
    "jsonpath_all": jsonpath_all,
    "xpath_first": xpath_first,
    "xpath_all": xpath_all,
    "regex_first": regex_first,
    "regex_all": regex_all,
}


def _find(
    obj: Any,
    expr: str,
    limit: int | Literal["first", "all", "auto"] = "auto",
    *,
    expr_type: Literal["jsonpath", "xpath", "regex"],
) -> Any:
    """Find values in an object using an expression.

    Args:
        obj: The object to search.
        expr: The expression to use.
        limit: The limit of results to return.
        expr_type: The type of expression.

    Returns:
        The result of the search.
    """
    auto = limit == "auto"
    slicer = slice(0, None)

    # determine the finder type based on the limit
    finder_type: Literal["first", "all"]
    if isinstance(limit, int):
        if limit < 1:
            finder_type = "first"
        else:
            finder_type = "all"
            slicer = slice(0, limit)
    elif auto:
        finder_type = "all"
    elif limit:
        finder_type = limit
    else:
        finder_type = "all"
        slicer = slice(0, 1)

    # call the appropriate finder function
    finder = _FINDERS[f"{expr_type}_{finder_type}"]
    result = finder(obj, expr)

    # return the result
    if not isinstance(result, list):
        return result
    elif auto:
        length = len(result)
        return None if length == 0 else result[0] if length == 1 else result
    else:
        return result[slicer]


def s2t(string: str) -> str:
    """Convert a string from simplified to traditional Chinese.

    Args:
        string: The simplified Chinese string.

    Returns:
        The traditional Chinese string.
    """
    return S2T_CONVERTER.convert(string)


def t2s(string: str) -> str:
    """Convert a string from traditional to simplified Chinese.

    Args:
        string: The traditional Chinese string.

    Returns:
        The simplified Chinese string.
    """
    return T2S_CONVERTER.convert(string)


def duration(
    value: float, unit: Literal["milliseconds", "seconds", "minutes"] = "milliseconds"
) -> str:
    """Convert a duration to a human-readable format.

    Args:
        value: The duration value.
        unit: The unit of the duration value.

    Returns:
        The human-readable duration string, e.g. "01:23:45".
    """
    match unit:
        case "milliseconds":
            d = datetime.timedelta(milliseconds=value)
        case "seconds":
            d = datetime.timedelta(seconds=value)
        case "minutes":
            d = datetime.timedelta(minutes=value)
    total_seconds = int(d.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return (f"{hours:02}:" if hours else "") + f"{minutes:02}:{seconds:02}"


def b64decode(s: str | bytes) -> str:
    """Decode the Base64 encoded bytes or string.

    Args:
        s: The Base64 encoded bytes or string.

    Returns:
        The decoded string.
    """
    return base64.b64decode(s).decode(ENCODING)


def b64encode(s: str | bytes) -> str:
    """Encode the bytes or string to Base64.

    Args:
        s: The bytes or string to encode.

    Returns:
        The Base64 encoded string.
    """
    if isinstance(s, str):
        s = s.encode(ENCODING)
    return base64.b64encode(s).decode(ENCODING)


def parent_path(path: Path | str, levels: int = 1, resolve: bool = False) -> str:
    """Return the parent directory of a path.

    Args:
        path: The path to get the parent of.
        levels: The number of levels to go up.
        resolve: Whether to resolve to an absolute path first.

    Returns:
        The parent directory path string.
    """
    if not isinstance(path, Path):
        path = Path(path)
    p = path.resolve() if resolve else path
    for _ in range(levels):
        p = p.parent
    return str(p)


_DATETIME_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%m/%d/%Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y",
]


def _datetime(value: Any, tz: float | None = None) -> datetime.datetime | None:
    """Convert a value to a datetime object.

    Args:
        value: The value to convert (datetime, date, int, float, or str).
        tz: The timezone offset in hours (e.g. 8 for UTC+8).

    Returns:
        The converted datetime object, or None if the value cannot be parsed.
    """
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):
        return datetime.datetime(value.year, value.month, value.day)

    tzinfo = datetime.timezone(datetime.timedelta(hours=tz)) if tz is not None else None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:  # milliseconds threshold
            ts /= 1000
        try:
            return datetime.datetime.fromtimestamp(ts, tz=tzinfo)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        value = value.strip()
        # numeric string
        try:
            return _datetime(float(value), tz)
        except ValueError:
            pass
        # try ISO 8601
        try:
            dt = datetime.datetime.fromisoformat(value)
            if dt.tzinfo is not None and tzinfo is not None:
                dt = dt.astimezone(tzinfo)
            return dt
        except ValueError:
            pass
        # try known datetime formats
        for fmt in _DATETIME_FORMATS:
            try:
                return datetime.datetime.strptime(value, fmt)
            except ValueError:
                continue
    return None


def strftime(
    value: Any, format: str = "%Y-%m-%d %H:%M:%S", tz: float | None = None
) -> str:
    """Format a datetime value as a string.

    Args:
        value: The datetime value to format.
        format: The datetime format string.
        tz: The timezone offset in hours.

    Returns:
        The formatted datetime string, or the original value if it cannot be parsed.
    """
    if value is None or isinstance(value, bool):
        return ""
    dt = _datetime(value, tz)
    if dt is not None:
        return dt.strftime(format)
    return str(value)


def year(value: Any, tz: float | None = None) -> int | None:
    """Extract the 4-digit year from a timestamp or date string.

    Args:
        value: The value to extract the year from.
        tz: The timezone offset in hours.

    Returns:
        The 4-digit year as an integer, or None if not found.
    """
    if value is None or isinstance(value, bool):
        return None
    dt = _datetime(value, tz)
    if dt is not None:
        return dt.year
    # fallback to regex search for a 4-digit year in strings
    if isinstance(value, str):
        m = re.search(r"\b(\d{4})\b", value.strip())
        if m:
            return int(m.group(1))
    return None


def _str(value: Any) -> str:
    """Convert a value to a string, treating None and bool as empty string.

    Args:
        value: The value to convert.

    Returns:
        The converted string.
    """
    if value is None or isinstance(value, bool):
        return ""
    return str(value)


def prefix(value: Any, prefix: Any, strict: bool = True) -> str:
    """Add a prefix to a string value.

    Args:
        value: The value to add the prefix to.
        prefix: The prefix to add.
        strict: Whether to return empty string if the value or prefix is falsy.

    Returns:
        The prefixed string.
    """
    if strict and (not value or not prefix):
        return ""
    return f"{_str(prefix)}{_str(value)}"


def suffix(value: Any, suffix: Any, strict: bool = True) -> str:
    """Add a suffix to a string value.

    Args:
        value: The value to add the suffix to.
        suffix: The suffix to add.
        strict: Whether to return empty string if the value or suffix is falsy.

    Returns:
        The suffixed string.
    """
    if strict and (not value or not suffix):
        return ""
    return f"{_str(value)}{_str(suffix)}"


def query_param(url: str, param: str) -> str:
    """Append a query parameter to a URL if the key is not already present.

    Args:
        url: The original URL.
        param: The query parameter in `key=value` format.

    Returns:
        The URL with the query parameter appended when applicable.
    """
    if not url or not param:
        return url

    key, separator, _ = param.partition("=")
    if not separator or not key:
        return url

    parts = urlsplit(url)
    existing_params = parse_qsl(parts.query, keep_blank_values=True)
    if any(existing_key == key for existing_key, _ in existing_params):
        return url

    query = param if not parts.query else f"{parts.query}&{param}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


# register custom filters
# https://jinja.palletsprojects.com/en/stable/api/#custom-filters
ENV.filters["trim"] = trim
ENV.filters["ltrim"] = ltrim
ENV.filters["rtrim"] = rtrim
ENV.filters["json_escape"] = json_escape
ENV.filters["jsonpath_first"] = jsonpath_first
ENV.filters["jsonpath_all"] = jsonpath_all
ENV.filters["jsonpath"] = partial(_find, expr_type="jsonpath")
ENV.filters["xpath_first"] = xpath_first
ENV.filters["xpath_all"] = xpath_all
ENV.filters["xpath"] = partial(_find, expr_type="xpath")
ENV.filters["regex_first"] = regex_first
ENV.filters["regex_all"] = regex_all
ENV.filters["regex"] = partial(_find, expr_type="regex")
ENV.filters["size"] = lambda x: format_bytes(int(float(x))) if x else ""
ENV.filters["s2t"] = s2t
ENV.filters["t2s"] = t2s
ENV.filters["duration"] = duration
ENV.filters["b64decode"] = b64decode
ENV.filters["b64encode"] = b64encode
ENV.filters["parent_path"] = parent_path
ENV.filters["strftime"] = strftime
ENV.filters["year"] = year
ENV.filters["prefix"] = prefix
ENV.filters["suffix"] = suffix
ENV.filters["query_param"] = query_param
ENV.filters["quote"] = quote


def is_file(path: Any) -> bool:
    """Check if the given path is an existing file.

    Args:
        path: The path to check, can be a string or a Path object.

    Returns:
        True if the path exists and is a file, False otherwise.
    """
    if not isinstance(path, (str, Path)):
        return False
    if isinstance(path, str):
        path = Path(path)
    return path.exists() and path.is_file()


def is_dir(path: Any) -> bool:
    """Check if the given path is an existing directory.

    Args:
        path: The path to check, can be a string or a Path object.

    Returns:
        True if the path exists and is a directory, False otherwise.
    """
    if not isinstance(path, (str, Path)):
        return False
    if isinstance(path, str):
        path = Path(path)
    return path.exists() and path.is_dir()


# register custom tests
# https://jinja.palletsprojects.com/en/stable/api/#custom-tests
ENV.tests["file"] = is_file
ENV.tests["dir"] = is_dir


def render(value: json.JSONType, context: dict, *, raw: bool = False) -> Any:
    """Render a JSON value with a context.

    Args:
        value: The value to render.
        context: The context to render with.
        raw: Whether to render the value as raw object.

    Returns:
        The rendered value.
    """
    if isinstance(value, str) and is_template(value):
        if raw and (key := raw_key(context, value)) is not None:
            return get_raw(context, key)
        template = ENV.from_string(value)
        return template.render(context)
    elif isinstance(value, list):
        return [render(v, context, raw=raw) for v in value]
    elif isinstance(value, dict):
        return {k: render(v, context, raw=raw) for k, v in value.items()}
    else:
        return value


def raw_key(dictionary: dict, string: str) -> str | None:
    """Check if a template string is a nested key in a dictionary.

    Args:
        dictionary: The dictionary to check.
        string: The template string to check.

    Returns:
        The stripped key if it is in the dictionary, None otherwise.
    """
    key = string.strip()
    if key.startswith(VARIABLE_START_STRING) and key.endswith(VARIABLE_END_STRING):
        key = key[2:-2].strip()
    # check nested keys
    keys = key.split(".")
    data = dictionary
    for k in keys:
        if isinstance(data, dict) and k in data:
            data = data[k]
        else:
            return None
    return key


def get_raw(dictionary: dict, key: str, default=None):
    """Get a nested key from a dictionary without rendering.

    Args:
        dictionary: The dictionary to get the key from.
        key: The nested key to get, separated by dots.
        default: The default value to return if the key is not found.

    Returns:
        The value of the nested key, or the default value if not found.
    """
    keys = key.split(".")
    data = dictionary
    for k in keys:
        if isinstance(data, dict) and k in data:
            data = data[k]
        else:
            return default
    return data


def is_template(string: str) -> bool:
    """Check if a string is a Jinja2 template.

    Args:
        string: The string to check.

    Returns:
        True if the string is a Jinja2 template, False otherwise.
    """
    if not string:
        return False
    if contains_symbols(string, VARIABLE_START_STRING, VARIABLE_END_STRING):
        return True
    return contains_symbols(string, BLOCK_START_STRING, BLOCK_END_STRING)


def contains_symbols(string: str, open_symbol: str, close_symbol: str) -> bool:
    """Check if a string contains a pair of symbols.

    Args:
        string: The string to check.
        open_symbol: The open symbol.
        close_symbol: The close symbol.

    Returns:
        True if the string contains the pair of symbols, False otherwise.
    """
    open_index = string.find(open_symbol)
    if open_index != -1:
        return string.find(close_symbol, open_index + len(open_symbol)) != -1
    return False
