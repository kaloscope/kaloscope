"""Unit tests for the Chinese numeral converter utility."""

import pytest

from app.utils.numeral import cn_to_int


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("零", 0),
        ("一", 1),
        ("九", 9),
        ("十", 10),
        ("十二", 12),
        ("二十", 20),
        ("二十一", 21),
        ("九十九", 99),
        ("一百", 100),
        ("一百零一", 101),
        ("一百二十三", 123),
        ("九百九十九", 999),
        ("一千", 1000),
        ("一千零一", 1001),
        ("一千一百", 1100),
        ("一千二百三十四", 1234),
        ("九千九百九十九", 9999),
    ],
)
def test_conversion(value: str, expected: int):
    assert cn_to_int(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("abc", id="invalid-characters"),
        pytest.param("一2三", id="mixed-arabic"),
        pytest.param("一万一千", id="unsupported-unit"),
        pytest.param("百", id="lone-hundred"),
        pytest.param("千", id="lone-thousand"),
    ],
)
def test_invalid(value: str):
    assert cn_to_int(value) is None
