"""Unit tests for core media."""

import asyncio
import hashlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.media.common import original_file_info
from app.core.media.coordination import library_lock
from app.core.media.handlers.base import MediaPathInfo, get_handler
from app.core.media.handlers.tvshow import TVShowMediaHandler
from app.core.media.shelver import update_metadata
from app.models.media import Language, LibType, MediaItem, MediaLib
from app.services.media import MediaItemService


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "broken_symlink"])
def test_original_info(tmp_path, monkeypatch, kind):
    target = tmp_path / "原始.mkv"
    if kind == "directory":
        target.mkdir()
    elif kind != "broken_symlink":
        target.write_bytes(b"video")
    if target.exists():
        os.utime(target, (1_700_000_000, 1_700_000_000))
    path = target
    if "symlink" in kind:
        path = tmp_path / "link.mkv"
        path.symlink_to("./原始.mkv")
    monkeypatch.chdir(tmp_path)

    original = original_file_info(path.name)

    assert original == {
        "path": str(path),
        "type": "symlink" if kind == "broken_symlink" else kind,
        "size": 5 if kind in ("file", "symlink") else None,
        "mtime": None if kind == "broken_symlink" else "2023-11-14T22:13:20+00:00",
        "link_target": "./原始.mkv" if "symlink" in kind else None,
    }


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_original_unavailable(tmp_path, monkeypatch, error):
    path = tmp_path / "original.mkv"

    def unavailable(self):
        """Simulate unavailable source attributes.

        Raises:
            OSError: With the selected filesystem access error.
        """
        raise error(str(self))

    monkeypatch.setattr(Path, "lstat", unavailable)

    original = original_file_info(path)

    assert original == {
        "path": str(path),
        "type": None,
        "size": None,
        "mtime": None,
        "link_target": None,
    }


@pytest.mark.parametrize("mtime", [253_402_300_800.0, -62_135_596_801.0, float("inf")])
def test_original_timestamp(tmp_path, monkeypatch, mtime):
    path = tmp_path / "source.mkv"
    path.write_bytes(b"video")
    info = list(path.stat())
    info[8] = mtime
    monkeypatch.setattr(Path, "lstat", Mock(return_value=os.stat_result(info)))

    original = original_file_info(path)

    assert original == {
        "path": str(path),
        "type": "file",
        "size": 5,
        "mtime": None,
        "link_target": None,
    }


@pytest.mark.parametrize(
    ("title", "expected"),
    [(None, "Example"), ("", "Example"), ("Library title", "Library title")],
)
def test_tvshow_title(monkeypatch, tmp_path: Path, title: str | None, expected: str):
    path = tmp_path / "Example (2024)" / "Example S01E01.mkv"
    path.parent.mkdir()
    path.touch()
    lib = MediaLib(
        id=1, dir=str(tmp_path), lib_type=LibType.TV_SHOW, language=Language.EN_US
    )
    monkeypatch.setattr(
        MediaItemService,
        "create",
        AsyncMock(side_effect=[MediaItem(id=1, title=title), MediaItem(id=2)]),
    )

    result = asyncio.run(TVShowMediaHandler().gen_items(lib, path))

    assert result[1].title == expected


@pytest.mark.parametrize(
    ("lib_type", "relative"),
    [
        (LibType.MOVIE, "Original.mkv"),
        (LibType.MOVIE, "Original/Original.mkv"),
        (LibType.TV_SHOW, "Original/Original S01E01.mkv"),
        (LibType.TV_SHOW, "Original/Season 01/Original S01E01.mkv"),
    ],
)
@pytest.mark.parametrize("legacy", [False, True])
def test_original(tmp_path, monkeypatch, lib_type, relative, legacy):
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    os.utime(path, (1_700_000_000, 1_700_000_000))
    scheduled = []
    monkeypatch.setattr("app.services.media.create_task", scheduled.append)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            handler = get_handler(lib_type)
            await handler.gen_items(lib, path)
            items = await MediaItem.all()
            assert all(item.original["path"] == item.path for item in items)
            item = next(item for item in items if item.path == str(path))
            original = {
                "path": str(path),
                "type": "file",
                "size": 5,
                "mtime": "2023-11-14T22:13:20+00:00",
                "link_target": None,
            }
            assert item.original == original
            for parent in items:
                if parent.id != item.id:
                    assert parent.original["type"] == "directory"
                    assert parent.original["size"] is None
            if legacy:
                await MediaItem.all().update(original=None)

            destination = path.with_name("Renamed.mkv")
            path.rename(destination)
            destination.write_bytes(b"changed video")
            os.utime(destination, (1_800_000_000, 1_800_000_000))
            await MediaItem.filter(id=item.id).update(
                path=str(destination), name=destination.stem
            )
            await item.refresh_from_db()
            async with library_lock(lib.dir):
                await MediaItemService.refresh_hash_and_size(item)
            before = await MediaItem.all().values()
            snapshot = Mock(side_effect=AssertionError("existing item was inspected"))
            monkeypatch.setattr("app.services.media.original_file_info", snapshot)
            await handler.gen_items(lib, destination)

            snapshot.assert_not_called()
            assert await MediaItem.all().values() == before
            await item.refresh_from_db()
            assert item.original == (None if legacy else original)
            assert item.size == len(b"changed video")
        finally:
            for coroutine in scheduled:
                coroutine.close()
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("registered", [False, True])
def test_metadata_targets(tmp_path, registered):
    path = tmp_path / "movie.nfo"
    path.write_text(
        "<movie><title>Updated</title><year>2026</year>"
        '<uniqueid type="tmdb" default="true">42</uniqueid></movie>'
    )

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            other_lib = await MediaLib.create(
                name="Other",
                dir=str(tmp_path / "other"),
                lib_type=LibType.MOVIE,
                priority=2,
            )
            names = ("disc-1", "movie") if registered else ("movie", "movie")
            items = [
                await MediaItem.create(
                    lib=lib,
                    path=str(tmp_path / f"{name}.{extension}"),
                    dir=str(tmp_path),
                    name=name,
                    nfo_path=str(path) if registered else None,
                    title="Original",
                )
                for name, extension in zip(names, ("mkv", "mp4"), strict=True)
            ]
            unrelated = await MediaItem.create(
                lib=lib,
                path=str(tmp_path / "unrelated.mkv"),
                dir=str(tmp_path),
                name="unrelated",
                title="Unrelated",
            )
            other = await MediaItem.create(
                lib=other_lib,
                path=str(tmp_path / "movie.mkv"),
                dir=str(tmp_path),
                name="movie",
                nfo_path=str(path),
                title="Other library",
            )

            affected = await update_metadata(lib, path)

            for item in items:
                await item.refresh_from_db()
                assert item.title == "Updated"
                assert item.year == 2026
                assert item.nfo_path == str(path)
                assert item.nfo_mtime is not None
                assert item.unique_id == "42" and item.nfo_source == "tmdb"
            assert sorted(affected) == sorted(item.id for item in items)
            await unrelated.refresh_from_db()
            assert unrelated.title == "Unrelated"
            assert unrelated.nfo_path is None
            assert unrelated.nfo_mtime is None
            await other.refresh_from_db()
            assert other.title == "Other library"
            assert other.nfo_mtime is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    "content",
    [None, "", "<movie><title>Updated</title></movie>"],
    ids=["missing", "invalid", "unmatched"],
)
def test_metadata_empty(tmp_path, content):
    path = tmp_path / "movie.nfo"
    if content is not None:
        path.write_text(content)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(tmp_path / "other.mkv"),
                dir=str(tmp_path),
                name="other",
                title="Original",
            )

            affected = await update_metadata(lib, str(path))

            assert affected == []
            await item.refresh_from_db()
            assert item.title == "Original"
            assert item.nfo_path is None
            assert item.nfo_mtime is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("removed", [None, "record", "file"])
def test_hash_current_path(tmp_path, monkeypatch, removed):
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))
    monkeypatch.setattr(MediaItemService, "HASH_READ_SIZE", 4)
    source = tmp_path / "old.mkv"
    destination = tmp_path / "new.mkv"
    source.write_bytes(b"head-tail")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        tasks = []
        waiting = asyncio.Event()

        def track_hash(coroutine):
            task = asyncio.create_task(coroutine)
            tasks.append(task)
            return task

        def waiting_lock(directory):
            waiting.set()
            return library_lock(directory)

        monkeypatch.setattr("app.services.media.create_task", track_hash)
        monkeypatch.setattr("app.services.media.library_lock", waiting_lock)
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            async with library_lock(lib.dir):
                item = await MediaItemService.create(
                    lib.id, path_info=MediaPathInfo(source)
                )
                source.rename(destination)
                await MediaItem.filter(id=item.id).update(
                    path=str(destination), name=destination.stem
                )
                await asyncio.wait_for(waiting.wait(), timeout=2)
                assert len(tasks) == 1 and not tasks[0].done()
                if removed == "record":
                    await item.delete()
                elif removed == "file":
                    destination.unlink()
            await asyncio.wait_for(tasks[0], timeout=3)

            if removed == "record":
                assert not await MediaItem.filter(id=item.id).exists()
            else:
                await item.refresh_from_db()
                assert item.path == str(destination)
                if removed == "file":
                    assert item.hash is None and item.size is None
                else:
                    assert item.hash == hashlib.md5(b"head").hexdigest()
                    assert item.size == len(b"head-tail")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("missing", ["hash", "size", "both", None])
def test_hash_missing(tmp_path, monkeypatch, missing):
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))
    scheduled = []
    monkeypatch.setattr("app.services.media.create_task", scheduled.append)
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"video")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItemService.create(lib.id, path_info=MediaPathInfo(path))
            await MediaItem.filter(id=item.id).update(
                hash=None if missing in ("hash", "both") else "previous",
                size=None if missing in ("size", "both") else 99,
            )

            assert len(scheduled) == 1
            await scheduled[0]

            await item.refresh_from_db()
            if missing is None:
                assert item.hash == "previous" and item.size == 99
            else:
                assert item.hash == hashlib.md5(b"video").hexdigest()
                assert item.size == len(b"video")
        finally:
            for coroutine in scheduled:
                coroutine.close()
            await Tortoise.close_connections()

    asyncio.run(run())
