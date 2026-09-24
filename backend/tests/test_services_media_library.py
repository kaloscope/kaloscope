"""Unit tests for media library services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import BadRequestException, ErrorCode, KaloscopeException
from app.core.media.coordination import library_lock
from app.core.media.watcher import LibWatcher
from app.models.flow import GraphCategory
from app.models.media import LibType, MediaEvent, MediaLib, MediaLibUpsert
from app.services.flow import FlowTriggerService
from app.services.media import MediaLibService


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


@pytest.mark.parametrize("relationship", ["same", "parent", "child"])
@pytest.mark.parametrize("existing_alias", [False, True])
def test_directory_alias(tmp_path, library_services, relationship, existing_alias):
    observer, bind = library_services
    watcher = object.__new__(LibWatcher)
    observer.side_effect = watcher._create_events
    directory = tmp_path / "library"
    directory.mkdir()
    child = directory / "child"
    child.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    existing, requested = (alias, directory) if existing_alias else (directory, alias)
    if relationship == "parent":
        existing /= "child"
    elif relationship == "child":
        requested /= "child"

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            library = await MediaLib.create(
                name="Existing", dir=str(existing), lib_type=LibType.MOVIE, priority=1
            )
            async with library_lock(library.dir):
                with pytest.raises(
                    KaloscopeException, match=ErrorCode.DUPLICATE_DIRECTORY
                ):
                    await asyncio.wait_for(
                        MediaLibService.upsert(
                            MediaLibUpsert(
                                name="Alias",
                                dir=str(requested),
                                lib_type=LibType.MOVIE,
                                danmaku_ttl=24,
                            )
                        ),
                        timeout=2,
                    )

            assert await MediaLib.all().count() == 1
            observer.assert_not_awaited()
            bind.assert_not_awaited()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("template", ["../{{title}}", "x" * 1025])
def test_template_invalid(tmp_path, template):
    with pytest.raises(ValidationError):
        MediaLibUpsert(
            dir=str(tmp_path),
            lib_type=LibType.MOVIE,
            name="Library",
            rename_template=template,
        )


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
    ("lib_type", "initial_template", "invalid_template", "request_type"),
    [
        (
            LibType.MOVIE,
            "{{title}}",
            "{{show_title}}/{{season}}/{{episode_code}}",
            None,
        ),
        (LibType.TV_SHOW, "{{show_title}}/{{episode_code}}", "{{title}}", None),
        (
            LibType.TV_SHOW,
            "{{show_title}}/{{episode_code}}",
            "{{title}}/{{episode_code}}",
            None,
        ),
        (
            LibType.MOVIE,
            "{{title}}",
            "{{show_title}}/{{season}}/{{episode_code}}",
            LibType.TV_SHOW,
        ),
        (
            LibType.TV_SHOW,
            "{{show_title}}/{{episode_code}}",
            "{{title}}",
            LibType.MOVIE,
        ),
    ],
)
def test_template_lib_type(
    tmp_path,
    library_services,
    lib_type,
    initial_template,
    invalid_template,
    request_type,
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
                lib_type=request_type,
                name="Invalid edit",
                danmaku_ttl=24,
                rename_template=invalid_template,
            )
            assert request.lib_type is request_type
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


def test_delete_wait(tmp_path, monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        tasks = []
        try:
            library = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            event = await MediaEvent.create(
                lib=library, src_path=str(tmp_path / "movie.mkv"), event_type="organize"
            )
            waiting = asyncio.Event()
            stopping = asyncio.Event()
            removed = []

            async def cleanup():
                await stopping.wait()
                async with await library_lock(library.dir).acquire(timeout=0):
                    assert not await MediaLib.filter(id=library.id).exists()
                    assert not await MediaEvent.filter(id=event.id).exists()

            consumer = asyncio.create_task(cleanup())
            tasks.append(consumer)

            async def remove_observer(directory):
                stopping.set()
                await consumer
                removed.append(directory)

            def waiting_lock(directory):
                waiting.set()
                return library_lock(directory)

            context = SimpleNamespace(
                lib_watcher=SimpleNamespace(remove_observer=remove_observer)
            )
            monkeypatch.setattr(
                MediaLibService, "app_ctx", classmethod(lambda cls: context)
            )
            monkeypatch.setattr(
                "app.services.media.library_lock", waiting_lock, raising=False
            )

            async with library_lock(library.dir):
                deletion = asyncio.create_task(MediaLibService.delete(library.id))
                tasks.append(deletion)
                await asyncio.wait_for(waiting.wait(), timeout=2)
                assert not deletion.done()
                updated = await asyncio.wait_for(
                    MediaEvent.filter(id=event.id).update(payload={"complete": True}),
                    timeout=2,
                )
                assert updated == 1
                assert not stopping.is_set()
            await asyncio.wait_for(deletion, timeout=3)

            assert removed == [library.dir]
            assert consumer.done()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())
