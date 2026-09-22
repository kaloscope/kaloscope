"""Organization integrates with the watcher without recreating media records."""

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tortoise import Tortoise
from watchdog.events import FileMovedEvent

from app.core.config import KaloscopeConfig
from app.core.constants import NFO_MIME_TYPE
from app.core.media import organizer, shelver, watcher
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import HistoryType, User, UserHistory, UserRole
from app.services.flow import FlowTriggerService
from app.services.media import MediaItemService


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


def test_ingest_paths(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    monkeypatch.setattr(watcher, "recover_organizing", AsyncMock(return_value={}))
    old = tmp_path / "old.mkv"
    old.write_bytes(b"video")
    old.with_suffix(".nfo").write_text("<movie><title>New</title></movie>")
    destination = tmp_path / "New.mkv"
    configs = []

    async def organize(lib, ids):
        configs.append(lib.rename_template)
        assert len(ids) == 1
        old.rename(destination)
        old.with_suffix(".nfo").rename(destination.with_suffix(".nfo"))
        await MediaItem.filter(id=ids[0]).update(
            path=str(destination),
            name="New",
            nfo_path=str(destination.with_suffix(".nfo")),
        )
        return {
            str(old): str(destination),
            str(old.with_suffix(".nfo")): str(destination.with_suffix(".nfo")),
        }

    monkeypatch.setattr(watcher, "organize_items", organize)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(old), event_type="created"
            )
            # the queued event contains the old library instance
            await MediaLib.filter(id=lib.id).update(rename_template="{{title}}")
            await watcher.consume_event(event)
            item = await MediaItem.get(lib_id=lib.id)
            original_id = item.id
            assert configs == ["{{title}}"]
            params = fire.call_args.kwargs["bootparams"]
            assert params["item_id"] == item.id
            assert params["item_path"] == str(destination)
            assert params["item_name"] == "New"
            assert params["title"] == "New"
            assert params["nfo_path"] == str(destination.with_suffix(".nfo"))
            for kind, source, target in [
                ("moved", old, destination),
                ("created", destination, None),
                ("deleted", old, None),
            ]:
                event = await MediaEvent.create(
                    lib=lib,
                    src_path=str(source),
                    dest_path=str(target) if target else None,
                    event_type=kind,
                )
                await watcher.consume_event(event)
            assert (await MediaItem.get(lib_id=lib.id)).id == original_id
            assert fire.await_count == 1
            assert await MediaEvent.all().count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("suffix", [".mkv", ".nfo"])
def test_reused_move_source(tmp_path, monkeypatch, suffix):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    monkeypatch.setattr(
        KaloscopeConfig,
        "get",
        lambda: SimpleNamespace(filesystem_trash_mode=False),
    )
    source = tmp_path / "old.mkv"
    nfo = source.with_suffix(".nfo")
    source.write_bytes(b"first video")
    nfo.write_text("<movie><title>New</title><year>2026</year></movie>")

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
                rename_template="{{title}} ({{year}})",
            )
            await watcher.consume_event(
                await MediaEvent.create(
                    lib=lib, src_path=str(source), event_type="created"
                )
            )
            original = await MediaItem.get(lib=lib)
            destination = tmp_path / "New (2026).mkv"
            assert original.path == str(destination)
            assert not source.exists()
            source.write_bytes(b"second video")
            nfo_content = "<movie><title>Second</title></movie>"
            nfo.write_text(nfo_content)
            await watcher.consume_event(
                await MediaEvent.create(
                    lib=lib, src_path=str(source), event_type="created"
                )
            )
            recreated = await MediaItem.get(lib=lib, path=str(source))
            assert recreated.id != original.id
            assert recreated.nfo_mtime is not None
            user = await User.create(
                username="viewer", password="test", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user,
                rel_type=HistoryType.VIDEO,
                rel_id=recreated.id,
                position=42,
            )
            fire.reset_mock()

            await watcher.consume_event(
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(source.with_suffix(suffix)),
                    dest_path=str(destination.with_suffix(suffix)),
                    event_type="moved",
                )
            )

            current = await MediaItem.get_or_none(id=recreated.id)
            assert current is not None
            assert current.path == str(source)
            assert current.nfo_path == str(nfo)
            assert current.nfo_mtime == recreated.nfo_mtime
            assert (await MediaItem.get(path=str(destination))).id == original.id
            assert await MediaItem.filter(lib=lib).count() == 2
            await history.refresh_from_db()
            assert history.rel_id == recreated.id
            assert history.position == 42
            assert source.read_bytes() == b"second video"
            assert destination.read_bytes() == b"first video"
            assert nfo.read_text() == nfo_content
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("suffix", [".mkv", ".nfo"])
def test_missing_move_source(tmp_path, monkeypatch, suffix):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr(FlowTriggerService, "fire", AsyncMock())
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    monkeypatch.setattr(
        KaloscopeConfig,
        "get",
        lambda: SimpleNamespace(filesystem_trash_mode=False),
    )
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>Movie</title></movie>")
    destination = tmp_path / "New.mkv"
    destination.write_bytes(b"video")
    destination_nfo = destination.with_suffix(".nfo")
    destination_nfo.write_text("<movie><title>Movie</title></movie>")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            for path in (source, destination):
                await watcher.consume_event(
                    await MediaEvent.create(
                        lib=lib, src_path=str(path), event_type="created"
                    )
                )
            original = await MediaItem.get(lib=lib, path=str(source))
            known = await MediaItem.get(lib=lib, path=str(destination))
            user = await User.create(
                username="viewer", password="test", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user, rel_type=HistoryType.VIDEO, rel_id=original.id, position=42
            )
            source.with_suffix(suffix).replace(destination.with_suffix(suffix))

            await watcher.consume_event(
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(source.with_suffix(suffix)),
                    dest_path=str(destination.with_suffix(suffix)),
                    event_type="moved",
                )
            )

            if suffix == ".mkv":
                assert not await MediaItem.filter(id=original.id).exists()
                assert not await UserHistory.filter(id=history.id).exists()
                assert not nfo.exists()
            else:
                await original.refresh_from_db()
                assert original.nfo_path is None
                assert original.nfo_mtime is None
                assert await UserHistory.filter(id=history.id).exists()
                assert source.is_file()
            current = await MediaItem.get(id=known.id)
            assert current.path == str(destination)
            assert current.nfo_path == str(destination_nfo)
            assert current.nfo_mtime is not None
            assert destination.is_file()
            assert destination_nfo.is_file()
            assert not await MediaEvent.filter(lib=lib).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("explicit_cache", [False, True])
@pytest.mark.parametrize("replacement", ["recreated", "moved", "temporary"])
@pytest.mark.parametrize(
    "content", [b"old video", b"new video", b"a longer replacement video"]
)
def test_replaced_video(tmp_path, monkeypatch, explicit_cache, replacement, content):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "Movie.mkv"
    source.write_bytes(b"old video")
    nfo = source.with_suffix(".nfo")
    nfo_content = "<movie><title>Movie</title></movie>"
    nfo.write_text(nfo_content)
    default_cache = tmp_path / ".Movie.json"
    default_cache.write_text("[]")
    cache = tmp_path / "custom.json" if explicit_cache else default_cache
    cache.write_text("[]")
    cached_meta = {"episode_id": 42}

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
                path=str(source),
                dir=str(tmp_path),
                name=source.stem,
                hash=hashlib.md5(source.read_bytes()).hexdigest(),
                size=source.stat().st_size,
                nfo_path=str(nfo),
                danmaku_path=str(cache) if explicit_cache else None,
                danmaku_meta=cached_meta,
            )
            events = []
            if replacement == "recreated":
                source.unlink()
                events.append(
                    await MediaEvent.create(
                        lib=lib, src_path=str(source), event_type="deleted"
                    )
                )
                source.write_bytes(content)
                events.append(
                    await MediaEvent.create(
                        lib=lib, src_path=str(source), event_type="created"
                    )
                )
            else:
                temporary = tmp_path / (
                    "Replacement.mkv" if replacement == "moved" else ".replacement.tmp"
                )
                temporary.write_bytes(content)
                temporary.replace(source)
                event = watcher.get_handler(lib.lib_type).filter_event(
                    FileMovedEvent(str(temporary), str(source)), base_path=lib.dir
                )
                events.append(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=event.src_path,
                        dest_path=event.dest_path,
                        event_type=event.event_type,
                    )
                )

            for event in events:
                await asyncio.wait_for(watcher.consume_event(event), timeout=3)

            current = await MediaItem.get(id=item.id)
            assert current.path == str(source)
            assert current.hash == hashlib.md5(content).hexdigest()
            assert current.size == len(content)
            assert current.nfo_path == str(nfo)
            assert nfo.read_text() == nfo_content
            if content == b"old video":
                assert cache.is_file()
                assert default_cache.is_file()
                assert current.danmaku_meta == cached_meta
                assert current.danmaku_path == (str(cache) if explicit_cache else None)
            else:
                assert not cache.exists()
                assert not default_cache.exists()
                assert current.danmaku_path is None
                assert current.danmaku_meta is None
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_ingest_shared_nfo(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    source.with_suffix(".nfo").write_text(
        "<movie><title>Film</title><year>2026</year></movie>"
    )

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
                rename_template="{{title}} ({{year}})/{{title}}",
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(source), event_type="created"
            )

            await watcher.consume_event(event)

            params = fire.call_args.kwargs["bootparams"]
            folder = tmp_path / "Film (2026)"
            assert params["item_path"] == str(folder / "Film.mkv")
            assert params["nfo_path"] == str(folder / "Film (2026).nfo")
            assert Path(params["nfo_path"]).is_file()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("missing_parent", [False, True])
def test_missing_nfo(tmp_path, monkeypatch, missing_parent):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    monkeypatch.setattr(watcher, "recover_organizing", AsyncMock(return_value={}))
    monkeypatch.setattr(watcher, "organize_items", AsyncMock(return_value={}))
    folder = tmp_path / "Series"
    folder.mkdir()
    nfo = folder / "Series.nfo"
    if not missing_parent:
        nfo.write_text("<tvshow><title>Series</title></tvshow>")
    video = folder / "S01E01.mkv"
    video.write_bytes(b"video")
    if missing_parent:
        video.with_suffix(".nfo").write_text(
            "<episodedetails><title>Pilot</title><season>1</season>"
            "<episode>1</episode></episodedetails>"
        )

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
                path=str(folder),
                dir=str(folder),
                name=folder.name,
                title="Series",
                nfo_path=None if missing_parent else str(nfo),
                season=1,
            )
            child = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(folder),
                name=video.stem,
                nfo_path=str(video.with_suffix(".nfo")) if missing_parent else None,
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(video), event_type="created"
            )
            await watcher.consume_event(event)
            params = [call.kwargs["bootparams"] for call in fire.call_args_list]
            expected_id = parent.id if missing_parent else child.id
            expected_type = "tvshow" if missing_parent else "episode"
            assert any(
                p["item_id"] == expected_id and p["nfo_type"] == expected_type
                for p in params
            )
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_synchronous_ingest(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"video")

    async def fire(*_args, bootparams):
        assert await shelver.gen_nfo(
            bootparams["nfo_type"],
            bootparams["nfo_path"],
            {"title": "Movie"},
            item_id=bootparams["item_id"],
        )

    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(video), event_type="created"
            )
            await asyncio.wait_for(watcher.consume_event(event), timeout=3)
            assert video.with_suffix(".nfo").is_file()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("lib_type", "source_name", "template", "target_name"),
    [
        (LibType.MOVIE, "old.mkv", "{{title}}/{{title}}", "Film/Film.mkv"),
        (LibType.MOVIE, "Old/old.mkv", "{{title}}", "Film.mkv"),
        (
            LibType.TV_SHOW,
            "Old/S01E02.mkv",
            "{{show_title}}/Season {{season}}/{{episode_code}} - {{title}}",
            "Series/Season 01/S01E02 - Pilot.mkv",
        ),
        (
            LibType.TV_SHOW,
            "Old/Season 01/S01E02.mkv",
            "{{show_title}}/{{episode_code}} - {{title}}",
            "Series/S01E02 - Pilot.mkv",
        ),
    ],
)
def test_organization_rescan(
    tmp_path, monkeypatch, lib_type, source_name, template, target_name
):
    """Exercise actual scanning, metadata parsing, organization and late events."""
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    source = tmp_path / source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"video")
    parent_nfo = (
        source.with_suffix(".nfo")
        if source.parent == tmp_path
        else source.parent / f"{source.parent.name}.nfo"
    )
    tag = "movie" if lib_type == LibType.MOVIE else "tvshow"
    title = "Film" if lib_type == LibType.MOVIE else "Series"
    parent_nfo.write_text(
        f"<{tag}><title>{title}</title><year>2026</year>"
        '<uniqueid type="tmdb" default="true">42</uniqueid></' + tag + ">"
    )
    if lib_type == LibType.TV_SHOW:
        source.with_suffix(".nfo").write_text(
            "<episodedetails><title>Pilot</title><season>1</season>"
            "<episode>2</episode></episodedetails>"
        )

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Library", dir=str(tmp_path), lib_type=lib_type, priority=1
            )
            monitor = watcher.LibWatcher(None)
            events = Queue()
            monitor._observers = {lib.dir: (None, events)}
            monitor._scanning_paths = []

            async def scan():
                await monitor.scan_directory(lib)
                while not events.empty():
                    await watcher.consume_event(events.get_nowait())

            await scan()
            item = await MediaItem.get(lib_id=lib.id, path=str(source))
            original_id = item.id
            fire.reset_mock()
            lib.rename_template = template
            await lib.save(update_fields=["rename_template"])
            await scan()
            await item.refresh_from_db()
            assert item.path == str(source)
            fire.assert_not_awaited()

            event = await MediaEvent.create(
                lib=lib, src_path=str(parent_nfo), event_type="modified"
            )
            await watcher.consume_event(event)
            destination = tmp_path / target_name
            await item.refresh_from_db()
            assert item.path == str(destination)
            assert destination.read_bytes() == b"video"
            # watchdog can deliver these after the journal transaction commits
            for kind, origin, target in [
                ("moved", source, destination),
                ("deleted", source, None),
                ("created", destination, None),
            ]:
                await watcher.consume_event(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=str(origin),
                        dest_path=str(target) if target else None,
                        event_type=kind,
                    )
                )
            await scan()
            await scan()
            assert (await MediaItem.get(path=str(destination))).id == original_id
            assert not await MediaEvent.filter(lib_id=lib.id).exists()
            assert all(
                Path(row.path).exists() for row in await MediaItem.filter(lib_id=lib.id)
            )
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("season_nfo", [False, True])
def test_unindexed_season(tmp_path, monkeypatch, season_nfo):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr(FlowTriggerService, "fire", AsyncMock())
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    source = tmp_path / "Old"
    nested = source / "Season 02"
    nested.mkdir(parents=True)
    (source / "Old.nfo").write_text("<tvshow><title>Series</title></tvshow>")
    (source / "S01E01.mkv").write_bytes(b"first season")
    (nested / "S02E01.mkv").write_bytes(b"second season")
    if season_nfo:
        (nested / "Season 02.nfo").write_text("<tvshow><title>Series</title></tvshow>")
    artwork = source / "art"
    artwork.mkdir()
    (artwork / "poster.jpg").write_bytes(b"poster")

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/Season {{season}}/{{episode_code}}",
            )
            monitor = watcher.LibWatcher(None)
            events = Queue()
            monitor._observers = {lib.dir: (None, events)}
            monitor._scanning_paths = []

            async def scan():
                await monitor.scan_directory(lib)
                while not events.empty():
                    await watcher.consume_event(events.get_nowait())

            await scan()

            first = tmp_path / "Series/Season 01/S01E01.mkv"
            second = (
                tmp_path / "Series/Season 02/S02E01.mkv"
                if season_nfo
                else nested / "S02E01.mkv"
            )
            assert first.read_bytes() == b"first season"
            assert second.read_bytes() == b"second season"
            assert set(tmp_path.rglob("*.mkv")) == {first, second}
            assert (first.parent / "art/poster.jpg").read_bytes() == b"poster"
            items = await MediaItem.filter(lib_id=lib.id, parent_id__not_isnull=True)
            ids = {item.path: item.id for item in items}
            assert set(ids) == {str(first), str(second)}

            await scan()

            items = await MediaItem.filter(lib_id=lib.id, parent_id__not_isnull=True)
            assert {item.path: item.id for item in items} == ids
            assert not await MediaEvent.filter(lib_id=lib.id).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_pending_startup(tmp_path, monkeypatch):
    tasks = []
    observer = Mock()
    monkeypatch.setattr(watcher, "Observer", observer)

    async def recover(lib):
        if lib.name == "Pending":
            raise watcher.OrganizePendingError("destination replaced externally")
        return {}

    monkeypatch.setattr(watcher, "recover_organizing", recover)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            libraries = []
            for priority, name in enumerate(("Pending", "Healthy"), start=1):
                directory = tmp_path / name
                directory.mkdir()
                libraries.append(
                    await MediaLib.create(
                        name=name,
                        dir=str(directory),
                        lib_type=LibType.MOVIE,
                        priority=priority,
                    )
                )
            pending_lib = libraries[0]
            event = await MediaEvent.create(
                lib=pending_lib,
                src_path=str(Path(pending_lib.dir) / "old.mkv"),
                event_type="deleted",
            )
            item = await MediaItem.create(
                lib=pending_lib,
                dir=pending_lib.dir,
                path=event.src_path,
                name="old",
            )
            app = SimpleNamespace(
                loop=asyncio.get_running_loop(),
                add_task=lambda task, **kwargs: tasks.append(task),
            )
            monitor = watcher.LibWatcher(app)
            monitor._watcher_lock = Mock()
            monitor._observing_paths = []
            monitor._scanning_paths = []
            monitor._observers = {}
            await monitor.start()
            assert set(monitor._observing_paths) == {lib.dir for lib in libraries}
            assert observer.call_count == 2
            # the pending library stays blocked without deleting its stale rows
            with pytest.raises(watcher.OrganizePendingError):
                await watcher.consume_event(event)
            with pytest.raises(watcher.OrganizePendingError):
                await monitor.scan_directory(pending_lib)
            assert await MediaItem.filter(id=item.id).exists()
            assert await MediaEvent.filter(id=event.id).exists()
        finally:
            for task in tasks:
                task.close()
            await Tortoise.close_connections()

    asyncio.run(run())


def test_pending_actions(tmp_path, monkeypatch):
    pending, healthy, removed = (
        str(tmp_path / name) for name in ("Pending", "Healthy", "Removed")
    )
    monitor = watcher.LibWatcher(None)
    monitor._watcher_actions = {
        pending: watcher.LibAction.SCAN,
        healthy: watcher.LibAction.SCAN,
        removed: watcher.LibAction.REMOVE,
    }
    monitor._observers = {path: (None, Queue()) for path in monitor._watcher_actions}
    rounds = []

    async def scan(path):
        if path == pending and not rounds:
            raise watcher.OrganizePendingError("destination replaced externally")

    async def pause(delay):
        rounds.append((delay, dict(monitor._watcher_actions)))
        if len(rounds) == 2:
            raise asyncio.CancelledError

    scan_action = AsyncMock(side_effect=scan)
    remove_action = AsyncMock()
    monkeypatch.setattr(monitor, "scan_directory", scan_action)
    monkeypatch.setattr(monitor, "remove_observer", remove_action)
    monkeypatch.setattr(watcher.asyncio, "sleep", pause)

    asyncio.run(monitor._listener())

    assert rounds == [(5, {pending: watcher.LibAction.SCAN}), (10, {})]
    assert [call.args[0] for call in scan_action.await_args_list] == [
        pending,
        healthy,
        pending,
    ]
    remove_action.assert_awaited_once_with(removed)


def test_persisted_event(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

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
            source = tmp_path / "Old.mkv"
            source.write_bytes(b"video")
            item = await MediaItem.create(
                lib=lib, path=str(source), dir=lib.dir, name="Old"
            )
            assert await shelver.gen_nfo(
                "movie",
                str(source.with_suffix(".nfo")),
                {"title": "New"},
                item_id=item.id,
                fallback={"title": "New"},
            )
            consume = watcher.consume_event

            async def finish(event):
                await consume(event)
                raise asyncio.CancelledError

            monkeypatch.setattr(watcher, "consume_event", finish)
            monitor = watcher.LibWatcher(None)
            await asyncio.wait_for(monitor._event_consumer(lib.id, Queue()), 3)

            await item.refresh_from_db()
            assert item.path == str(tmp_path / "New.mkv")
            assert Path(item.path).read_bytes() == b"video"
            assert not source.exists()
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_deferred_events(tmp_path, monkeypatch):
    attempts = []
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            pending = await MediaEvent.create(
                lib=lib,
                src_path=str(tmp_path / "Pending.nfo"),
                event_type="ingest",
                payload={"bootparams": [], "organize_ids": [1]},
            )
            events = Queue()
            events.put(pending)

            async def organize(current_lib, ids):
                attempts.append(ids)
                if ids == [1]:
                    if len(attempts) == 1:
                        await MediaEvent.create(
                            lib=current_lib,
                            src_path=str(tmp_path / "Ready.nfo"),
                            event_type="ingest",
                            payload={"bootparams": [], "organize_ids": [2]},
                        )
                    raise watcher.OrganizeDeferredError("pending transfer")
                return {}

            consume = watcher.consume_event

            async def finish(event):
                await consume(event)
                raise asyncio.CancelledError

            monkeypatch.setattr(watcher, "organize_items", organize)
            monkeypatch.setattr(watcher, "consume_event", finish)
            monitor = watcher.LibWatcher(None)
            await asyncio.wait_for(monitor._event_consumer(lib.id, events), 5)

            await pending.refresh_from_db()
            assert attempts == [[1], [1], [2]]
            assert pending.payload["organize_ids"] == [1]
            assert await MediaEvent.filter(lib=lib).count() == 1
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("interruption", ["organization", "workflow"])
def test_ingest_recovery(tmp_path, monkeypatch, restart, interruption):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr(MediaItemService, "_hash_and_size", AsyncMock())
    source = tmp_path / "Old" / "S01E01.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    (source.parent / "Old.nfo").write_text("<tvshow><title>Series</title></tvshow>")
    destination = tmp_path / "Series" / source.name
    move_files = organizer._move_files
    interrupted = False
    fired = []

    def move(root, payload):
        nonlocal interrupted
        if interruption == "organization" and not interrupted:
            interrupted = True
            raise OSError("temporary filesystem failure")
        return move_files(root, payload)

    async def fire(*_args, bootparams):
        nonlocal interrupted
        if bootparams["nfo_type"] == "episode":
            if interruption == "workflow" and not interrupted:
                interrupted = True
                raise RuntimeError("temporary workflow failure")
            body = {"title": "Pilot", "season": 1, "episode": 1}
            assert await shelver.gen_nfo(
                bootparams["nfo_type"],
                bootparams["nfo_path"],
                body,
                item_id=bootparams["item_id"],
                fallback=body,
            )
        fired.append(bootparams)

    monkeypatch.setattr(organizer, "_move_files", move)
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/{{episode_code}} - {{title}}",
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(source), event_type="created"
            )
            expected = (
                watcher.OrganizePendingError
                if interruption == "organization"
                else RuntimeError
            )
            with pytest.raises(expected):
                await watcher.consume_event(event)

            if restart:
                monitor = watcher.LibWatcher(None)
                events = await monitor._create_events(lib)
                while not events.empty():
                    await watcher.consume_event(events.get_nowait())
            else:
                await watcher.consume_event(event)

            assert destination.is_file()
            assert destination.with_suffix(".nfo").is_file()
            assert len(fired) == 2
            episode = next(
                params for params in fired if params["nfo_type"] == "episode"
            )
            assert episode["item_path"] == str(destination)
            assert episode["nfo_path"] == str(destination.with_suffix(".nfo"))
            assert episode["title"] == "Series"
            for kind, origin, target in [
                ("moved", source, destination),
                ("created", destination, None),
                ("deleted", source, None),
            ]:
                await watcher.consume_event(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=str(origin),
                        dest_path=str(target) if target else None,
                        event_type=kind,
                    )
                )
            for pending in await MediaEvent.filter(lib=lib):
                await watcher.consume_event(pending)
            organized = destination.with_name("S01E01 - Pilot.mkv")
            assert organized.read_bytes() == b"video"
            assert organized.with_suffix(".nfo").is_file()
            assert len(fired) == 2
            assert await MediaEvent.filter(lib=lib).count() == 0
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("restart", [False, True])
def test_hash_recovery(tmp_path, monkeypatch, restart):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    source.with_suffix(".nfo").write_text("<movie><title>New</title></movie>")
    destination = tmp_path / "New.mkv"
    move_files = organizer._move_files
    hash_and_size = MediaItemService._hash_and_size
    blocked = True

    def move(root, payload):
        move_files(root, payload)
        if blocked:
            raise OSError("interrupted after moving files")

    monkeypatch.setattr(organizer, "_move_files", move)

    async def run():
        nonlocal blocked
        db_url = f"sqlite://{tmp_path / 'media.sqlite3'}"
        hash_started = asyncio.Event()
        hash_finished = asyncio.Event()
        hash_tasks = []

        async def track_hash(item_id):
            hash_tasks.append(asyncio.current_task())
            hash_started.set()
            try:
                if restart:
                    await asyncio.Event().wait()
                await hash_and_size(item_id)
            finally:
                hash_finished.set()

        monkeypatch.setattr(MediaItemService, "_hash_and_size", track_hash)
        await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Movies",
                dir=str(tmp_path),
                lib_type=LibType.MOVIE,
                priority=1,
                rename_template="{{title}}",
            )
            event = await MediaEvent.create(
                lib=lib, src_path=str(source), event_type="created"
            )

            with pytest.raises(watcher.OrganizePendingError):
                await watcher.consume_event(event)
            await asyncio.wait_for(hash_started.wait(), timeout=3)
            if restart:
                for task in hash_tasks:
                    task.cancel()
                await asyncio.gather(*hash_tasks, return_exceptions=True)
                await Tortoise.close_connections()
                await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
                lib = await MediaLib.get(id=lib.id)
            else:
                await asyncio.wait_for(hash_finished.wait(), timeout=3)

            item = await MediaItem.get(lib=lib)
            original_id = item.id
            assert item.hash is None and item.size is None
            assert item.path == str(source)
            assert destination.is_file() and not source.exists()
            with pytest.raises(watcher.OrganizePendingError):
                await watcher.consume_event(event)
            fire.assert_not_awaited()

            blocked = False
            monitor = watcher.LibWatcher(None)
            events = await monitor._create_events(lib)
            while not events.empty():
                await watcher.consume_event(events.get_nowait())

            await item.refresh_from_db()
            assert item.id == original_id
            assert item.path == str(destination)
            assert item.hash == hashlib.md5(b"video").hexdigest()
            assert item.size == len(b"video")
            assert not await MediaEvent.filter(lib=lib).exists()
            assert fire.await_count == 1
        finally:
            for task in hash_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*hash_tasks, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("backfill_nfo_events", [False, True])
@pytest.mark.parametrize("missing", ["hash", "size", "both"])
def test_hash_scan(tmp_path, monkeypatch, backfill_nfo_events, missing):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)
    source = tmp_path / "old.mkv"
    source.write_bytes(b"video")
    digest = hashlib.md5(b"video").hexdigest()
    nfo = source.with_suffix(".nfo")
    nfo.write_text("<movie><title>New</title></movie>")
    cache = tmp_path / "custom.json"
    cache.write_text("[]")
    cached_meta = {"episode_id": 42}

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
            item = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(tmp_path),
                name=source.stem,
                hash=digest if missing == "size" else None,
                size=len(b"video") if missing == "hash" else None,
                danmaku_path=str(cache),
                danmaku_meta=cached_meta,
            )
            await shelver.update_metadata(lib, nfo)
            monitor = watcher.LibWatcher(None)
            events = Queue()
            monitor._observers = {lib.dir: (None, events)}
            monitor._scanning_paths = []

            await monitor.scan_directory(lib, backfill_nfo_events=backfill_nfo_events)
            while not events.empty():
                await watcher.consume_event(events.get_nowait())

            await item.refresh_from_db()
            assert item.path == str(source)
            assert item.hash == digest
            assert item.size == len(b"video")
            assert item.danmaku_path == str(cache)
            assert item.danmaku_meta == cached_meta
            assert cache.read_text() == "[]"
            assert not await MediaEvent.filter(lib=lib).exists()
            fire.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
