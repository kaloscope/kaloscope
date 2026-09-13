"""Unit tests for the dictionary utility."""

import pytest

from app.utils.dict import TrackableDict


def test_setdefault():
    data = TrackableDict[str, int]({"count": 1})

    value: int = data.setdefault("count", 2)
    assert value == 1
    assert data.setdefault("limit", default=3) == 3
    assert data == {"count": 1, "limit": 3}
    assert data.is_modified()


def test_setdefault_none():
    data = TrackableDict[str, int | None]()

    assert data.setdefault("value") is None
    assert data == {"value": None}
    assert data.is_modified()


def test_pop():
    data = TrackableDict[str, int]({"count": 1})

    value: int = data.pop("count")
    assert value == 1
    assert data == {}
    assert data.is_modified()
    with pytest.raises(KeyError):
        data.pop("missing")


@pytest.mark.parametrize("default", [None, 0, "missing"])
def test_pop_default(default: int | str | None):
    data = TrackableDict[str, int]({"count": 1})

    assert data.pop("count", default) == 1
    assert data.pop("missing", default=default) == default
    assert data == {}
    assert data.is_modified()
