"""Unit tests for media routes."""

import asyncio
import inspect
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.exceptions import BadRequestException
from app.models.media import LibType, MediaItem, MediaLib, MediaMetadata
from app.routes import media as media_routes


@pytest.mark.parametrize("lib_type", [LibType.MOVIE, LibType.TV_SHOW])
@pytest.mark.parametrize("written", [False, True])
def test_nfo_episode_refresh(tmp_path, monkeypatch, lib_type, written):
    publish = AsyncMock(return_value=written)
    update = AsyncMock()
    refresh = AsyncMock()
    monkeypatch.setattr(media_routes, "gen_nfo", publish)
    monkeypatch.setattr(media_routes, "update_metadata", update)
    monkeypatch.setattr(media_routes.MediaItemService, "refresh_episodes", refresh)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(tmp_path),
                dir=str(tmp_path),
                name="Original",
                nfo_path=str(tmp_path / "Original.nfo"),
            )
            body = MediaMetadata(graph_id=1, metadata={"title": "Updated"})
            route = inspect.unwrap(cast(Any, media_routes.generate_nfo))

            if written:
                response = await route(None, body, item.id)
                assert response.status == 204
            else:
                with pytest.raises(BadRequestException):
                    await route(None, body, item.id)

            publish.assert_awaited_once()
            if written:
                update.assert_awaited_once_with(
                    lib, item.nfo_path, fallback=body.metadata
                )
            else:
                update.assert_not_awaited()
            if written and lib_type == LibType.TV_SHOW:
                refresh.assert_awaited_once_with(item, body)
            else:
                refresh.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
