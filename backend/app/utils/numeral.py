"""Chinese numeral conversion utilities."""

_DIGITS: dict[str, int] = {
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_UNITS: dict[str, int] = {"十": 10, "百": 100, "千": 1000}

_ALLOWED = frozenset(_DIGITS) | frozenset(_UNITS)


def cn_to_int(string: str) -> int | None:
    """Convert a simplified Chinese numeral string (1~9999) to an integer.

    Args:
        string: The input string containing Chinese numeral characters.

    Returns:
        The integer value represented by the input string, or `None` if the string
        is not a valid Chinese numeral.
    """
    if not string:
        return None
    if not all(ch in _ALLOWED for ch in string):
        return None
    # leading 十 → implicit 一十
    if string[0] == "十":
        string = "一" + string
    result = 0
    temp = 0
    for ch in string:
        if ch in _DIGITS:
            temp = _DIGITS[ch]
        elif ch in _UNITS:
            if temp == 0:
                return None
            result += temp * _UNITS[ch]
            temp = 0
    return result + temp
