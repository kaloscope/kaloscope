"""Unit tests for media routes."""

import asyncio
import inspect
from types import SimpleNamespace
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
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(tmp_path),
                dir=str(tmp_path),
                name="Original",
                nfo_path=str(tmp_path / "Original.nfo"),
            )
            child = await MediaItem.create(
                lib=lib,
                parent=item,
                path=str(tmp_path / "episode.mkv"),
                dir=str(tmp_path),
                name="episode",
                episode=1,
            )
            body = MediaMetadata(graph_id=1, metadata={"title": "Updated"})
            route = inspect.unwrap(cast(Any, media_routes.generate_nfo))

            if written:
                response = await route(None, body, item.id)
                assert response.status == 204
            else:
                with pytest.raises(BadRequestException):
                    await route(None, body, item.id)

            publish.assert_awaited_once_with(
                media_routes.get_nfo_type(lib_type),
                item.nfo_path,
                body.metadata,
                overwrite=True,
                item_id=item.id,
                refresh=True,
            )
            if written and lib_type == LibType.TV_SHOW:
                refresh.assert_awaited_once_with(item, body, episode_ids=[child.id])
            else:
                refresh.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["moved", "removed", "empty"])
def test_episode_scope(tmp_path, monkeypatch, change):
    execute = AsyncMock(return_value=[])
    app = SimpleNamespace(
        ctx=SimpleNamespace(flow_engine=SimpleNamespace(execute=execute))
    )
    monkeypatch.setattr("app.services.media.Sanic.get_app", lambda: app)

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
                lib=lib,
                path=str(tmp_path / "Show"),
                dir=str(tmp_path / "Show"),
                name="Show",
                nfo_path=str(tmp_path / "Show" / "Show.nfo"),
            )
            other = await MediaItem.create(
                lib=lib,
                path=str(tmp_path / "Other"),
                dir=str(tmp_path / "Other"),
                name="Other",
            )
            original = None
            if change != "empty":
                original = await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(tmp_path / "Show" / "Original.mkv"),
                    dir=parent.dir,
                    name="Original",
                    episode=1,
                )

            async def publish(*args, **kwargs):
                if original is not None:
                    if change == "removed":
                        await original.delete()
                    else:
                        await MediaItem.filter(id=original.id).update(
                            parent_id=other.id
                        )
                await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(tmp_path / "Show" / "New.mkv"),
                    dir=parent.dir,
                    name="New",
                    episode=2,
                )
                return True

            monkeypatch.setattr(media_routes, "gen_nfo", publish)
            body = MediaMetadata(graph_id=1, metadata={"title": "Updated"})
            route = inspect.unwrap(cast(Any, media_routes.generate_nfo))

            response = await route(None, body, parent.id)

            assert response.status == 204
            expected = [original.path] if change == "moved" else []
            assert [
                call.kwargs["bootparams"]["item_path"]
                for call in execute.await_args_list
            ] == expected
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
