"""Unit tests for NFO publication and media shelving."""

import asyncio
import hashlib
import mimetypes
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from filelock import Timeout
from lxml import etree
from sanic import Sanic
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.constants import NFO_MIME_TYPE
from app.core.media import organizer, shelver, watcher
from app.core.media.coordination import library_lock
from app.models.media import (
    LibType,
    MediaEvent,
    MediaItem,
    MediaLib,
    MediaMetadata,
    NFOType,
)
from app.services.flow import FlowTriggerService
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
@pytest.mark.parametrize("cancel_count", [1, 2])
@pytest.mark.parametrize("indexed", [False, True])
def test_publish_cancellation(
    tmp_path, monkeypatch, failure, cleanup_failure, cancel_count, indexed
):
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
                    NFOType.MOVIE,
                    str(nfo),
                    {"title": "Movie"},
                    item_id=item.id if indexed else None,
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 3)
                for _ in range(cancel_count):
                    task.cancel()
                    await asyncio.sleep(0)
                    if indexed:
                        with pytest.raises(Timeout):
                            async with await library_lock(lib.dir).acquire(timeout=0):
                                pass
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
            async with await library_lock(lib.dir).acquire(timeout=1):
                temporary = list(tmp_path.glob(".*.tmp"))
                if cleanup_failure:
                    assert len(temporary) == 1
                    assert (
                        etree.parse(temporary[0]).getroot().findtext("title") == "Movie"
                    )
                else:
                    assert not temporary
        finally:
            release.set()
            await Tortoise.close_connections()

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
            metadata = shelver.parse_nfo(lib.lib_type, current_nfo)
            assert metadata is not None
            assert metadata.title == "New"
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


@pytest.mark.parametrize("written", [False, True])
@pytest.mark.parametrize("refresh", [False, True])
def test_metadata_fallback(tmp_path, monkeypatch, written, refresh):
    update_metadata = shelver.update_metadata

    async def locked_update(lib, path, **kwargs):
        with pytest.raises(Timeout):
            async with await library_lock(lib.dir).acquire(timeout=0):
                pass
        return await update_metadata(lib, path, **kwargs)

    update = AsyncMock(side_effect=locked_update)
    monkeypatch.setattr(shelver, "update_metadata", update)

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
                lib=lib,
                path=str(current),
                dir=str(current),
                name="New",
                title="Original",
            )
            nfo_path = current / "New.nfo"
            if not written:
                nfo_path.write_text("<tvshow><title>Original</title></tvshow>")
            body = {"title": "New", "season": 3}

            result = await shelver.gen_nfo(
                NFOType.TV_SHOW,
                str(tmp_path / "Old" / "Old.nfo"),
                body,
                item_id=item.id,
                refresh=refresh,
            )

            assert result is written
            await item.refresh_from_db()
            if written and refresh:
                update.assert_awaited_once()
                assert item.nfo_path == str(nfo_path)
                assert item.season == 3
                assert item.title == "New"
            else:
                update.assert_not_awaited()
                assert item.nfo_path is None
                assert item.season is None
                assert item.title == "Original"
            assert not (tmp_path / "Old").exists()
            assert not await MediaEvent.all().exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("lib_type", "nfo_type", "explicit_nfo"),
    [
        (LibType.MOVIE, NFOType.MOVIE, True),
        (LibType.TV_SHOW, NFOType.EPISODE, False),
        (LibType.TV_SHOW, NFOType.EPISODE, True),
    ],
)
def test_child_nfo(tmp_path, lib_type, nfo_type, explicit_nfo):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            directory = tmp_path / "Parent"
            directory.mkdir()
            parent_nfo = directory / "Parent.nfo"
            parent_type = shelver.get_nfo_type(lib_type)
            parent_content = f"<{parent_type}><title>Parent</title></{parent_type}>"
            parent_nfo.write_text(parent_content)
            parent = await MediaItem.create(
                lib=lib,
                path=str(directory),
                dir=str(directory),
                name=directory.name,
                nfo_path=str(parent_nfo),
                title="Parent",
            )
            video = directory / "child.mkv"
            video.write_bytes(b"video")
            child_nfo = directory / "custom.nfo" if explicit_nfo else None
            item = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(directory),
                name=video.stem,
                nfo_path=str(child_nfo) if child_nfo else None,
            )

            assert await shelver.gen_nfo(
                nfo_type,
                str(tmp_path / "stale.nfo"),
                {"title": "Child", "year": 2027},
                overwrite=True,
                item_id=item.id,
                refresh=True,
            )

            await item.refresh_from_db()
            await parent.refresh_from_db()
            expected_nfo = child_nfo or video.with_suffix(".nfo")
            assert item.nfo_path == str(expected_nfo)
            assert etree.parse(expected_nfo).getroot().findtext("title") == "Child"
            assert item.title == "Child"
            assert parent.title == "Parent"
            assert parent_nfo.read_text() == parent_content
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["pending", "moved", "conflict"])
@pytest.mark.parametrize("refresh", [False, True])
def test_nfo_recovery(tmp_path, stage, refresh):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            source, destination = tmp_path / "old.mkv", tmp_path / "new.mkv"
            source.write_bytes(b"video")
            old_nfo, new_nfo = (
                source.with_suffix(".nfo"),
                destination.with_suffix(".nfo"),
            )
            original = "<movie><title>Original</title></movie>"
            old_nfo.write_text(original)
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(tmp_path),
                name=source.stem,
                title="Original",
                nfo_path=str(old_nfo),
            )
            moves = [(source, destination), (old_nfo, new_nfo)]
            event = await MediaEvent.create(
                lib=lib,
                src_path=str(source),
                event_type="organize",
                payload={
                    "moves": [
                        {
                            "src": str(old),
                            "dst": str(new),
                            "identity": organizer._fingerprint(old),
                        }
                        for old, new in moves
                    ],
                    "updates": [
                        {
                            "id": item.id,
                            "path": str(destination),
                            "dir": str(tmp_path),
                            "name": destination.stem,
                            "nfo_path": str(new_nfo),
                        }
                    ],
                    "parent": None,
                    "delete_parent": None,
                    "mapping": {str(source): str(destination)},
                    "creates": [],
                    "symlinks": [],
                    "nfo_edits": [],
                },
            )
            if stage == "moved":
                for old, new in moves:
                    old.rename(new)
            elif stage == "conflict":
                destination.write_bytes(b"unrelated")

            if stage == "conflict":
                with pytest.raises(organizer.OrganizePendingError):
                    await shelver.gen_nfo(
                        NFOType.MOVIE,
                        str(old_nfo),
                        {"title": "Corrected"},
                        overwrite=True,
                        item_id=item.id,
                        refresh=refresh,
                    )
                await item.refresh_from_db()
                assert item.path == str(source)
                assert item.nfo_path == str(old_nfo)
                assert item.title == "Original"
                assert old_nfo.read_text() == original
                assert not new_nfo.exists()
                assert source.read_bytes() == b"video"
                assert destination.read_bytes() == b"unrelated"
                assert await MediaEvent.filter(id=event.id).exists()
                return

            assert await shelver.gen_nfo(
                NFOType.MOVIE,
                str(old_nfo),
                {"title": "Corrected"},
                overwrite=True,
                item_id=item.id,
                refresh=refresh,
            )

            await item.refresh_from_db()
            assert item.path == str(destination)
            assert item.nfo_path == str(new_nfo)
            assert item.title == ("Corrected" if refresh else "Original")
            assert etree.parse(new_nfo).getroot().findtext("title") == "Corrected"
            assert destination.read_bytes() == b"video"
            assert not source.exists()
            assert not old_nfo.exists()
            assert not await MediaEvent.filter(id=event.id).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("recovery", ["restart", "watchdog", "published"])
def test_metadata_recovery(tmp_path, monkeypatch, recovery):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    directory = tmp_path / "library"
    directory.mkdir()
    video = directory / "Old.mkv"
    video.write_bytes(b"video")
    nfo = video.with_suffix(".nfo")
    db_url = f"sqlite://{tmp_path / 'media.sqlite'}"
    notifications = []

    def notify(lib_id):
        with closing(
            sqlite3.connect(f"file:{tmp_path / 'media.sqlite'}?mode=ro", uri=True)
        ) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM media_event "
                "WHERE lib_id = ? AND event_type = 'ingest'",
                (lib_id,),
            ).fetchone()[0]
            notifications.append((lib_id, count))

    monkeypatch.setattr(shelver, "notify_media_events", notify)

    async def run():
        await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(directory),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            item = await MediaItem.create(
                lib=lib, path=str(video), dir=str(directory), name=video.stem
            )
            body = {"title": "New"}

            assert await shelver.gen_nfo(
                NFOType.MOVIE,
                str(nfo),
                body,
                overwrite=True,
                item_id=item.id,
                refresh=True,
            )

            await item.refresh_from_db()
            assert item.title == "New"
            assert notifications == [(lib.id, 1)]
            assert item.nfo_mtime is not None
            assert video.is_file()
            assert await MediaEvent.all().count() == 1
            pending = await MediaEvent.get()
            assert pending.payload == {"bootparams": [], "organize_ids": [item.id]}

            if recovery == "restart":
                await Tortoise.close_connections()
                await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
                lib = await MediaLib.get(id=lib.id)
            elif recovery == "published":
                await watcher.consume_event(pending)
            if recovery != "restart":
                await watcher.consume_event(
                    await MediaEvent.create(
                        lib=lib, src_path=str(nfo), event_type="created"
                    )
                )
            monitor = watcher.LibWatcher(Mock(spec=Sanic))
            events = await monitor._create_events(lib)
            while not events.empty():
                await watcher.consume_event(events.get_nowait())

            await item.refresh_from_db()
            assert item.path == str(directory / "New.mkv")
            assert Path(item.path).read_bytes() == b"video"
            assert item.nfo_path == str(directory / "New.nfo")
            assert not video.exists()
            assert not await MediaEvent.all().exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_metadata_event_failure(tmp_path, monkeypatch):
    create_event = AsyncMock(side_effect=RuntimeError("Event persistence failed"))
    monkeypatch.setattr(shelver.MediaEvent, "create", create_event)

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
            video = tmp_path / "Old.mkv"
            video.write_bytes(b"video")
            item = await MediaItem.create(
                lib=lib, path=str(video), dir=str(tmp_path), name=video.stem
            )
            nfo = video.with_suffix(".nfo")
            body = {"title": "New"}

            with pytest.raises(RuntimeError, match="Event persistence failed"):
                await shelver.gen_nfo(
                    NFOType.MOVIE,
                    str(nfo),
                    body,
                    overwrite=True,
                    item_id=item.id,
                    refresh=True,
                )

            await item.refresh_from_db()
            assert item.title is None
            assert item.nfo_path is None
            assert item.nfo_mtime is None
            assert etree.parse(nfo).getroot().findtext("title") == "New"
            assert not await MediaEvent.all().exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_movie_parent_wait(tmp_path, monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        task = None
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}} ({{year}})/{{title}}",
            )
            video = tmp_path / "old.mkv"
            video.write_bytes(b"video")
            original_nfo = video.with_suffix(".nfo")
            original_nfo.write_text(
                "<movie><title>Movie</title><year>2026</year></movie>"
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(video),
                dir=str(tmp_path),
                name=video.stem,
                nfo_path=str(original_nfo),
            )
            waiting = asyncio.Event()

            def waiting_lock(directory):
                waiting.set()
                return library_lock(directory)

            monkeypatch.setattr(shelver, "library_lock", waiting_lock)
            async with library_lock(lib.dir):
                task = asyncio.create_task(
                    shelver.gen_nfo(
                        NFOType.MOVIE,
                        str(original_nfo),
                        {"title": "Corrected", "year": 2027},
                        overwrite=True,
                        item_id=item.id,
                        refresh=True,
                    )
                )
                await asyncio.wait_for(waiting.wait(), timeout=3)
                assert not task.done()

                await organizer.organize_items(lib, [item.id])
                await item.refresh_from_db()
                assert item.parent_id is not None
                assert item.nfo_path is None

            assert await asyncio.wait_for(task, timeout=3)

            await item.refresh_from_db()
            parent = await MediaItem.get(id=item.parent_id)
            assert parent.nfo_path is not None
            assert parent.nfo_path == str(tmp_path / "Movie (2026)/Movie (2026).nfo")
            assert etree.parse(parent.nfo_path).getroot().findtext("title") == (
                "Corrected"
            )
            assert parent.title == "Corrected"
            assert parent.year == 2027
            assert item.nfo_path is None
            assert not Path(item.path).with_suffix(".nfo").exists()
            assert not original_nfo.exists()
        finally:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())
