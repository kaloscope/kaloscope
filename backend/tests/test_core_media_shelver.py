"""NFO publication and stable item identity during library organization."""

import asyncio
import hashlib
import threading
from pathlib import Path

import pytest
from filelock import Timeout
from lxml import etree
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.media import shelver
from app.core.media.coordination import library_lock
from app.models.media import LibType, MediaItem, MediaLib, NFOType
from app.services.media import MediaItemService


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


def test_nfo_publish(tmp_path, monkeypatch):
    path = tmp_path / "movie.nfo"
    publish = shelver.rename_exclusive
    observed = []

    def inspect_publication(source, destination):
        assert not destination.exists()
        observed.append(etree.parse(source).getroot().findtext("title"))
        publish(source, destination)

    monkeypatch.setattr(shelver, "rename_exclusive", inspect_publication)

    async def run():
        assert await shelver.gen_nfo(NFOType.MOVIE, str(path), {"title": "First"})
        assert not await shelver.gen_nfo(NFOType.MOVIE, str(path), {"title": "Second"})
        assert observed == ["First"]
        assert etree.parse(path).getroot().findtext("title") == "First"
        path.chmod(0o600)
        assert await shelver.gen_nfo(
            NFOType.MOVIE, str(path), {"title": "Updated"}, overwrite=True
        )
        assert path.stat().st_mode & 0o777 == 0o600
        assert etree.parse(path).getroot().findtext("title") == "Updated"
        assert list(tmp_path.iterdir()) == [path]

    asyncio.run(run())


def test_current_path(tmp_path):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            destination = tmp_path / "New" / "New.mkv"
            destination.parent.mkdir()
            destination.write_bytes(b"video")
            item = await MediaItem.create(
                lib=lib, path=str(destination), dir=str(destination.parent), name="New"
            )
            stale_nfo = tmp_path / "Old" / "Old.nfo"
            assert await shelver.gen_nfo(
                NFOType.MOVIE, str(stale_nfo), {"title": "New"}, item_id=item.id
            )
            current_nfo = destination.with_suffix(".nfo")
            assert current_nfo.is_file()
            assert not stale_nfo.parent.exists()
            assert await shelver.update_metadata(lib, current_nfo) == [item.id]
            await item.refresh_from_db()
            assert item.title == "New"
            assert item.nfo_path == str(current_nfo)
            # detail reads remain pure even when organization is configured
            lib.rename_template = "{{title}} ({{year}})"
            assert shelver.parse_nfo(lib.lib_type, current_nfo).title == "New"
            assert Path(item.path).is_file()
            await MediaItemService._hash_and_size(item.id)
            await item.refresh_from_db()
            assert item.hash == hashlib.md5(b"video").hexdigest()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_metadata_fallback(tmp_path):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Shows", dir=str(tmp_path), lib_type=LibType.TV_SHOW, priority=1
            )
            current = tmp_path / "New"
            current.mkdir()
            item = await MediaItem.create(
                lib=lib, path=str(current), dir=str(current), name="New"
            )
            body = {"title": "New", "season": 3}
            assert await shelver.gen_nfo(
                NFOType.TV_SHOW,
                str(tmp_path / "Old" / "Old.nfo"),
                body,
                item_id=item.id,
                fallback=body,
            )
            await item.refresh_from_db()
            assert item.nfo_path == str(current / "New.nfo")
            assert item.season == 3  # tvshow.nfo does not serialize this field.
            assert item.title == "New"
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_publish_cancellation(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    publish = shelver.rename_exclusive

    def delayed_publish(source, destination):
        started.set()
        assert release.wait(timeout=5)
        try:
            publish(source, destination)
        finally:
            finished.set()

    monkeypatch.setattr(shelver, "rename_exclusive", delayed_publish)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            video = tmp_path / "Movie.mkv"
            video.write_bytes(b"video")
            item = await MediaItem.create(
                lib=lib, path=str(video), dir=str(tmp_path), name=video.stem
            )
            nfo = video.with_suffix(".nfo")
            task = asyncio.create_task(
                shelver.gen_nfo(
                    NFOType.MOVIE, str(nfo), {"title": "Movie"}, item_id=item.id
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 3)
                task.cancel()
                with pytest.raises(Timeout):
                    async with await library_lock(lib.dir).acquire(timeout=0):
                        pass
                assert not finished.is_set()
                assert not task.done()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert finished.is_set()
            assert etree.parse(nfo).getroot().findtext("title") == "Movie"
            async with await library_lock(lib.dir).acquire(timeout=1):
                assert not list(tmp_path.glob(".*.tmp"))
        finally:
            release.set()
            await Tortoise.close_connections()

    asyncio.run(run())
