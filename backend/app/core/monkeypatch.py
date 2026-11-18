import os
import ssl
from typing import cast

import sanic.mixins.startup as startup
from sanic import Sanic
from sanic.constants import LocalCertCreator
from sanic.http.tls.creators import CertCreator
from sanic.worker.manager import WorkerManager


def apply_monkey_patches():
    """Apply runtime monkey patches required by the backend."""

    # allow longer startup time
    WorkerManager.THRESHOLD = 600  # type: ignore

    # allow self-signed TLS in PROD mode
    startup.get_ssl_context = _patched_get_ssl_context  # type: ignore

    # preserve SQLite rows and indexes during migrations
    _patch_tortoise_sqlite_migrations()


def _patched_get_ssl_context(app: Sanic, ssl: ssl.SSLContext | None) -> ssl.SSLContext:
    """Patched version of get_ssl_context that allows self-signed TLS in PROD mode.

    Args:
        app: The Sanic application instance.
        ssl: An optional SSLContext. If provided, it will be used directly.

    Returns:
        The SSLContext to be used for TLS connections.
    """
    if ssl:
        return ssl

    creator = CertCreator.select(
        app,
        cast(LocalCertCreator, app.config.LOCAL_CERT_CREATOR),
        app.config.LOCAL_TLS_KEY,
        app.config.LOCAL_TLS_CERT,
    )
    # prefer TLS_HOSTNAME env variable over app config
    hostname = os.environ.get("TLS_HOSTNAME") or app.config.LOCALHOST
    context = creator.generate_cert(hostname)
    return context


def _patch_tortoise_sqlite_migrations():
    """Avoid SQLite table rebuilds for descriptions and column removal."""
    from tortoise.migrations.schema_editor.base import BaseSchemaEditor
    from tortoise.migrations.schema_editor.sqlite import SqliteSchemaEditor

    # native DROP COLUMN preserves self-referencing rows and existing indexes
    SqliteSchemaEditor.remove_field = BaseSchemaEditor.remove_field
    original = SqliteSchemaEditor._alter_field

    async def _alter_field(self, model, old_field, new_field):
        """Skip schema changes that only update field descriptions.

        Args:
            model: Model whose schema is being changed.
            old_field: Existing field definition.
            new_field: Replacement field definition.
        """
        old_definition = old_field.describe(serializable=True)
        new_definition = new_field.describe(serializable=True)
        old_definition.pop("description", None)
        new_definition.pop("description", None)
        if old_definition == new_definition:
            return
        await original(self, model, old_field, new_field)

    SqliteSchemaEditor._alter_field = _alter_field
