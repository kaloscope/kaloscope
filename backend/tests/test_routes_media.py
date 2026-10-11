"""Unit tests for media routes."""

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from lxml import etree
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import BadRequestException
from app.core.media import organizer
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib, MediaMetadata
from app.models.user import PermType, User, UserPermission
from app.routes import media as media_routes
from tests.test_services_media_reading import _client, _database, _user


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


@pytest.mark.parametrize("lib_type", list(LibType))
@pytest.mark.parametrize("access", ["admin", "granted", "denied"])
def test_list_permissions(tmp_path, lib_type, access):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            parent = await MediaItem.create(
                lib=lib, path=str(tmp_path / "Work"), dir=str(tmp_path), name="Work"
            )
            child = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(tmp_path / "Work" / "Chapter"),
                dir=parent.path,
                name="Chapter",
            )
            user = _user() if access == "admin" else _user([])
            await User.create(
                id=user.id, username=user.username, password="unused", role=user.role
            )
            if access == "granted":
                await UserPermission.create(
                    user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=lib.id
                )
            async with _client(user) as client:
                for params, item in (
                    ({}, parent),
                    ({"lib_id": lib.id}, parent),
                    ({"keyword": "Work"}, parent),
                    ({"path": parent.path}, parent),
                    ({"path": child.path}, child),
                ):
                    response = await client.get("/_api/media/list", params=params)
                    assert response.status_code == 200, response.text
                    ids = [item["id"] for item in response.json()["data"]["items"]]
                    assert ids == ([] if access == "denied" else [item.id])
                await MediaItem.filter(id=parent.id).update(visible=False)
                for params in ({}, {"path": parent.path}, {"path": child.path}):
                    response = await client.get("/_api/media/list", params=params)
                    assert response.status_code == 200, response.text
                    assert response.json()["data"]["items"] == []
                client.cookies.clear()
                response = await client.get(
                    "/_api/media/list", headers={"Authorization": ""}
                )
                assert response.status_code == 401

    asyncio.run(run())


@pytest.mark.parametrize("admin", [False, True])
def test_library_management_permissions(tmp_path, admin):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=LibType.NOVEL, priority=1
            )
            scan = AsyncMock()
            async with _client(_user() if admin else _user([])) as client:
                app = cast(Any, client._transport).app
                app.ctx.lib_watcher = SimpleNamespace(scan_directory=scan)
                response = await client.get(f"/_api/media/lib/{lib.id}/scan")
                assert response.status_code == (204 if admin else 403), response.text
                assert scan.await_count == int(admin)
                response = await client.post(
                    "/_api/media/lib/sort", json={"ids": [lib.id]}
                )
                assert response.status_code == (204 if admin else 403), response.text

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", list(LibType))
def test_scan_library(tmp_path, lib_type):
    async def run():
        async with _database(), _client(_user()) as client:
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            scan = AsyncMock()
            cast(Any, client._transport).app.ctx.lib_watcher = SimpleNamespace(
                scan_directory=scan
            )
            response = await client.get(f"/_api/media/lib/{lib.id}/scan")
            assert response.status_code == 204, response.text
            scan.assert_awaited_once_with(lib, validate_request=True)
            scan.side_effect = ContentError("media_source_unavailable")
            response = await client.get(f"/_api/media/lib/{lib.id}/scan")
            assert response.status_code == 503
            assert response.json()["message"] == "media_source_unavailable"

    asyncio.run(run())


@pytest.mark.parametrize("admin", [False, True])
def test_library_scan_status(tmp_path, admin):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=LibType.NOVEL, priority=1
            )
            user = _user() if admin else _user([])
            await User.create(
                id=user.id, username=user.username, password="unused", role=user.role
            )
            await UserPermission.create(
                user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=lib.id
            )
            task = await MediaEvent.create(
                lib=lib,
                src_path=str(tmp_path / "Book.txt"),
                event_type="reconcile",
                payload={"state": "deferred", "error_code": "unsupported_layout"},
            )
            async with _client(user) as client:
                cast(Any, client._transport).app.ctx.lib_watcher = SimpleNamespace(
                    is_scanning=lambda _: False
                )
                response = await client.get("/_api/media/lib/list")
                assert response.status_code == 200, response.text
                data = response.json()["data"][0]
                assert data["scanning"] is True
                assert "issues" not in data
                task.payload = {"state": "failed", "error_code": "unsupported_layout"}
                await task.save()
                data = (await client.get("/_api/media/lib/list")).json()["data"][0]
                assert data["scanning"] is False
                assert "issues" not in data
                await task.delete()
                data = (await client.get("/_api/media/lib/list")).json()["data"][0]
                assert data["scanning"] is False

    asyncio.run(run())


@pytest.mark.parametrize(
    ("range_header", "start", "end"),
    [("bytes=0-999", 0, 9), ("bytes=4-999", 4, 9), ("bytes=4-6", 4, 6)],
)
def test_video_stream_range(tmp_path, range_header, start, end):
    async def run():
        async with _database(), _client(_user()) as client:
            path = tmp_path / "video.mp4"
            data = b"0123456789"
            path.write_bytes(data)
            lib = await MediaLib.create(
                name="Video", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            await MediaItem.create(
                lib=lib, path=str(path), dir=str(tmp_path), name=path.stem
            )
            response = await client.get(
                "/_api/media/stream",
                params={"path": str(path)},
                headers={"Range": range_header},
            )
            assert response.status_code == 206
            assert response.content == data[start : end + 1]
            assert response.headers["Content-Range"] == f"bytes {start}-{end}/10"

    asyncio.run(run())


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
            body = MediaMetadata(
                graph_id=1, metadata={"title": "Updated", "site": "tmdb", "id": "42"}
            )
            route = inspect.unwrap(cast(Any, media_routes.generate_nfo))

            response = await route(None, body, parent.id)

            assert response.status == 204
            expected = []
            if change == "moved":
                assert original is not None
                expected = [original.path]
            assert [
                call.kwargs["bootparams"]["path"] for call in execute.await_args_list
            ] == expected
            for call in execute.await_args_list:
                params = call.kwargs["bootparams"]
                assert params["site"] == "tmdb" and params["series_id"] == "42"
                assert params["name"] == "Original"
                assert {"item_path", "item_name", "nfo_source"}.isdisjoint(params)
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


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
