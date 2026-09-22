"""Unit tests for media routes."""

import asyncio
import inspect
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from lxml import etree
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import BadRequestException
from app.core.media import organizer
from app.core.media.coordination import library_lock
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib, MediaMetadata
from app.routes import media as media_routes


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


def test_nfo_parent_recovery(tmp_path):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            folder = tmp_path / "Old"
            folder.mkdir()
            video = folder / "movie.mkv"
            video.write_bytes(b"video")
            nfo = folder / "Old.nfo"
            nfo.write_text("<movie><title>Original</title></movie>")
            parent = await MediaItem.create(
                lib=lib,
                path=str(folder),
                dir=str(folder),
                name=folder.name,
                nfo_path=str(nfo),
            )
            child = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(folder),
                name=video.stem,
            )
            async with library_lock(lib.dir):
                payload = await organizer._plan(lib, [child], parent)
                await MediaEvent.create(
                    lib=lib,
                    event_type="organize",
                    src_path=str(folder),
                    payload=payload,
                )
            body = MediaMetadata(graph_id=1, metadata={"title": "Manual edit"})
            route = inspect.unwrap(cast(Any, media_routes.generate_nfo))

            with pytest.raises(BadRequestException):
                await route(None, body, parent.id)

            await child.refresh_from_db()
            current_nfo = tmp_path / "Original.nfo"
            assert not await MediaItem.filter(id=parent.id).exists()
            assert child.path == str(tmp_path / "Original.mkv")
            assert child.nfo_path == str(current_nfo)
            assert etree.parse(current_nfo).getroot().findtext("title") == "Original"
            assert not nfo.exists()
            assert not folder.exists()
            assert not await MediaEvent.filter(event_type="organize").exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("written", [False, True])
def test_nfo_episode_refresh(tmp_path, monkeypatch, written):
    publish = AsyncMock(return_value=written)
    refresh = AsyncMock()
    monkeypatch.setattr(media_routes, "gen_nfo", publish)
    monkeypatch.setattr(media_routes.MediaItemService, "refresh_episodes", refresh)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Shows", dir=str(tmp_path), lib_type=LibType.TV_SHOW, priority=1
            )
            parent = await MediaItem.create(
                lib=lib, path=str(tmp_path), dir=str(tmp_path), name="Show"
            )
            child = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(tmp_path / "episode.mkv"),
                dir=str(tmp_path),
                name="episode",
                episode=1,
            )
            body = MediaMetadata(graph_id=1, metadata={"title": "Updated"})
            route = inspect.unwrap(cast(Any, media_routes.generate_nfo))

            if written:
                response = await route(None, body, parent.id)
                assert response.status == 204
            else:
                with pytest.raises(BadRequestException):
                    await route(None, body, parent.id)

            publish.assert_awaited_once()
            if written:
                refresh.assert_awaited_once_with(parent, body, episode_ids=[child.id])
            else:
                refresh.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
