"""Unit tests for exact, bounded paths in canonical workflow comparisons."""

import pytest

from app.core.flow.diff import changed_paths


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ({"x": 1}, {"x": 1.0}, []),
        ({"x": True}, {"x": 1}, ["/x"]),
        ({}, {"x": None}, ["/x"]),
        ({"x": {}}, {"x": []}, ["/x"]),
        ({"x": [1, 2]}, {"x": [0, 2, 3]}, ["/x/0", "/x/2"]),
        ({"a/b~c": {"": 1}}, {"a/b~c": {"": 2}}, ["/a~1b~0c/"]),
        (None, {"id": "new"}, [""]),
        ({"id": "removed"}, None, [""]),
        ({"b": 0, "a": 0}, {"a": 1, "b": 1}, ["/a", "/b"]),
    ],
)
def test_paths(before, after, expected):
    assert changed_paths(before, after) == (expected, len(expected))


def test_bounds():
    before = {str(index): 0 for index in range(100)}
    after = dict.fromkeys(reversed(before), 1)

    paths, count = changed_paths(before, after)

    assert count == 100
    assert len(paths) == 32
    assert paths == [f"/{key}" for key in sorted(before)[:32]]


def test_long_paths():
    long_key = "字段" * 300
    paths, count = changed_paths({}, {long_key: None, "short": 1})

    assert count == 2
    assert paths == ["/short"]
    assert sum(len(path.encode()) for path in paths) <= 1024
