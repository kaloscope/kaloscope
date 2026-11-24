"""Unit tests for media content fields and serialization."""

import asyncio

import pytest
from pydantic import ValidationError

from app.models.media import (
    IndexState,
    LibType,
    MediaFormat,
    MediaItem,
    MediaLib,
    MediaLibUpsert,
)
from app.models.user import HistoryEntry, HistoryQuery, HistoryType
from app.services.media import MediaItemService
from tests.helpers import memory_db


@pytest.mark.parametrize("media_format", [MediaFormat.CBZ, MediaFormat.DIR])
def test_content_fields(media_format):
    async def run():
        async with memory_db():
            lib = await MediaLib.create(
                name="Library", dir="/media", lib_type=LibType.COMIC, priority=1
            )
            parent = await MediaItem.create(
                lib=lib, dir="/media/work", path="/media/work", name="Work"
            )
            original = {
                "path": "/media/original/source",
                "type": "file",
                "size": 1024,
                "mtime": "2026-10-12T00:00:00+00:00",
                "link_target": None,
            }
            child = await MediaItem.create(
                lib=lib,
                parent=parent,
                dir="/media/work",
                path="/media/work/source",
                name="Source",
                original=original,
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
            assert saved["original"] == original
            assert saved["format"] == media_format.value
            assert saved["index_state"] == "error"
            assert saved["index_error"] == "invalid_archive"
            assert saved["index_version"] == "a" * 64

            detail = await MediaItemService.dump(parent)
            assert detail["original"] is None
            assert detail["children"][0]["original"] == original
            assert "extra" not in detail
            assert "extra" not in detail["children"][0]
            assert detail["format"] is None
            assert detail["index_state"] is None

    asyncio.run(run())


def test_library_type_required(tmp_path):
    with pytest.raises(ValidationError, match="library type is required"):
        MediaLibUpsert(dir=str(tmp_path), name="Library")
    assert MediaLibUpsert(id=1, name="Edited").lib_type is None


@pytest.mark.parametrize("lib_type", [LibType.NOVEL, LibType.COMIC])
def test_reading_library_alias(tmp_path, lib_type):
    directory = tmp_path / "Library"
    directory.mkdir()
    alias = tmp_path / "Alias"
    alias.symlink_to(directory, target_is_directory=True)
    request = MediaLibUpsert(lib_type=lib_type, dir=str(alias), name="Library")
    assert request.dir == str(directory.resolve())


@pytest.mark.parametrize("rel_type", [HistoryType.TEXT, HistoryType.IMAGE])
def test_reading_inputs(rel_type):
    with pytest.raises(ValidationError):
        HistoryEntry(rel_type=rel_type, rel_id=1)
    assert HistoryQuery(rel_type=rel_type, rel_id=1).rel_id == 1
