"""Upgrade existing SQLite media and download data without resetting identities."""

import asyncio

from tortoise import Tortoise
from tortoise.migrations.executor import MigrationExecutor, MigrationTarget

from app.models.download import DownloadTask, OfflineDownloadJob
from app.models.media import MediaEvent, MediaLib


def test_upgrade_data():
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        connection = Tortoise.get_connection("default")
        executor = MigrationExecutor(
            connection,
            {
                "models": {
                    "models": ["app.models"],
                    "default_connection": "default",
                    "migrations": "app.migrations",
                }
            },
        )
        try:
            await executor.migrate(
                [MigrationTarget("models", "0005_auto_20260905_0751")]
            )
            await connection.execute_script(
                """
                INSERT INTO media_lib
                    (id, lib_type, dir, name, priority, language, danmaku_ttl)
                VALUES (7, 'movie', '/library', 'Existing library', 1, 'zh-CN', 48);
                INSERT INTO media_event
                    (id, lib_id, src_path, dest_path, event_type, is_directory)
                VALUES (11, 7, '/library/old.mkv', '/library/movie.mkv', 'moved', 0);
                INSERT INTO media_item
                    (id, lib_id, dir, path, name, visible, title, hash, nfo_path)
                VALUES (13, 7, '/library', '/library/movie.mkv', 'movie', 1,
                        'Existing title', '0123456789abcdef0123456789abcdef',
                        '/library/movie.nfo');
                INSERT INTO downloader (id, config, name, priority)
                VALUES (3, 'existing config', 'Existing downloader', 1);
                INSERT INTO download_task
                    (id, downloader_id, dir, name, state, files,
                     transfer_lib_id, transfer_method, unique_id)
                VALUES (17, 3, '/downloads', 'movie', 'completed', '["movie.mkv"]',
                        7, 'hardlink', 'existing-remote-id');
                INSERT INTO offline_download_job
                    (id, download_id, job_uuid, source_fingerprint, remote_dir,
                     manifest, delete_local, unchanged_count, retry_count)
                VALUES (19, 17, '11111111111111111111111111111111',
                        'existing-fingerprint', '/remote/movie',
                        '[{"path":"movie.mkv","is_dir":false,"size":42}]',
                        0, 2, 1);
                """
            )
            new_fields = {
                "media_lib": "rename_template",
                "media_event": "payload",
                "download_task": "transfer_targets",
            }
            tables = [*new_fields, "media_item", "downloader", "offline_download_job"]
            before = {
                table: await connection.execute_query_dict(
                    f'SELECT * FROM "{table}" ORDER BY id'
                )
                for table in tables
            }
            for table, column in new_fields.items():
                assert column not in before[table][0]

            target = MigrationTarget("models", "0006_media_organization")
            await executor.migrate([target])

            for table in tables:
                rows = await connection.execute_query_dict(
                    f'SELECT * FROM "{table}" ORDER BY id'
                )
                if column := new_fields.get(table):
                    assert rows[0].pop(column) is None
                assert rows == before[table]
            assert await connection.execute_query_dict("PRAGMA foreign_key_check") == []
            assert await executor.plan([target]) == []

            # current models can read migrated rows and their new optional fields
            library = await MediaLib.get(id=7)
            event = await MediaEvent.get(id=11)
            task = await DownloadTask.get(id=17)
            job = await OfflineDownloadJob.get(id=19)
            assert library.rename_template is None
            assert event.payload is None
            assert event.lib_id == library.id
            assert task.transfer_targets is None
            assert task.files == ["movie.mkv"]
            assert task.transfer_lib_id == library.id
            assert job.download_id == task.id
            assert job.manifest == [{"path": "movie.mkv", "is_dir": False, "size": 42}]
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
