"""Unit tests for the core renderer."""

from pathlib import Path

import pytest
from jinja2 import UndefinedError

from app.core.renderer import (
    ENV,
    b64decode,
    b64encode,
    duration,
    json_escape,
    jsonpath_all,
    jsonpath_first,
    ltrim,
    parent_path,
    prefix,
    query_param,
    regex_all,
    regex_first,
    render,
    rtrim,
    s2t,
    strftime,
    suffix,
    t2s,
    trim,
    xpath_all,
    xpath_first,
    year,
)
from app.utils.json import JSONType


class TestTrim:
    def test_default(self):
        assert trim("  Alpha  ") == "Alpha"

    def test_chars(self):
        assert trim("--Alpha--", "-") == "Alpha"

    def test_nested(self):
        data = {"title": "  Alpha  ", "tags": [" x ", " y "]}
        assert trim(data) == {"title": "Alpha", "tags": ["x", "y"]}


class TestLtrim:
    def test_default(self):
        assert ltrim("  Alpha  ") == "Alpha  "

    def test_chars(self):
        assert ltrim("--Alpha--", "-") == "Alpha--"


class TestRtrim:
    def test_default(self):
        assert rtrim("  Alpha  ") == "  Alpha"

    def test_chars(self):
        assert rtrim("--Alpha--", "-") == "--Alpha"


class TestJsonEscape:
    def test_special_chars(self):
        assert json_escape('line\n"quote"') == 'line\\n\\"quote\\"'


class TestJsonPathFirst:
    def test_match(self):
        data = {"items": [{"title": "Alpha"}, {"title": "Beta"}]}
        assert jsonpath_first(data, "$.items[*].title") == "Alpha"

    def test_missing(self):
        assert jsonpath_first({"items": []}, "$.items[*].title") is None


class TestJsonPathAll:
    def test_object(self):
        data = {"items": [{"title": "Alpha"}, {"title": "Beta"}]}
        assert jsonpath_all(data, "$.items[*].title") == ["Alpha", "Beta"]

    def test_string(self):
        data = '{"items": [{"title": "Alpha"}, {"title": "Beta"}]}'
        assert jsonpath_all(data, "$.items[*].title") == ["Alpha", "Beta"]


class TestJsonPath:
    def test_auto_none(self):
        data = {"items": [{"title": "Alpha"}]}
        assert ENV.filters["jsonpath"](data, "$.items[*].year") is None

    def test_auto_one(self):
        data = {"items": [{"title": "Alpha"}]}
        assert ENV.filters["jsonpath"](data, "$.items[*].title") == "Alpha"

    def test_auto_many(self):
        data = {"items": [{"title": "Alpha"}, {"title": "Beta"}]}
        assert ENV.filters["jsonpath"](data, "$.items[*].title") == [
            "Alpha",
            "Beta",
        ]

    def test_first(self):
        data = {"items": [{"title": "Alpha"}, {"title": "Beta"}]}
        assert ENV.filters["jsonpath"](data, "$.items[*].title", "first") == "Alpha"

    def test_all(self):
        data = {"items": [{"title": "Alpha"}, {"title": "Beta"}]}
        assert ENV.filters["jsonpath"](data, "$.items[*].title", "all") == [
            "Alpha",
            "Beta",
        ]

    def test_limit(self):
        data = {"items": [{"title": "Alpha"}, {"title": "Beta"}]}
        assert ENV.filters["jsonpath"](data, "$.items[*].title", 1) == ["Alpha"]


class TestXPathFirst:
    def test_match(self):
        html = '<html><body><a href="/one">One</a><a href="/two">Two</a></body></html>'
        assert xpath_first(html, "//a/text()") == "One"

    def test_missing(self):
        assert xpath_first("<html><body></body></html>", "//a/text()") is None


class TestXPathAll:
    def test_match(self):
        html = '<html><body><a href="/one">One</a><a href="/two">Two</a></body></html>'
        assert xpath_all(html, "//a/text()") == ["One", "Two"]

    def test_scalar(self):
        html = "<html><body><h1>Title</h1></body></html>"
        assert xpath_all(html, "string(//h1)") == ["Title"]


class TestXPath:
    def test_auto_scalar(self):
        html = "<html><body><h1>Title</h1></body></html>"
        assert ENV.filters["xpath"](html, "string(//h1)") == "Title"

    def test_auto_many(self):
        html = '<html><body><a href="/one">One</a><a href="/two">Two</a></body></html>'
        assert ENV.filters["xpath"](html, "//a/text()") == ["One", "Two"]

    def test_first(self):
        html = '<html><body><a href="/one">One</a><a href="/two">Two</a></body></html>'
        assert ENV.filters["xpath"](html, "//a/text()", "first") == "One"

    def test_limit(self):
        html = '<html><body><a href="/one">One</a><a href="/two">Two</a></body></html>'
        assert ENV.filters["xpath"](html, "//a/text()", 1) == ["One"]


class TestRegexFirst:
    def test_match(self):
        assert regex_first("S01E02 S01E03", r"E(\d+)") == "02"

    def test_missing(self):
        assert regex_first("S01", r"E(\d+)") is None


class TestRegexAll:
    def test_match(self):
        assert regex_all("S01E02 S01E03", r"E(\d+)") == ["02", "03"]

    def test_missing(self):
        assert regex_all("S01", r"E(\d+)") == []


class TestRegex:
    def test_auto_none(self):
        assert ENV.filters["regex"]("S01", r"E(\d+)") is None

    def test_auto_one(self):
        assert ENV.filters["regex"]("S01E02", r"E(\d+)") == "02"

    def test_auto_many(self):
        assert ENV.filters["regex"]("S01E02 S01E03", r"E(\d+)") == ["02", "03"]

    def test_first(self):
        assert ENV.filters["regex"]("S01E02 S01E03", r"E(\d+)", "first") == "02"

    def test_all(self):
        assert ENV.filters["regex"]("S01E02 S01E03", r"E(\d+)", "all") == [
            "02",
            "03",
        ]

    def test_limit(self):
        assert ENV.filters["regex"]("S01E02 S01E03 S01E04", r"E(\d+)", 2) == [
            "02",
            "03",
        ]


class TestSize:
    def test_number(self):
        assert ENV.filters["size"](1536) == "1.5 KB"

    def test_string(self):
        assert ENV.filters["size"]("2048") == "2 KB"

    def test_empty(self):
        assert ENV.filters["size"]("") == ""


class TestChinese:
    def test_s2t(self):
        assert s2t("简体中文") == "簡體中文"

    def test_t2s(self):
        assert t2s("繁體中文") == "繁体中文"


class TestDuration:
    def test_default(self):
        assert duration(3723000) == "01:02:03"

    def test_seconds(self):
        assert duration(125, "seconds") == "02:05"

    def test_minutes(self):
        assert duration(2, "minutes") == "02:00"


class TestBase64:
    def test_encode_string(self):
        assert b64encode("你好") == "5L2g5aW9"

    def test_encode_bytes(self):
        assert b64encode(b"hello") == "aGVsbG8="

    def test_decode_string(self):
        assert b64decode("5L2g5aW9") == "你好"

    def test_decode_bytes(self):
        assert b64decode(b"aGVsbG8=") == "hello"


class TestParentPath:
    def test_default(self):
        assert parent_path("library/movies/file.mkv") == "library/movies"

    def test_levels(self):
        assert parent_path("library/movies/Movie/file.mkv", 2) == "library/movies"

    def test_path(self):
        assert parent_path(Path("library/movies/file.mkv")) == "library/movies"

    def test_resolve(self, tmp_path):
        path = tmp_path / "library" / "movies" / "file.mkv"
        assert parent_path(path, resolve=True) == str(path.resolve().parent)


class TestStrftime:
    def test_default(self):
        assert strftime("2026-07-04") == "2026-07-04 00:00:00"

    def test_format(self):
        assert strftime("2026-07-04", "%Y/%m/%d") == "2026/07/04"

    def test_timezone(self):
        assert strftime(0, "%Y-%m-%d %H:%M:%S", tz=0) == "1970-01-01 00:00:00"

    def test_invalid(self):
        assert strftime("not a date") == "not a date"


class TestYear:
    def test_date(self):
        assert year("2026-07-04") == 2026

    def test_text(self):
        assert year("Released in 1999 HD") == 1999

    def test_timestamp(self):
        assert year(0, tz=0) == 1970

    def test_invalid(self):
        assert year("unknown") is None


class TestPrefix:
    def test_default(self):
        assert prefix("Episode", "S01 ") == "S01 Episode"

    def test_empty(self):
        assert prefix("", "S01 ") == ""

    def test_loose(self):
        assert prefix("", "S01 ", strict=False) == "S01 "


class TestSuffix:
    def test_default(self):
        assert suffix("Movie", ".mkv") == "Movie.mkv"

    def test_empty(self):
        assert suffix("Movie", "") == ""

    def test_loose(self):
        assert suffix("", ".mkv", strict=False) == ".mkv"


class TestQuote:
    def test_default(self):
        assert ENV.filters["quote"]("a b/中文") == "a%20b/%E4%B8%AD%E6%96%87"

    def test_safe(self):
        assert ENV.filters["quote"]("a/b", safe="") == "a%2Fb"


class TestQueryParam:
    def test_append_without_query(self):
        assert query_param("https://example.com/video", "lang=zh-CN") == (
            "https://example.com/video?lang=zh-CN"
        )

    def test_append_with_query(self):
        assert query_param("https://example.com/video?page=1", "lang=zh-CN") == (
            "https://example.com/video?page=1&lang=zh-CN"
        )

    def test_skip_existing_key(self):
        assert query_param("https://example.com/video?lang=en", "lang=zh-CN") == (
            "https://example.com/video?lang=en"
        )

    def test_keep_fragment(self):
        assert query_param("https://example.com/video#player", "lang=zh-CN") == (
            "https://example.com/video?lang=zh-CN#player"
        )

    def test_empty_url(self):
        assert query_param("", "lang=zh-CN") == ""

    def test_empty_param(self):
        assert (
            query_param("https://example.com/video", "") == "https://example.com/video"
        )

    def test_invalid_param(self):
        assert query_param("https://example.com/video", "lang") == (
            "https://example.com/video"
        )

    def test_empty_key(self):
        assert query_param("https://example.com/video", "=zh-CN") == (
            "https://example.com/video"
        )

    def test_render_uses_filter(self):
        result = render(
            "{{ url | query_param('lang=zh-CN') }}",
            {"url": "https://example.com/video?page=1"},
        )
        assert result == "https://example.com/video?page=1&lang=zh-CN"


class TestRender:
    def test_raw(self):
        values = {"ids": ["one", "two"], "paused": False}
        result = render(
            {
                "ids": "{{ ids }}",
                "paused": "{{ paused }}",
                "count": "{{ ids|length }}",
                "nested": ["{{ ids[0] }}", "{{ none }}"],
            },
            values,
            raw=True,
        )
        assert result == {
            "ids": ["one", "two"],
            "paused": False,
            "count": 2,
            "nested": ["one", None],
        }

    def test_text(self):
        assert render("001", {}, raw=True) == "001"
        assert render("{{ none }}", {}) == ""
        assert (
            render("{% for id in ids %}{{ id }}{% endfor %}", {"ids": [1, 2]}, raw=True)
            == "12"
        )
        for name, id in (("task", 1), ("other", 2)):
            assert (
                render("{{name}}/{{id}}", {"name": name, "id": id}, raw=True)
                == f"{name}/{id}"
            )

    @pytest.mark.parametrize("raw", [False, True])
    def test_strict(self, raw):
        template: JSONType = {"items": ["{{ calendar }}"]}
        assert render(template, {}, raw=raw) == {"items": [""]}
        with pytest.raises(UndefinedError):
            render(template, {}, raw=raw, strict=True)
        assert render(template, {}, raw=raw) == {"items": [""]}
        assert render("{{ calendar|default([]) }}", {}, raw=raw, strict=True) == (
            [] if raw else "[]"
        )
        assert render("{{ calendar }}", {"calendar": None}, raw=raw, strict=True) == (
            None if raw else ""
        )

    def test_cache(self):
        for count in (1, 2):
            context = {"count": count}
            assert render("{{ count + 1 }}", context) == str(count + 1)
            assert render("{{ count + 1 }}", context, raw=True) == count + 1

    @pytest.mark.parametrize("strict", [False, True])
    @pytest.mark.parametrize(
        "template, expected",
        [
            ("{{- count + 1 }}", 3),
            ("{{ count + 1 -}}", 3),
            ("{{- -count -}}", -2),
            ("{{- data.items -}}", ["one"]),
            ("{{+ data.items }}", ["one"]),
            ("{{+-count}}", -2),
        ],
    )
    def test_whitespace(self, strict, template, expected):
        context = {"count": 2, "data": {"items": ["one"]}}
        assert render(template, context, raw=True, strict=strict) == expected

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize("kind", ["file", "dir"])
    def test_file_state(self, tmp_path, raw, kind):
        path = tmp_path / "entry"
        template = "{{ " + repr(str(path)) + " is " + kind + " }}"
        for strict in (False, True):
            assert render(template, {}, raw=raw, strict=strict) == (
                False if raw else "False"
            )
        path.touch() if kind == "file" else path.mkdir()
        for strict in (False, True):
            assert render(template, {}, raw=raw, strict=strict) == (
                True if raw else "True"
            )
        path.unlink() if kind == "file" else path.rmdir()
        for strict in (False, True):
            assert render(template, {}, raw=raw, strict=strict) == (
                False if raw else "False"
            )

    def test_resolved_path(self, tmp_path):
        first, second = tmp_path / "first" / "child", tmp_path / "second" / "child"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        link = tmp_path / "link"
        template = "{{ " + repr(str(link)) + "|parent_path(resolve=true) }}"
        for target in (first, second):
            link.symlink_to(target)
            assert render(template, {}) == str(target.resolve().parent)
            link.unlink()

    @pytest.mark.parametrize("raw", [False, True])
    @pytest.mark.parametrize(
        "template",
        [
            "{{ [missing] }}",
            '{{ {"id": missing} }}',
            '{{ {"items": [1, {"id": missing}]} }}',
            "{{ (missing,) }}",
        ],
    )
    def test_nested_undefined(self, raw, template):
        with pytest.raises(UndefinedError, match="missing"):
            render(template, {}, raw=raw, strict=True)

    def test_nested_values(self):
        value = {"items": [None, False, 0, "{{ missing }}"], "file": ("name", b"data")}
        assert render("{{ data }}", {"data": value}, raw=True, strict=True) is value

    @pytest.mark.parametrize("raw", [False, True])
    def test_json_undefined(self, raw):
        template = '{{ {"id": missing}|tojson }}'
        with pytest.raises(UndefinedError, match="missing"):
            render(template, {}, raw=raw, strict=True)
        assert render(template, {}, raw=raw) == '{"id": null}'

    def test_json_values(self, tmp_path):
        template = "{{ data|tojson }}"
        context = {"data": {"path": tmp_path, "items": [None, False, 0, "中文"]}}
        assert render(template, context, strict=True) == render(template, context)


class TestRenderFilters:
    def test_no_args(self):
        assert render("{{ title | trim }}", {"title": "  Alpha  "}) == "Alpha"

    def test_args(self):
        result = render("{{ url | query_param('lang=zh-CN') }}", {"url": "https://x"})
        assert result == "https://x?lang=zh-CN"

    def test_kwargs(self):
        assert render("{{ value | duration(unit='minutes') }}", {"value": 2}) == "02:00"
