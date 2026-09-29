"""Unit tests for media library migrations."""

import asyncio

from tortoise import Tortoise
from tortoise.migrations.executor import MigrationExecutor, MigrationTarget

from app.models.media import MediaLib


def test_startup_scan(tmp_path):
    apps = {
        "models": {
            "models": ["app.models"],
            "default_connection": "default",
            "migrations": "app.migrations",
        }
    }
    previous = [MigrationTarget("models", "0005_auto_20260905_0751")]
    current = [MigrationTarget("models", "0006_auto_20260929_1745")]
    added_columns = {
        "media_lib": {"rename_template", "scan_on_startup"},
        "media_event": {"payload"},
        "download_task": {"transfer_pending", "transfer_targets"},
    }

    async def run():
        await Tortoise.init(
            config={
                "connections": {"default": f"sqlite://{tmp_path / 'migration.db'}"},
                "apps": apps,
            }
        )
        try:
            connection = Tortoise.get_connection("default")
            executor = MigrationExecutor(connection, apps)
            await executor.migrate(previous)
            await connection.execute_query(
                "INSERT INTO media_lib (lib_type, dir, name, priority, danmaku_ttl) "
                "VALUES ('movie', ?, 'Library', 1, 24)",
                [str(tmp_path)],
            )

            await executor.migrate(current)

            for table, added in added_columns.items():
                columns = await connection.execute_query_dict(
                    f"PRAGMA table_info({table})"
                )
                assert added <= {column["name"] for column in columns}
            library = await MediaLib.get(name="Library")
            assert library.scan_on_startup is True

            await MediaLib.filter(id=library.id).update(scan_on_startup=False)
            await executor.migrate(current)

            await library.refresh_from_db()
            assert library.scan_on_startup is False
            assert await MediaLib.all().count() == 1

            await executor.migrate(previous)

            for table, added in added_columns.items():
                columns = await connection.execute_query_dict(
                    f"PRAGMA table_info({table})"
                )
                assert not (added & {column["name"] for column in columns})
            assert await connection.execute_query_dict(
                "SELECT name FROM media_lib"
            ) == [{"name": "Library"}]

            await executor.migrate(current)

            assert (await MediaLib.get(id=library.id)).scan_on_startup is True
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
