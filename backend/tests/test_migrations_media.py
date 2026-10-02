"""Unit tests for media library migrations."""

import asyncio

import pytest
from tortoise import Tortoise
from tortoise.exceptions import IntegrityError
from tortoise.migrations.autodetector import MigrationAutodetector
from tortoise.migrations.executor import MigrationExecutor, MigrationTarget
from tortoise.migrations.schema_editor.sqlite import SqliteSchemaEditor

from app.core.monkeypatch import _patch_tortoise_sqlite_migrations
from app.models.media import MediaItem, MediaLib
from app.models.user import HistoryType, UserHistory


@pytest.fixture(autouse=True)
def sqlite_migrations(monkeypatch):
    """Use the application's SQLite compatibility patches in isolated tests.

    Args:
        monkeypatch: The fixture restoring schema editor methods after each test.
    """
    for name in ("_alter_field", "remove_field"):
        monkeypatch.setattr(SqliteSchemaEditor, name, getattr(SqliteSchemaEditor, name))
    _patch_tortoise_sqlite_migrations()


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


def test_content_index(tmp_path):
    apps = {
        "models": {
            "models": ["app.models"],
            "default_connection": "default",
            "migrations": "app.migrations",
        }
    }
    previous = [MigrationTarget("models", "0006_auto_20260929_1745")]
    current = [MigrationTarget("models", "0007_auto_20261002_2108")]
    added = {
        "media_item": {
            "format",
            "extra",
            "index_version",
            "index_state",
            "index_error",
        },
        "user_history": {"locator"},
    }
    index_query = (
        "SELECT i.name FROM pragma_index_list('media_event') AS i "
        "WHERE (SELECT group_concat(name, ',') FROM "
        "(SELECT name FROM pragma_index_info(i.name) ORDER BY seqno)) "
        "= 'lib_id,event_type'"
    )

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
            await connection.execute_many(
                "INSERT INTO media_lib "
                "(id, lib_type, dir, name, priority, danmaku_ttl, scan_on_startup) "
                "VALUES (?, ?, ?, ?, ?, 24, 0)",
                [(1, "movie", "/movies", "Movies", 1), (2, "tv_show", "/tv", "TV", 2)],
            )
            await connection.execute_many(
                "INSERT INTO media_item "
                "(id, lib_id, parent_id, dir, path, name, title, visible, nfo_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        1,
                        1,
                        None,
                        "/movies",
                        "/movies/a.mkv",
                        "a",
                        "Movie",
                        1,
                        "/movies/a.nfo",
                    ),
                    (2, 2, None, "/tv/a", "/tv/a", "a", "Show", 1, "/tv/a/tvshow.nfo"),
                    (3, 2, 2, "/tv/a", "/tv/a/1.mkv", "1", "Episode", 0, "/tv/a/1.nfo"),
                ],
            )
            await connection.execute_many(
                "INSERT INTO media_event "
                "(id, lib_id, src_path, event_type, is_directory, payload) "
                "VALUES (?, 1, '/movies/a.mkv', ?, 0, ?)",
                [
                    (1, "created", None),
                    (2, "ingest", '{"boot_params":[],"organize_ids":[1]}'),
                    (3, "organize", '{"state":"pending"}'),
                ],
            )
            await connection.execute_query(
                "INSERT INTO user (id, username, password, role, preferences) "
                "VALUES (1, 'reader', 'test', 'user', ?)",
                ['{"watch_records":3,"search_records":0}'],
            )
            await connection.execute_many(
                "INSERT INTO user_history "
                "(id, user_id, rel_type, rel_id, position, percentage, keyword, "
                "repetitions) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "video", 3, 123, 42, None, 2),
                    (2, "search", 0, None, None, "Example", 4),
                ],
            )
            snapshots = {
                table: await connection.execute_query_dict(
                    f"SELECT * FROM {table} ORDER BY id"
                )
                for table in (
                    "media_lib",
                    "media_item",
                    "media_event",
                    "user",
                    "user_history",
                )
            }
            item_indexes_query = (
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'media_item' ORDER BY name"
            )
            item_indexes = await connection.execute_query_dict(item_indexes_query)

            # verify upgrading, rolling back and upgrading again without data loss
            for target in (current, previous, current):
                await executor.migrate(target)
                for table, originals in snapshots.items():
                    rows = await connection.execute_query_dict(
                        f"SELECT * FROM {table} ORDER BY id"
                    )
                    assert len(rows) == len(originals), (target, table, rows)
                    for row, original in zip(rows, originals, strict=True):
                        assert {key: row[key] for key in original} == original
                        if fields := added.get(table):
                            if target == current:
                                assert all(row[key] is None for key in fields)
                            else:
                                assert not fields.intersection(row)
                indexes = await connection.execute_query_dict(index_query)
                assert len(indexes) == (1 if target == current else 0)
                assert (
                    await connection.execute_query_dict(item_indexes_query)
                    == item_indexes
                )
                assert (
                    await connection.execute_query_dict("PRAGMA foreign_key_check")
                    == []
                )

            # a repeated upgrade must not reset subsequently written values
            await MediaItem.filter(id=1).update(title="Updated")
            locator = {"schema_version": 1, "chapter_id": "c1", "block_id": "b2"}
            reading = await UserHistory.create(
                user_id=1, rel_type=HistoryType.TEXT, rel_id=10, locator=locator
            )
            await executor.migrate(current)
            assert (await MediaItem.get(id=1)).title == "Updated"
            await reading.refresh_from_db()
            assert reading.locator == locator
            assert reading.position is None
            assert (await UserHistory.get(id=1)).position == 123
            episode = await MediaItem.get(id=3)
            assert (await episode.parent).id == 2
            with pytest.raises(IntegrityError):
                await MediaItem.create(
                    lib_id=1, path="/movies/a.mkv", dir="/movies", name="Duplicate"
                )
            assert await MigrationAutodetector(Tortoise.apps, apps).changes() == []
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
