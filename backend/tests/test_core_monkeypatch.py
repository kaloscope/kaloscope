"""Unit tests for runtime monkey patches."""

import subprocess
import sys
from pathlib import Path


def test_main_applies_patches():
    """Test that Sanic startup imports apply backend monkey patches."""
    backend_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import app.main; "
                "import sanic.mixins.startup as startup; "
                "from sanic.worker.manager import WorkerManager; "
                "from tortoise.migrations.schema_editor.base import BaseSchemaEditor; "
                "from tortoise.migrations.schema_editor.sqlite import "
                "SqliteSchemaEditor; "
                "assert SqliteSchemaEditor.remove_field is "
                "BaseSchemaEditor.remove_field; "
                "print(startup.get_ssl_context.__module__); "
                "print(WorkerManager.THRESHOLD)"
            ),
        ],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["app.core.monkeypatch", "600"]
