"""Unit tests for independent media and NFO module imports."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "app.services.media",
        "app.core.media.shelver",
        "app.core.flow.nodes.nfo",
        "app.core.media.handlers.reading",
        "app.core.media.common",
        "app.core.media.text",
        "app.core.media.image",
        "app.core.media.archive",
        "app.core.media.epub",
        "app.core.media.xhtml",
    ],
)
def test_module(module):
    excluded = {
        "app.core.media.common": (
            "app.core.media.handlers",
            "app.core.media.text",
            "app.core.media.image",
            "app.core.media.archive",
            "app.core.media.epub",
            "app.core.media.xhtml",
        ),
        "app.core.media.archive": (
            "app.core.media.handlers",
            "app.core.media.text",
            "app.core.media.image",
            "app.core.media.epub",
            "app.core.media.xhtml",
        ),
        "app.core.media.epub": (
            "app.core.media.handlers",
            "app.core.media.text",
            "app.core.media.image",
            "app.core.media.xhtml",
        ),
        "app.core.media.xhtml": (
            "app.core.media.handlers",
            "app.core.media.text",
            "app.core.media.image",
        ),
        "app.core.media.text": ("app.core.media.handlers", "app.core.media.image"),
        "app.core.media.image": ("app.core.media.text",),
    }.get(module, ())
    code = (
        "import sys\n"
        f"import {module}\n"
        f"unexpected = set({excluded!r}).intersection(sys.modules)\n"
        "assert not unexpected, sorted(unexpected)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
