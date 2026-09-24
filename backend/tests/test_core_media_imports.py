"""Unit tests for media and NFO module imports."""

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module",
    ["app.services.media", "app.core.media.shelver", "app.core.flow.nodes.nfo"],
)
def test_module(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
