"""Unit tests for media library services."""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist

from app.core.config import KaloscopeConfig
from app.core.exceptions import BadRequestException, ErrorCode, KaloscopeException
from app.core.media.coordination import library_lock
from app.core.media.watcher import LibWatcher
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


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("layout", ["same", "split", "merged", "flat", "removed"])
def test_item_delete_scope(tmp_path, monkeypatch, local, layout):
    def remove(path):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    monkeypatch.setattr("app.services.media.delete_path", remove)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        deletion = None
        try:
            lib = await MediaLib.create(
                name="Shows", dir=str(tmp_path), lib_type=LibType.TV_SHOW, priority=1
            )

            async def create_parent(name):
                directory = tmp_path / name
                directory.mkdir()
                return await MediaItem.create(
                    lib=lib, path=str(directory), dir=str(directory), name=name
                )

            async def create_child(parent, name):
                path = Path(parent.path) / f"{name}.mkv"
                path.write_bytes(name.encode())
                return await MediaItem.create(
                    lib=lib, parent=parent, path=str(path), dir=parent.path, name=name
                )

            parent = await create_parent("Original")
            selected = [
                await create_child(parent, "first"),
                await create_child(parent, "second"),
            ]
            other_parent = await create_parent("Other")
            untouched = [await create_child(other_parent, "other")]
            affected_parents = [parent]
            waiting = asyncio.Event()

            def waiting_lock(directory):
                waiting.set()
                return library_lock(directory)

            monkeypatch.setattr("app.services.media.library_lock", waiting_lock)
            async with library_lock(lib.dir):
                deletion = asyncio.create_task(
                    MediaItemService.delete(parent.id, local=local)
                )
                await asyncio.wait_for(waiting.wait(), timeout=3)
                assert not deletion.done()
                assert all(Path(child.path).is_file() for child in selected)

                if layout in {"split", "merged"}:
                    destination = (
                        other_parent
                        if layout == "merged"
                        else await create_parent("Split")
                    )
                    affected_parents.append(destination)
                    child = selected[1]
                    old_path = Path(child.path)
                    new_path = Path(destination.path) / old_path.name
                    old_path.rename(new_path)
                    child.path, child.dir = str(new_path), destination.path
                    child.parent_id = destination.id
                    await child.save(update_fields=["path", "dir", "parent_id"])
                    if layout == "merged":
                        incoming = await create_child(parent, "second")
                        Path(incoming.path).write_bytes(b"replacement")
                        untouched.append(incoming)
                elif layout == "flat":
                    for child in selected:
                        old_path = Path(child.path)
                        new_path = tmp_path / old_path.name
                        old_path.rename(new_path)
                        child.path, child.dir, child.parent_id = (
                            str(new_path),
                            str(tmp_path),
                            None,
                        )
                        await child.save(update_fields=["path", "dir", "parent_id"])
                    Path(parent.path).rmdir()
                    await parent.delete()
                elif layout == "removed":
                    await parent.delete()

            await asyncio.wait_for(deletion, timeout=3)

            for child in selected:
                current = await MediaItem.get_or_none(id=child.id)
                if layout == "removed":
                    assert current is None
                    assert Path(child.path).read_bytes() == child.name.encode()
                elif local:
                    assert current is None
                    assert not Path(child.path).exists()
                else:
                    assert current is not None
                    assert current.visible is False
                    assert Path(child.path).read_bytes() == child.name.encode()
            for affected in affected_parents:
                current = await MediaItem.get_or_none(id=affected.id)
                if layout in {"flat", "removed"} or (local and layout != "merged"):
                    assert current is None
                else:
                    assert current is not None
                    assert current.visible is (layout == "merged")
            for child in untouched:
                await child.refresh_from_db()
                assert child.visible is True
                expected = b"replacement" if child.name == "second" else b"other"
                assert Path(child.path).read_bytes() == expected
            await other_parent.refresh_from_db()
            assert other_parent.visible is True
        finally:
            if deletion is not None:
                if not deletion.done():
                    deletion.cancel()
                await asyncio.gather(deletion, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("local", [False, True])
def test_item_delete_missing(local):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            if local:
                with pytest.raises(DoesNotExist):
                    await MediaItemService.delete(1, local=True)
            else:
                assert await MediaItemService.delete(1) is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
