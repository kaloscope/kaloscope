"""Media library rename settings persist across current and older API requests."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import BadRequestException
from app.core.media import organizer
from app.core.media.coordination import library_lock
from app.models.flow import GraphCategory
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib, MediaLibUpsert
from app.services.flow import FlowTriggerService
from app.services.media import MediaItemService, MediaLibService


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


@pytest.fixture
def library_services(monkeypatch):
    observer = AsyncMock()
    bind = AsyncMock()
    context = SimpleNamespace(lib_watcher=SimpleNamespace(add_observer=observer))
    monkeypatch.setattr(MediaLibService, "app_ctx", classmethod(lambda cls: context))
    monkeypatch.setattr(FlowTriggerService, "bind_triggers", bind)
    return observer, bind


@pytest.mark.parametrize(
    ("lib_type", "initial_template", "edited_template"),
    [
        (LibType.MOVIE, "{{title}}", "{{title}} ({{year}})/{{title}}"),
        (
            LibType.TV_SHOW,
            "{{show_title}}/{{episode_code}}",
            "{{show_title}}/Season {{season}}/{{episode_code}} - {{title}}",
        ),
    ],
)
@pytest.mark.parametrize("cleared_template", [None, "", "   "])
def test_rename_template(
    tmp_path,
    library_services,
    lib_type,
    initial_template,
    edited_template,
    cleared_template,
):
    observer, bind = library_services

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            library = await MediaLibService.upsert(
                MediaLibUpsert(
                    dir=str(tmp_path),
                    lib_type=lib_type,
                    name="Library",
                    danmaku_ttl=24,
                    rename_template=f"  {initial_template}  ",
                )
            )
            assert library.rename_template == initial_template
            observer.assert_awaited_once_with(library)
            bind.assert_awaited_once_with(GraphCategory.INGEST, library.id, None)
            assert (await MediaLibService.dump(library))["rename_template"] == (
                initial_template
            )

            library = await MediaLibService.upsert(
                MediaLibUpsert(
                    id=library.id,
                    name="Edited",
                    danmaku_ttl=24,
                    rename_template=edited_template,
                )
            )
            assert library.name == "Edited"
            assert library.lib_type is lib_type
            assert library.rename_template == edited_template

            legacy_request = MediaLibUpsert(
                id=library.id, name="Legacy request", danmaku_ttl=24
            )
            assert "rename_template" not in legacy_request.model_fields_set
            library = await MediaLibService.upsert(legacy_request)
            assert library.rename_template == edited_template

            library = await MediaLibService.upsert(
                MediaLibUpsert(
                    id=library.id,
                    name="Disabled",
                    danmaku_ttl=24,
                    rename_template=cleared_template,
                )
            )
            await library.refresh_from_db()
            assert library.rename_template is None
            assert await MediaLib.all().count() == 1
            observer.assert_awaited_once()
            assert bind.await_count == 4
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("lib_type", "initial_template", "invalid_template"),
    [
        (LibType.MOVIE, "{{title}}", "{{show_title}}/{{season}}/{{episode_code}}"),
        (LibType.TV_SHOW, "{{show_title}}/{{episode_code}}", "{{title}}"),
        (
            LibType.TV_SHOW,
            "{{show_title}}/{{episode_code}}",
            "{{title}}/{{episode_code}}",
        ),
    ],
)
def test_template_lib_type(
    tmp_path, library_services, lib_type, initial_template, invalid_template
):
    observer, bind = library_services

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            library = await MediaLib.create(
                dir=str(tmp_path),
                lib_type=lib_type,
                name="Existing library",
                priority=1,
                rename_template=initial_template,
            )
            request = MediaLibUpsert(
                id=library.id,
                name="Invalid edit",
                danmaku_ttl=24,
                rename_template=invalid_template,
            )
            assert request.lib_type is None
            with pytest.raises(BadRequestException):
                await MediaLibService.upsert(request)
            await library.refresh_from_db()
            assert library.rename_template == initial_template
            assert library.name == "Existing library"
            observer.assert_not_awaited()
            bind.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("delete_parent", [False, True])
@pytest.mark.parametrize("pending_recovery", [False, True])
def test_local_delete_lock(tmp_path, monkeypatch, delete_parent, pending_recovery):
    deleted_paths = []

    def delete(path):
        deleted_paths.append(path)
        path.unlink()

    monkeypatch.setattr("app.services.media.delete_path", delete)

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
            source_dir = tmp_path / "Original" if delete_parent else tmp_path
            source_dir.mkdir(exist_ok=True)
            video = source_dir / "original.mkv"
            video.write_bytes(b"video")
            nfo = (
                source_dir / "Original.nfo"
                if delete_parent
                else video.with_suffix(".nfo")
            )
            nfo.write_text("<movie><title>New</title></movie>")
            parent = (
                await MediaItem.create(
                    lib=lib,
                    path=str(source_dir),
                    dir=str(source_dir),
                    name=source_dir.name,
                    nfo_path=str(nfo),
                )
                if delete_parent
                else None
            )
            item = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(source_dir),
                name=video.stem,
                nfo_path=None if parent else str(nfo),
            )
            waiting = asyncio.Event()

            def waiting_lock(directory):
                waiting.set()
                return library_lock(directory)

            monkeypatch.setattr("app.services.media.library_lock", waiting_lock)
            async with library_lock(lib.dir):
                task = asyncio.create_task(
                    MediaItemService.delete(
                        parent.id if parent else item.id, local=True
                    )
                )
                try:
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    assert not task.done()
                    assert video.is_file()
                    if pending_recovery:
                        payload = await organizer._plan(lib, [item], parent)
                        await MediaEvent.create(
                            lib=lib,
                            event_type="organize",
                            src_path=item.path,
                            payload=payload,
                        )
                    else:
                        await organizer.organize_items(lib, [item.id])
                        assert not video.exists()
                        assert (tmp_path / "New.mkv").is_file()
                except BaseException:
                    task.cancel()
                    raise
            await asyncio.wait_for(task, timeout=3)
            assert deleted_paths == [tmp_path / "New.mkv"]
            assert not await MediaItem.filter(id=item.id).exists()
            assert not await MediaEvent.filter(event_type="organize").exists()
            assert not (tmp_path / "New.mkv").exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_library_delete_lock(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    move_files = organizer._move_files

    def delayed_move(root, payload):
        started.set()
        assert release.wait(timeout=5)
        try:
            move_files(root, payload)
        finally:
            finished.set()

    monkeypatch.setattr(organizer, "_move_files", delayed_move)

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
            video = tmp_path / "original.mkv"
            video.write_bytes(b"video")
            nfo = video.with_suffix(".nfo")
            nfo.write_text("<movie><title>New</title></movie>")
            item = await MediaItem.create(
                lib=lib,
                path=str(video),
                dir=str(tmp_path),
                name=video.stem,
                nfo_path=str(nfo),
            )
            waiting = asyncio.Event()
            removed = []

            def waiting_lock(directory):
                waiting.set()
                return library_lock(directory)

            async def remove_observer(directory):
                assert finished.is_set()
                async with await library_lock(directory).acquire(timeout=0):
                    assert not await MediaLib.filter(id=lib.id).exists()
                removed.append(directory)

            context = SimpleNamespace(
                lib_watcher=SimpleNamespace(remove_observer=remove_observer)
            )
            monkeypatch.setattr(
                MediaLibService, "app_ctx", classmethod(lambda cls: context)
            )
            monkeypatch.setattr("app.services.media.library_lock", waiting_lock)

            async def organize():
                async with library_lock(lib.dir):
                    await organizer.organize_items(lib, [item.id])

            organization = asyncio.create_task(organize())
            deletion = None
            try:
                assert await asyncio.to_thread(started.wait, 3)
                deletion = asyncio.create_task(MediaLibService.delete(lib.id))
                await asyncio.wait_for(waiting.wait(), timeout=3)
                assert not deletion.done()
                assert not finished.is_set()
                assert await MediaEvent.filter(event_type="organize").exists()
            finally:
                release.set()
                await asyncio.wait_for(organization, timeout=3)
                if deletion is not None:
                    await asyncio.wait_for(deletion, timeout=3)
            assert removed == [lib.dir]
            assert not await MediaEvent.all().exists()
            assert not await MediaItem.all().exists()
            assert (tmp_path / "New.mkv").read_bytes() == b"video"
        finally:
            release.set()
            await Tortoise.close_connections()

    asyncio.run(run())


def test_hide_lock(tmp_path, monkeypatch):
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
                rename_template="{{title}}/{{title}}",
            )
            folder = tmp_path / "Old"
            folder.mkdir()
            video = folder / "old.mkv"
            video.write_bytes(b"video")
            nfo = folder / "Old.nfo"
            nfo.write_text("<movie><title>New</title></movie>")
            parent = await MediaItem.create(
                lib=lib,
                path=str(folder),
                dir=str(folder),
                name=folder.name,
                nfo_path=str(nfo),
            )
            item = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(folder),
                name=video.stem,
            )
            waiting = asyncio.Event()

            def waiting_lock(directory):
                waiting.set()
                return library_lock(directory)

            monkeypatch.setattr("app.services.media.library_lock", waiting_lock)
            async with library_lock(lib.dir):
                payload = await organizer._plan(lib, [item], parent)
                assert payload["parent"]["id"] == parent.id
                assert payload["parent"]["visible"] is True
                event = await MediaEvent.create(
                    lib=lib,
                    event_type="organize",
                    src_path=str(folder),
                    payload=payload,
                )
                task = asyncio.create_task(MediaItemService.delete(parent.id))
                try:
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    assert not task.done()
                    await organizer._finish(lib, event)
                except BaseException:
                    task.cancel()
                    raise
            await asyncio.wait_for(task, timeout=3)
            await parent.refresh_from_db()
            assert parent.path == str(tmp_path / "New")
            assert parent.visible is False
            assert (tmp_path / "New" / "New.mkv").read_bytes() == b"video"
            assert not await MediaEvent.filter(event_type="organize").exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
