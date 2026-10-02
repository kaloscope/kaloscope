"""Unit tests for media content fields and serialization."""

import asyncio

import pytest
from pydantic import ValidationError
from tortoise import Tortoise

from app.models.media import (
    IndexState,
    LibType,
    MediaFormat,
    MediaItem,
    MediaLib,
    MediaLibUpsert,
)
from app.models.user import HistoryEntry, HistoryQuery, HistoryType, User, UserHistory
from app.services.media import MediaItemService
from app.services.user import UserHistoryService


@pytest.mark.parametrize("media_format", [MediaFormat.CBZ, MediaFormat.DIR])
def test_content_fields(media_format):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            lib = await MediaLib.create(
                name="Library", dir="/media", lib_type=LibType.COMIC, priority=1
            )
            parent = await MediaItem.create(
                lib=lib, dir="/media/work", path="/media/work", name="Work"
            )
            child = await MediaItem.create(
                lib=lib,
                parent=parent,
                dir="/media/work",
                path="/media/work/source",
                name="Source",
            )
            extra = {"schema_version": 1, "content": {"page_count": 24}}
            await MediaItem.filter(id=child.id).update(
                format=media_format,
                extra=extra,
                index_version="a" * 64,
                index_state=IndexState.READY,
            )
            await child.refresh_from_db()
            assert child.format is media_format
            assert child.index_state is IndexState.READY
            assert child.extra == extra
            assert child.index_version == "a" * 64
            assert child.index_error is None

            # reject invalid enum values before they can break later reads
            for field, value in (("format", "pdf"), ("index_state", "invalid")):
                with pytest.raises(ValueError):
                    await MediaItem.filter(id=child.id).update(**{field: value})
            await child.refresh_from_db()
            assert child.format is media_format
            assert child.index_state is IndexState.READY

            await MediaItem.filter(id=child.id).update(
                index_state=IndexState.ERROR, index_error="invalid_archive"
            )
            page = await MediaItemService.dump_page(
                await MediaItem.page(), exclude={"lib", "parent", "children"}
            )
            assert all("extra" not in item for item in page["items"])
            saved = next(item for item in page["items"] if item["id"] == child.id)
            assert saved["format"] == media_format.value
            assert saved["index_state"] == "error"
            assert saved["index_error"] == "invalid_archive"
            assert saved["index_version"] == "a" * 64

            detail = await MediaItemService.dump(parent)
            assert "extra" not in detail
            assert "extra" not in detail["children"][0]
            assert detail["format"] is None
            assert detail["index_state"] is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_library_inputs(tmp_path, lib_type):
    with pytest.raises(ValidationError):
        MediaLibUpsert(lib_type=lib_type, dir=str(tmp_path), name="Library")


@pytest.mark.parametrize("rel_type", [HistoryType.TEXT, HistoryType.IMAGE])
def test_reading_inputs(rel_type):
    with pytest.raises(ValidationError):
        HistoryEntry(rel_type=rel_type, rel_id=1)
    with pytest.raises(ValidationError):
        HistoryQuery(rel_type=rel_type)


def test_reading_locator():
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            user = await User.create(username="reader", password="test", role="user")
            for index, (lib_type, rel_type) in enumerate(
                ((LibType.NOVEL, HistoryType.TEXT), (LibType.COMIC, HistoryType.IMAGE)),
                start=1,
            ):
                lib = await MediaLib.create(
                    name=lib_type, dir=f"/{lib_type}", lib_type=lib_type, priority=index
                )
                item = await MediaItem.create(
                    lib=lib, dir=lib.dir, path=f"{lib.dir}/work", name="Work"
                )
                locator = {"schema_version": 1, "index_version": "a" * 64}
                if rel_type == HistoryType.TEXT:
                    locator.update(chapter_id="c1", block_id="b2", offset=8)
                else:
                    locator.update(chapter_item_id=None, page_id="p4", offset=0.5)
                history = await UserHistory.create(
                    user=user, rel_type=rel_type, rel_id=item.id, locator=locator
                )
                await history.refresh_from_db()
                assert history.rel_type is rel_type
                assert history.locator == locator
                assert history.position is None
                assert (await UserHistoryService.dump(history))["locator"] == locator
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
