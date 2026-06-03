#!/usr/bin/env python3
"""Bump the semantic version of both frontend and backend projects."""

import re
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent
ROOT_DIR = SCRIPTS_DIR.parent
PYPROJECT = ROOT_DIR / "backend" / "pyproject.toml"
PACKAGE_JSON = ROOT_DIR / "frontend" / "package.json"


def parse_version(text: str) -> tuple[int, int, int]:
    """Extract semver tuple from pyproject.toml content."""
    m = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text, re.MULTILINE)
    if not m:
        raise ValueError("version not found in pyproject.toml")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(major: int, minor: int, patch: int, part: str) -> tuple[int, int, int]:
    """Increment the specified version part, resetting lower parts."""
    if part == "major":
        return major + 1, 0, 0
    if part == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def update_toml(path: Path, version: str):
    """Replace version in a TOML file: version = "x.y.z" """
    content = path.read_text(encoding="utf-8")
    updated = re.sub(
        r'(^version\s*=\s*")[^"]+(")',
        lambda m: f"{m.group(1)}{version}{m.group(2)}",
        content,
        count=1,
        flags=re.MULTILINE,
    )
    path.write_text(updated, encoding="utf-8")


def update_json(path: Path, version: str):
    """Replace version in a JSON file: "version": "x.y.z" """
    content = path.read_text(encoding="utf-8")
    updated = re.sub(
        r'("version"\s*:\s*")[^"]+(")',
        lambda m: f"{m.group(1)}{version}{m.group(2)}",
        content,
        count=1,
    )
    path.write_text(updated, encoding="utf-8")


def choose_part() -> str:
    """Interactively ask the user which version part to bump."""
    print("Which version part to bump?")
    print("  1) major")
    print("  2) minor")
    print("  3) patch")
    choice = input("Enter choice (1/2/3) [3]: ").strip()
    return {"1": "major", "2": "minor"}.get(choice, "patch")


def main():
    # read current version from pyproject.toml
    toml_content = PYPROJECT.read_text(encoding="utf-8")
    major, minor, patch = parse_version(toml_content)
    old_ver = f"{major}.{minor}.{patch}"

    # determine bump part from CLI arg or interactive prompt
    if len(sys.argv) > 1 and sys.argv[1] in ("major", "minor", "patch"):
        part = sys.argv[1]
    else:
        part = choose_part()

    new_major, new_minor, new_patch = bump(major, minor, patch, part)
    new_ver = f"{new_major}.{new_minor}.{new_patch}"

    print("------------------------------")
    print(f"Bumping {part}: {old_ver} -> {new_ver}")

    # update backend pyproject.toml
    update_toml(PYPROJECT, new_ver)
    print(f"  Updated {PYPROJECT.relative_to(ROOT_DIR)}")

    # update frontend package.json
    update_json(PACKAGE_JSON, new_ver)
    print(f"  Updated {PACKAGE_JSON.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
