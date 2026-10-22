"""Locate canonical graph differences without exposing unbounded field values."""

from typing import Any


def changed_paths(before: Any, after: Any) -> tuple[list[str], int]:
    """Locate differing values with bounded, deterministic JSON Pointers.

    Added or removed containers are one difference at the container's path.
    Existing arrays are compared by index. Equivalent JSON numbers compare equal;
    booleans remain distinct from numbers. No pointer is shortened or approximated.

    Args:
        before: Original canonical JSON value, or `None` for an added element.
        after: Proposed canonical JSON value, or `None` for a removed element.

    Returns:
        Up to 32 pointers within 1,024 UTF-8 bytes and the exact difference count.
    """
    missing = object()
    pending = [("", before, after)]
    paths = []
    count = size = 0
    while pending:
        path, old, new = pending.pop()
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys(), reverse=True):
                escaped = key.replace("~", "~0").replace("/", "~1")
                pending.append(
                    (f"{path}/{escaped}", old.get(key, missing), new.get(key, missing))
                )
            continue
        if isinstance(old, list) and isinstance(new, list):
            for index in reversed(range(max(len(old), len(new)))):
                pending.append(
                    (
                        f"{path}/{index}",
                        old[index] if index < len(old) else missing,
                        new[index] if index < len(new) else missing,
                    )
                )
            continue
        if old == new and isinstance(old, bool) == isinstance(new, bool):
            continue
        count += 1
        length = len(path.encode())
        if len(paths) < 32 and size + length <= 1024:
            paths.append(path)
            size += length
    return paths, count
