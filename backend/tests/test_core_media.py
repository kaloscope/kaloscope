"""Unit tests for core media."""

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.media.coordination import library_lock
from app.core.media.handlers.base import MediaPathInfo
from app.core.media.handlers.tvshow import TVShowMediaHandler
from app.core.media.shelver import update_metadata
from app.models.media import Language, LibType, MediaItem, MediaLib
from app.services.media import MediaItemService


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


@pytest.mark.parametrize("registered", [False, True])
def test_metadata_targets(tmp_path, registered):
    path = tmp_path / "movie.nfo"
    path.write_text("<movie><title>Updated</title><year>2026</year></movie>")

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
    calculate = MediaItemService._hash_and_size

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        tasks = []
        waiting = asyncio.Event()

        async def track_hash(*args):
            tasks.append(asyncio.current_task())
            await calculate(*args)

        def waiting_lock(directory):
            waiting.set()
            return library_lock(directory)

        monkeypatch.setattr(MediaItemService, "_hash_and_size", track_hash)
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
            item = await MediaItem.create(
                lib=lib,
                path=str(path),
                dir=str(tmp_path),
                name=path.stem,
                hash=None if missing in ("hash", "both") else "previous",
                size=None if missing in ("size", "both") else 99,
            )

            await MediaItemService._hash_and_size(item.id)

            await item.refresh_from_db()
            if missing is None:
                assert item.hash == "previous" and item.size == 99
            else:
                assert item.hash == hashlib.md5(b"video").hexdigest()
                assert item.size == len(b"video")
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
