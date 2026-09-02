import asyncio

from tortoise import Tortoise, connections
from tortoise.migrations.api import migrate, plan
from tortoise.migrations.schema_editor.sqlite import SqliteSchemaEditor
from tortoise.timezone import now

from app.core.monkeypatch import _patch_tortoise_sqlite_descriptions
from app.models.download import (
    Downloader,
    DownloadState,
    DownloadTask,
    OfflineDownloadJob,
)


def test_upgrade_from_004(tmp_path):
    config = {
        "connections": {"default": f"sqlite://{tmp_path / 'migration.sqlite3'}"},
        "apps": {
            "models": {
                "models": ["app.models"],
                "default_connection": "default",
                "migrations": "app.migrations",
            }
        },
        "use_tz": True,
        "timezone": "UTC",
    }

    async def upgrade(target=None):
        await migrate(config=config, target=target)
        await Tortoise.close_connections()
        await Tortoise.init(config=config)

    async def task_indexes():
        return await connections.get("default").execute_query_dict(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'index' AND tbl_name = 'download_task' ORDER BY name"
        )

    async def run():
        try:
            await upgrade("models.0004_auto_20260728_1628")
            indexes_before = await task_indexes()
            downloader = await Downloader.create(
                config="test", name="migration", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(tmp_path),
                name="movie.mkv",
                state=DownloadState.COMPLETED,
            )
            await upgrade()
            assert await task_indexes() == indexes_before
            task = await DownloadTask.get(id=task.id)
            assert task.name == "movie.mkv"
            assert task.state == DownloadState.COMPLETED
            assert task.downloader_id == downloader.id
            job = await OfflineDownloadJob.create(
                download_id=task.id,
                job_uuid="1" * 32,
                source_fingerprint="a" * 64,
                remote_dir=f"/Kaloscope/{'1' * 32}",
            )
            assert job.source_fingerprint == "a" * 64
            assert job.completion_due_at is None
            assert job.delete_due_at is None
            assert job.delete_local is False
            await OfflineDownloadJob.filter(id=job.id).update(
                completion_due_at=now(), delete_due_at=now(), delete_local=True
            )

            await upgrade()
            job = await OfflineDownloadJob.get(download_id=task.id)
            assert job.completion_due_at is not None
            assert job.delete_due_at is not None
            assert job.delete_local is True
            connection = connections.get("default")
            indexes = await connection.execute_query_dict(
                "PRAGMA index_list(offline_download_job)"
            )
            indexed_columns = []
            for index in indexes:
                columns = await connection.execute_query_dict(
                    f'PRAGMA index_info("{index["name"]}")'
                )
                indexed_columns.extend(column["name"] for column in columns)
            assert "next_poll_at" in indexed_columns
            assert "completion_due_at" not in indexed_columns
            assert "delete_due_at" in indexed_columns
            assert not any(line.startswith("+") for line in await plan(config=config))

            await upgrade("models.0004_auto_20260728_1628")
            assert await task_indexes() == indexes_before
            assert (await DownloadTask.get(id=task.id)).name == "movie.mkv"
            assert not await connections.get("default").execute_query_dict(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'offline_download_job'"
            )
        finally:
            await Tortoise.close_connections()

    original = SqliteSchemaEditor._alter_field
    try:
        _patch_tortoise_sqlite_descriptions()
        asyncio.run(run())
    finally:
        SqliteSchemaEditor._alter_field = original
