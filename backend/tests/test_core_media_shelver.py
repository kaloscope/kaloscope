"""Unit tests for NFO publication and media shelving."""

import asyncio
import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from lxml import etree
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.media import shelver
from app.core.media.coordination import library_lock
from app.models.media import LibType, MediaItem, MediaLib, MediaMetadata, NFOType
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


@pytest.mark.parametrize("title", ["A" * 240, "影" * 80])
def test_long_name(tmp_path, title):
    path = tmp_path / f"{title}.nfo"

    async def run():
        assert await shelver.gen_nfo(NFOType.MOVIE, str(path), {"title": title})
        assert etree.parse(path).getroot().findtext("title") == title

        assert await shelver.gen_nfo(
            NFOType.MOVIE, str(path), {"title": "Updated"}, overwrite=True
        )
        assert etree.parse(path).getroot().findtext("title") == "Updated"
        assert list(tmp_path.iterdir()) == [path]

    asyncio.run(run())


@pytest.mark.parametrize("overwrite", [False, True])
def test_publication_failure(tmp_path, monkeypatch, overwrite):
    path = tmp_path / "movie.nfo"
    original = "<movie><title>Original</title></movie>"
    if overwrite:
        path.write_text(original)

    def failed_render(*args, **kwargs):
        raise ValueError("Render failed")

    monkeypatch.setattr(shelver, "render", failed_render)

    async def run():
        with pytest.raises(ValueError, match="Render failed"):
            await shelver.gen_nfo(
                NFOType.MOVIE, str(path), {"title": "Updated"}, overwrite=overwrite
            )

    asyncio.run(run())

    if overwrite:
        assert path.read_text() == original
        assert list(tmp_path.iterdir()) == [path]
    else:
        assert not list(tmp_path.iterdir())


def test_publication_conflict(tmp_path, monkeypatch):
    path = tmp_path / "movie.nfo"
    original = "<movie><title>Existing</title></movie>"
    publish = shelver.rename_exclusive

    def competing_publication(source, destination):
        destination.write_text(original)
        publish(source, destination)

    monkeypatch.setattr(shelver, "rename_exclusive", competing_publication)

    result = asyncio.run(shelver.gen_nfo(NFOType.MOVIE, str(path), {"title": "New"}))

    assert result is False
    assert path.read_text() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("failure", "cleanup_failure"),
    [(None, False), (OSError, False), (FileExistsError, False), (OSError, True)],
)
def test_publish_cancellation(tmp_path, monkeypatch, failure, cleanup_failure):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    publish = shelver.rename_exclusive
    unlink = Path.unlink

    def failed_cleanup(path, *args, **kwargs):
        if path.name.startswith(".nfo-"):
            raise OSError("Temporary file cleanup failed")
        return unlink(path, *args, **kwargs)

    def delayed_publish(source, destination):
        started.set()
        assert release.wait(timeout=5)
        try:
            if failure is not None:
                raise failure("Publication failed")
            publish(source, destination)
        finally:
            finished.set()

    monkeypatch.setattr(shelver, "rename_exclusive", delayed_publish)
    if cleanup_failure:
        monkeypatch.setattr(Path, "unlink", failed_cleanup)

    async def run():
        nfo = tmp_path / "movie.nfo"
        task = asyncio.create_task(
            shelver.gen_nfo(NFOType.MOVIE, str(nfo), {"title": "Movie"})
        )
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            assert not finished.is_set()
            assert not task.done()
            assert len(list(tmp_path.glob(".nfo-*.tmp"))) == 1
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert finished.is_set()
        if failure is None:
            assert etree.parse(nfo).getroot().findtext("title") == "Movie"
        else:
            assert not nfo.exists()
        temporary = list(tmp_path.glob(".nfo-*.tmp"))
        if cleanup_failure:
            assert len(temporary) == 1
            assert etree.parse(temporary[0]).getroot().findtext("title") == "Movie"
        else:
            assert not temporary

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
            async with library_lock(lib.dir):
                await MediaItemService.refresh_hash_and_size(item)
            await item.refresh_from_db()
            assert item.hash == hashlib.md5(b"video").hexdigest()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["moved", "record", "file"])
def test_episode_path(tmp_path, monkeypatch, change):
    source = tmp_path / "Show" / "old.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    destination = source.with_name("new.mkv")

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
                path=str(source.parent),
                dir=str(source.parent),
                name="Show",
                season=1,
            )
            item = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(source),
                dir=str(source.parent),
                name=source.stem,
                season=1,
                episode=1,
            )

            async def execute(**kwargs):
                if change == "moved":
                    source.rename(destination)
                    await MediaItem.filter(id=item.id).update(
                        path=str(destination), name=destination.stem
                    )
                else:
                    source.unlink()
                    if change == "record":
                        await item.delete()
                return [{"title": "Updated"}]

            app = SimpleNamespace(
                ctx=SimpleNamespace(flow_engine=SimpleNamespace(execute=execute))
            )
            monkeypatch.setattr("app.services.media.Sanic.get_app", lambda: app)
            meta = MediaMetadata(graph_id=1, metadata={"title": "Show", "season": 1})

            await MediaItemService.refresh_episodes(parent, meta)

            assert not source.with_suffix(".nfo").exists()
            current_nfo = destination.with_suffix(".nfo")
            if change == "moved":
                assert etree.parse(current_nfo).getroot().findtext("title") == "Updated"
                assert destination.read_bytes() == b"video"
            else:
                assert not current_nfo.exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
