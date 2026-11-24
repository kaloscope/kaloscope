"""Unit tests for URL query utilities."""

import pytest

from app.utils.url import query_param

URL = "https://example.com/video"
CASES = {
    "append_without_query": (URL, "lang=zh-CN", f"{URL}?lang=zh-CN"),
    "append_with_query": (f"{URL}?page=1", "lang=zh-CN", f"{URL}?page=1&lang=zh-CN"),
    "skip_existing_key": (f"{URL}?lang=en", "lang=zh-CN", f"{URL}?lang=en"),
    "keep_fragment": (f"{URL}#player", "lang=zh-CN", f"{URL}?lang=zh-CN#player"),
    "empty_url": ("", "lang=zh-CN", ""),
    "empty_param": (URL, "", URL),
    "invalid_param": (URL, "lang", URL),
    "empty_key": (URL, "=zh-CN", URL),
}


@pytest.mark.parametrize(("url", "param", "expected"), CASES.values(), ids=CASES)
def test_query_param(url, param, expected):
    assert query_param(url, param) == expected
