"""Unit tests for core media."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

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
