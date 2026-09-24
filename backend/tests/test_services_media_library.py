"""Unit tests for media library services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import ErrorCode, KaloscopeException
from app.core.media.coordination import library_lock
from app.core.media.watcher import LibWatcher
from app.models.media import LibType, MediaLib, MediaLibUpsert
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
