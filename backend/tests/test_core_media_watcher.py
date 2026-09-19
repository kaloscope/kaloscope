"""Organization integrates with the watcher without recreating media records."""

import asyncio
import mimetypes
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.constants import NFO_MIME_TYPE
from app.core.media import shelver, watcher
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
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
