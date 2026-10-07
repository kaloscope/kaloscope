"""Tests for reading ingestion, moves, removal, indexes and collection summaries."""

import asyncio
import json
import os
import shutil
import threading
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from filelock import Timeout
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist
from tortoise.expressions import Q
from tortoise.queryset import QuerySet

from app.core.config import KaloscopeConfig
from app.core.media import events as media_events
from app.core.media import watcher as media_watcher
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.epub.cache import EpubContent
from app.core.media.events import (
    ReadingReconcile,
    coalesce_reading_events,
    prepare_reading_event,
)
from app.core.media.handlers import reading as reading_handler
from app.core.media.handlers.base import get_handler
from app.core.media.handlers.reading import ReadingMediaHandler, ReadingSource
from app.core.media.image import load_image_index, read_image_resource
from app.core.media.text import load_text_index, read_text_chapter
from app.models.media import (
    IndexState,
    LibType,
    MediaEvent,
    MediaFormat,
    MediaItem,
    MediaLib,
)
from app.models.user import HistoryType, User, UserHistory, UserRole
from app.services import media as media_service
from app.services.media import MediaItemService

_PNG = b"\x89PNG\r\n\x1a\nimage"


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Redirect caches and library locks to the test directory.

    Args:
        tmp_path: The isolated test directory.
        monkeypatch: The fixture restoring configuration after the test.
    """
    directory = tmp_path / "cache"
    directory.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


@asynccontextmanager
async def _database() -> AsyncGenerator[None]:
    """Provide an isolated schema without starting application resources.

    Yields:
        Control while the in-memory database is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _source(
    tmp_path: Path, format: MediaFormat, *, chapter: bool = False
) -> tuple[MediaLib, ReadingSource]:
    """Create a library and real reading files without registering media items.

    Args:
        tmp_path: The isolated filesystem root.
        format: The body format to construct.
        chapter: Whether to create a comic chapter; defaults to a standalone work.

    Returns:
        The library and source, with a parent path for a comic chapter.
    """
    root = tmp_path / "Library"
    work = root / "Work"
    directory = work / "Chapter" if chapter else work
    directory.mkdir(parents=True)
    lib = await MediaLib.create(
        name="Reading",
        dir=str(root),
        priority=1,
        lib_type=LibType.NOVEL
        if format in (MediaFormat.TXT, MediaFormat.EPUB)
        else LibType.COMIC,
    )
    path = directory if format == MediaFormat.DIR else directory / f"Book.{format}"
    if format == MediaFormat.TXT:
        path.write_text("Body")
    elif format == MediaFormat.DIR:
        (path / "1.png").write_bytes(_PNG)
        (path / "cover.png").write_bytes(_PNG)
    else:
        with zipfile.ZipFile(path, "w") as archive:
            if format in (MediaFormat.CBZ, MediaFormat.ZIP):
                archive.writestr("1.png", _PNG)
            else:
                archive.writestr("mimetype", "application/epub+zip")
                archive.writestr(
                    "META-INF/container.xml",
                    "<container "
                    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                    'version="1.0"><rootfiles><rootfile full-path="book.opf" '
                    'media-type="application/oebps-package+xml"/></rootfiles></container>',
                )
                archive.writestr(
                    "book.opf",
                    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    "<dc:title>Embedded</dc:title></metadata>"
                    '<manifest><item id="body" href="body.xhtml" '
                    'media-type="application/xhtml+xml"/></manifest>'
                    '<spine><itemref idref="body"/></spine></package>',
                )
                archive.writestr(
                    "body.xhtml",
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    "<body><p>Body</p></body></html>",
                )
    return lib, ReadingSource(path, format, work if chapter else None)


async def _item(
    tmp_path: Path, format: MediaFormat, *, chapter: bool = False
) -> MediaItem:
    """Create a reading source and a directly seeded row for publication tests.

    Args:
        tmp_path: The isolated filesystem root.
        format: The body format to construct.
        chapter: Whether to create a comic chapter; defaults to a standalone work.

    Returns:
        The pending item with its library assigned.
    """
    lib, source = await _source(tmp_path, format, chapter=chapter)
    parent = (
        await MediaItem.create(
            lib=lib,
            path=str(source.parent_path),
            dir=str(source.parent_path),
            name="Work",
            format=None,
        )
        if chapter
        else None
    )
    return await MediaItem.create(
        lib=lib,
        parent=parent,
        path=str(source.path),
        dir=str(source.directory),
        name="Book",
        format=format,
        title="Saved title",
        year=2000,
        index_state=IndexState.PENDING,
        extra={"metadata_sync": {"state": "ready"}},
    )


def _cache(item: MediaItem) -> Path:
    """Locate an item's published cache for assertions.

    Args:
        item: A row with a published index version.

    Returns:
        The version directory in the isolated workspace.
    """
    assert item.index_version is not None
    return (
        Path(KaloscopeConfig.get_workspace("temp"))
        / "media_index"
        / str(item.id)
        / item.index_version
    )


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat]
    + [
        (format, True) for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
    ],
)
def test_ingest_work(tmp_path, format, chapter):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            before = {
                path: path.read_bytes() for path in work.rglob("*") if path.is_file()
            }
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            items = await MediaItem.filter(lib_id=lib.id).order_by("id")
            assert len(items) == (2 if chapter else 1)
            assert all(item.index_state == IndexState.READY for item in items)
            item = items[-1]
            assert item.path == str(source.path) and item.format == format
            assert item.title is not None and item.extra is not None
            if chapter:
                assert item.parent_id == items[0].id
                assert items[0].index_version is None
                assert items[0].extra is not None
                assert items[0].extra["content"]["chapter_count"] == 1
            cache = _cache(item)
            if format in (MediaFormat.TXT, MediaFormat.EPUB):
                index = load_text_index(cache)
                body = read_text_chapter(cache, index.chapters[0].id)
                assert (
                    body == ["Body"]
                    if format == MediaFormat.TXT
                    else isinstance(body, EpubContent)
                )
            else:
                index = load_image_index(cache)
                assert read_image_resource(source.path, cache, index.pages[0].id) == (
                    _PNG,
                    "image/png",
                )
            assert all(path.read_bytes() == data for path, data in before.items())
            saved = await MediaItem.all().values()
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            assert await MediaItem.all().values() == saved

    asyncio.run(run())


@pytest.mark.parametrize("format", list(MediaFormat))
def test_ingest_metadata(tmp_path, format):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            first = await MediaItem.get(lib_id=lib.id, path=str(source.path))
            await MediaItem.filter(id=first.id).update(visible=False)
            novel = lib.lib_type == LibType.NOVEL
            metadata = source.directory / ("metadata.opf" if novel else "ComicInfo.xml")
            metadata.write_text(
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<metadata><dc:title>Current</dc:title></metadata></package>"
                if novel
                else "<ComicInfo><Title>Current</Title></ComicInfo>"
            )
            assert not await MediaItemService.ingest_reading_work(
                lib.id, source.directory
            )
            current = await MediaItem.get(id=first.id)
            assert current.title == "Current" and not current.visible
            assert current.index_version == first.index_version
            assert current.extra is not None
            assert current.extra["metadata_sync"]["state"] == "ready"
            metadata.write_text("<broken")
            assert await MediaItemService.ingest_reading_work(
                lib.id, source.directory
            ) == {source.path: "invalid_metadata"}
            current = await MediaItem.get(id=first.id)
            assert (
                current.title == "Current" and current.index_state == IndexState.READY
            )
            assert current.index_version == first.index_version
            metadata.unlink()
            assert not await MediaItemService.ingest_reading_work(
                lib.id, source.directory
            )
            current = await MediaItem.get(id=first.id)
            assert current.title == first.title and not current.visible
            assert current.index_version == first.index_version
            assert current.extra is not None
            assert current.extra["metadata_sync"]["state"] == "none"

    asyncio.run(run())


def test_ingest_chapter_errors(tmp_path):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.parent_path
            assert work is not None
            ambiguous, broken, archived = [work / name for name in ("0", "1", "2")]
            for directory in (ambiguous, broken, archived):
                directory.mkdir()
            (ambiguous / "1.cbz").write_bytes(b"broken")
            (ambiguous / "2.cbz").write_bytes(b"broken")
            body = broken / "Book.cbz"
            body.write_bytes(b"broken")
            with zipfile.ZipFile(archived / "Book.zip", "w") as archive:
                archive.writestr("1.png", _PNG)
            metadata = work / "ComicInfo.xml"
            metadata.write_text("<broken")
            issues = await MediaItemService.ingest_reading_work(lib.id, work)
            assert issues == {
                ambiguous: "ambiguous_layout",
                work: "invalid_metadata",
                body: "invalid_archive",
            }
            collection = await MediaItem.get(lib_id=lib.id, path=str(work))
            assert collection.index_state == IndexState.READY
            assert collection.extra is not None
            assert collection.extra["metadata_sync"]["state"] == "error"
            assert collection.extra["content"]["chapter_count"] == 3
            failed = await MediaItem.get(lib_id=lib.id, path=str(body))
            assert failed.index_state == IndexState.ERROR
            assert failed.index_error == "invalid_archive"
            assert await MediaItem.filter(index_state=IndexState.READY).count() == 3
            assert not await MediaItem.filter(dir=str(ambiguous)).exists()
            metadata.unlink()
            (ambiguous / "1.cbz").unlink()
            (ambiguous / "2.cbz").unlink()
            with zipfile.ZipFile(body, "w") as archive:
                archive.writestr("1.png", _PNG)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            recovered = await MediaItem.get(id=failed.id)
            assert (
                recovered.index_state == IndexState.READY
                and recovered.index_error is None
            )

    asyncio.run(run())


@pytest.mark.parametrize("whole_work", [False, True])
def test_ingest_targets(tmp_path, whole_work):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.parent_path
            assert work is not None
            other = work / "Other"
            other.mkdir()
            (other / "1.png").write_bytes(_PNG)
            assert not await MediaItemService.ingest_reading_work(
                lib.id, work, targets={source.directory}
            )
            assert await MediaItem.all().count() == 2
            assert not await MediaItem.filter(path=str(other)).exists()
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            before = {item.path: item.index_version for item in await MediaItem.all()}
            target = work if whole_work else source.directory
            assert not await MediaItemService.ingest_reading_work(
                lib.id, work, targets={target}, force=True
            )
            after = {item.path: item.index_version for item in await MediaItem.all()}
            assert after[str(source.path)] != before[str(source.path)]
            assert (after[str(other)] != before[str(other)]) == whole_work
            assert after[str(work)] is None
            # unrelated layout failures must not fail a selected chapter update
            (other / "Book.zip").write_bytes(b"broken")
            assert not await MediaItemService.ingest_reading_work(
                lib.id, work, targets={source.directory}
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat]
    + [
        (format, True) for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
    ],
)
def test_ingest_body_events(tmp_path, monkeypatch, format, chapter):
    """Rebuild changed bodies even when the source size and mtime are preserved.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture disabling application notifications.
        format: The reading source format.
        chapter: Whether the source is a comic chapter.
    """
    monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            await MediaItemService.ingest_reading_work(lib.id, work)
            original = await MediaItem.get(lib_id=lib.id, path=str(source.path))
            path = source.path / "1.png" if format == MediaFormat.DIR else source.path
            before = path.stat()
            if format in (MediaFormat.TXT, MediaFormat.DIR):
                path.write_bytes(
                    path.read_bytes()
                    .replace(b"Body", b"Next")
                    .replace(b"image", b"other")
                )
            else:
                with zipfile.ZipFile(path) as archive:
                    members = [
                        (entry, archive.read(entry)) for entry in archive.infolist()
                    ]
                with zipfile.ZipFile(path, "w") as archive:
                    for entry, data in members:
                        archive.writestr(
                            entry,
                            data.replace(b"Body", b"Next").replace(b"image", b"other"),
                        )
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert path.stat().st_size == before.st_size
            await MediaEvent.create(lib=lib, src_path=str(path), event_type="modified")
            task = (await coalesce_reading_events(lib.id))[0]
            payload = ReadingReconcile.model_validate(task.payload)
            assert payload.force_targets == [str(source.directory)]
            assert not await MediaItemService.ingest_reading_work(
                lib.id,
                work,
                targets={Path(path) for path in payload.targets},
                force_targets={Path(path) for path in payload.force_targets},
            )
            current = await MediaItem.get(id=original.id)
            assert current.index_version != original.index_version
            assert current.index_state == IndexState.READY
            if format in (MediaFormat.TXT, MediaFormat.EPUB):
                index = load_text_index(_cache(current))
                content = read_text_chapter(_cache(current), index.chapters[0].id)
                if format == MediaFormat.TXT:
                    assert content == ["Next"]
                else:
                    assert isinstance(content, EpubContent)
                    assert '"Next"' in content.model_dump_json()
            else:
                index = load_image_index(_cache(current))
                assert read_image_resource(
                    source.path, _cache(current), index.pages[0].id
                ) == (_PNG.replace(b"image", b"other"), "image/png")

    asyncio.run(run())


@pytest.mark.parametrize("format", list(MediaFormat))
def test_ingest_metadata_events(tmp_path, monkeypatch, format):
    """Apply metadata and cover events without parsing an unchanged body.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture rejecting unnecessary body builds and notifications.
        format: The reading source format.
    """
    monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            original = await MediaItem.get(lib_id=lib.id, path=str(source.path))
            monkeypatch.setattr(
                media_service,
                "_build_index",
                lambda *_args: pytest.fail("metadata must reuse the body index"),
            )
            novel = lib.lib_type == LibType.NOVEL
            metadata = source.directory / ("metadata.opf" if novel else "ComicInfo.xml")
            metadata.write_text(
                '<package xmlns="http://www.idpf.org/2007/opf" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata>'
                "<dc:title>Updated</dc:title></metadata></package>"
                if novel
                else "<ComicInfo><Title>Updated</Title></ComicInfo>"
            )
            cover = source.directory / "cover.png"
            cover.write_bytes(_PNG)
            for path in (metadata, cover, source.directory):
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(path),
                    event_type="modified",
                    is_directory=path == source.directory,
                )
            task = (await coalesce_reading_events(lib.id))[0]
            payload = ReadingReconcile.model_validate(task.payload)
            assert payload.force_targets == []
            assert not await MediaItemService.ingest_reading_work(
                lib.id,
                source.directory,
                targets={Path(path) for path in payload.targets},
                force_targets={Path(path) for path in payload.force_targets},
            )
            current = await MediaItem.get(id=original.id)
            assert current.title == "Updated"
            assert current.index_version == original.index_version

    asyncio.run(run())


@pytest.mark.parametrize("scope", ["chapter", "work", "empty", "force"])
def test_ingest_forced_scope(tmp_path, monkeypatch, scope):
    """Limit forced rebuilds to selected chapters while retaining full-force support.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture disabling application notifications.
        scope: The forced scope or the existing full-force flag to exercise.
    """
    monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.parent_path
            assert work is not None
            other = work / "Other"
            other.mkdir()
            (other / "1.png").write_bytes(_PNG)
            await MediaItemService.ingest_reading_work(lib.id, work)
            await MediaItem.filter(path=str(source.path)).update(visible=False)
            before = {item.path: item.index_version for item in await MediaItem.all()}
            forced = (
                {work}
                if scope == "work"
                else {source.directory}
                if scope == "chapter"
                else set()
            )
            if scope == "chapter":
                metadata = work / "ComicInfo.xml"
                metadata.write_text("<ComicInfo><Title>Updated</Title></ComicInfo>")
                for path in (source.path / "1.png", metadata):
                    await MediaEvent.create(
                        lib=lib, src_path=str(path), event_type="modified"
                    )
                task = (await coalesce_reading_events(lib.id))[0]
                payload = ReadingReconcile.model_validate(task.payload)
                assert payload.targets == [str(work)]
                forced = {Path(path) for path in payload.force_targets}
            assert not await MediaItemService.ingest_reading_work(
                lib.id,
                work,
                targets={work},
                force_targets=forced,
                force=scope == "force",
            )
            after = {item.path: item.index_version for item in await MediaItem.all()}
            assert (after[str(source.path)] != before[str(source.path)]) == (
                scope != "empty"
            )
            assert (after[str(other)] != before[str(other)]) == (
                scope in ("work", "force")
            )
            assert after[str(work)] is None
            assert not (await MediaItem.get(path=str(source.path))).visible
            if scope == "chapter":
                assert (await MediaItem.get(path=str(work))).title == "Updated"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("novel", "selection", "forced"),
    [
        (False, None, "../Other"),
        (False, None, ".Hidden"),
        (False, None, "Chapter/Nested"),
        (False, "Chapter", "Other"),
        (False, "Chapter", "."),
        (True, None, "Chapter"),
    ],
)
def test_ingest_forced_invalid(tmp_path, novel, selection, forced):
    """Reject forced containers outside the requested reading scope before writing.

    Args:
        tmp_path: The isolated filesystem root.
        novel: Whether to use the stricter novel layout.
        selection: The selected chapter, or None for the whole work.
        forced: The invalid forced container relative to the work.
    """

    async def run():
        async with _database():
            lib, source = await _source(
                tmp_path,
                MediaFormat.TXT if novel else MediaFormat.DIR,
                chapter=not novel,
            )
            work = source.parent_path or source.directory
            with pytest.raises(ValueError):
                await MediaItemService.ingest_reading_work(
                    lib.id,
                    work,
                    targets={work / selection} if selection else None,
                    force_targets={work / forced},
                )
            assert not await MediaItem.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem", ["empty", "missing", "ambiguous", "symlink", "target"]
)
def test_ingest_undiscovered(tmp_path, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            before = await MediaItem.all().values()
            if problem == "empty":
                source.path.unlink()
            elif problem == "missing":
                shutil.rmtree(source.directory)
            elif problem == "ambiguous":
                (source.directory / "Other.txt").write_text("Body")
            elif problem == "symlink":
                moved = tmp_path / "Moved"
                source.directory.rename(moved)
                source.directory.symlink_to(moved, target_is_directory=True)
            else:
                await MediaLib.filter(id=lib.id).update(lib_type=LibType.COMIC)
                chapter = source.directory / "Unselected"
                chapter.mkdir()
                (chapter / "1.png").write_bytes(_PNG)
            issues = await MediaItemService.ingest_reading_work(
                lib.id,
                source.directory,
                targets={source.directory / "Missing"} if problem == "target" else None,
            )
            assert issues == (
                {source.directory: "ambiguous_layout"}
                if problem == "ambiguous"
                else {source.directory: "media_source_unavailable"}
                if problem in ("missing", "symlink")
                else {source.path: "media_source_unavailable"}
                if problem == "empty"
                else {}
            )
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("chapter", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_ingest_empty_images(tmp_path, monkeypatch, chapter, hidden):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=chapter)
            work = source.parent_path or source.directory
            metadata = source.directory / "ComicInfo.xml"
            xml = "<ComicInfo><Title>Saved title</Title></ComicInfo>"
            metadata.write_text(xml)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            first = await MediaItem.get(lib_id=lib.id, path=str(source.path))
            await MediaItem.filter(id=first.id).update(visible=not hidden)
            page = source.path / "1.png"
            page.unlink()
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            await MediaEvent.create(
                lib_id=lib.id,
                event_type="deleted",
                src_path=str(page),
                is_directory=False,
            )
            task = (await coalesce_reading_events(lib.id))[0]
            payload = ReadingReconcile.model_validate(task.payload)
            for _ in range(2):
                assert not await MediaItemService.ingest_reading_work(
                    lib.id,
                    work,
                    targets={Path(path) for path in payload.targets},
                    force_targets={Path(path) for path in payload.force_targets},
                )
                current = await MediaItem.get(id=first.id)
                assert current.index_state == IndexState.EMPTY
                assert current.index_error == "empty_content"
                assert current.title == "Saved title" and current.visible != hidden
                assert current.parent_id == first.parent_id
                assert current.index_version == first.index_version
                assert current.extra == first.extra and current.size == first.size
                assert list(_cache(current).parent.iterdir()) == [_cache(first)]
                assert metadata.read_text() == xml
                if chapter:
                    parent = await MediaItem.get(id=first.parent_id)
                    assert parent.index_state == IndexState.EMPTY
                    assert parent.index_error == "empty_content"
                    assert parent.index_version is None and parent.extra is not None
                    assert parent.extra["content"]["chapter_count"] == int(not hidden)
            page.write_bytes(_PNG)
            (source.path / "2.png").write_bytes(_PNG)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            recovered = await MediaItem.get(id=first.id)
            assert recovered.index_state == IndexState.READY
            assert recovered.index_error is None and recovered.visible != hidden
            assert recovered.title == "Saved title"
            assert recovered.index_version != first.index_version
            assert recovered.extra is not None
            assert recovered.extra["content"]["page_count"] == 2
            assert await MediaItem.all().count() == (2 if chapter else 1)
            if chapter:
                parent = await MediaItem.get(id=first.parent_id)
                assert parent.index_state == (
                    IndexState.EMPTY if hidden else IndexState.READY
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat if format != MediaFormat.DIR]
    + [(MediaFormat.CBZ, True), (MediaFormat.ZIP, True)],
)
def test_ingest_file_events(tmp_path, monkeypatch, format, chapter):
    """Ingest file events and remove confirmed missing bodies before later reimport.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture controlling task time and application notifications.
        format: The body format ingested and then removed externally.
        chapter: Whether the source belongs to a comic collection.
    """

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            content = source.path.read_bytes()
            clock = [100.0]
            monkeypatch.setattr(media_events, "time", lambda: clock[0])
            monkeypatch.setattr(media_watcher, "time", lambda: clock[0])
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            first = None
            for removed in (False, True, False):
                if removed:
                    source.path.unlink()
                else:
                    source.directory.mkdir(parents=True, exist_ok=True)
                    source.path.write_bytes(content)
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(source.path),
                    event_type="deleted" if removed else "created",
                )
                task = (await coalesce_reading_events(lib.id))[0]
                assert not await prepare_reading_event(task.id)
                clock[0] += 2
                assert await prepare_reading_event(task.id)
                task = await MediaEvent.get(id=task.id).select_related("lib")
                assert await media_watcher.consume_event(task)
                if removed:
                    assert not await MediaItem.filter(path=str(source.path)).exists()
                    assert first is not None and not _cache(first).parent.exists()
                    payload = ReadingReconcile.model_validate(
                        (await MediaEvent.get(id=task.id)).payload
                    )
                    assert payload.state == "pending" and payload.attempts == 0
                    assert payload.error_code is None
                    clock[0] += 2
                    assert await prepare_reading_event(task.id)
                    assert await media_watcher.consume_event(
                        await MediaEvent.get(id=task.id).select_related("lib")
                    )
                else:
                    item = await MediaItem.get(path=str(source.path))
                    assert item.index_state == IndexState.READY
                    if first is not None:
                        assert item.id != first.id
                    first = item
                assert not await MediaEvent.exists()
                assert await MediaItem.all().count() == (
                    int(chapter) + int(not removed)
                )
                assert source.directory.exists() != removed
                assert Path(work).is_dir() == (chapter or not removed)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat]
    + [(MediaFormat.CBZ, True), (MediaFormat.ZIP, True)],
)
def test_ingest_replacement(tmp_path, format, chapter):
    """Require identity reconciliation before registering another body in a container.

    Args:
        tmp_path: The isolated filesystem root.
        format: The previously indexed body format.
        chapter: Whether the replaced source belongs to a comic collection.
    """

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            await MediaItemService.ingest_reading_work(lib.id, work)
            item = await MediaItem.get(path=str(source.path))
            await MediaItem.filter(id=item.id).update(visible=False)
            if format == MediaFormat.DIR:
                (source.path / "1.png").unlink()
                with zipfile.ZipFile(source.path / "Replaced.cbz", "w") as archive:
                    archive.writestr("1.png", _PNG)
            else:
                source.path.rename(source.path.with_stem("Renamed"))
            before = await MediaItem.filter(id=item.id).values()
            assert await MediaItemService.ingest_reading_work(lib.id, work) == {
                source.directory: "content_changed"
            }
            assert await MediaItem.filter(id=item.id).values() == before
            assert await MediaItem.all().count() == (2 if chapter else 1)
            assert _cache(item).is_dir()

    asyncio.run(run())


def test_ingest_missing_sibling(tmp_path):
    """Keep a missing chapter pending without blocking another selected chapter.

    Args:
        tmp_path: The isolated filesystem root.
    """

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.CBZ, chapter=True)
            work = source.directory.parent
            other = work / "Other"
            other.mkdir()
            (other / "Book.cbz").write_bytes(source.path.read_bytes())
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            before = await MediaItem.all().values()
            source.path.unlink()
            assert not await MediaItemService.ingest_reading_work(
                lib.id, work, targets={other}
            )
            assert await MediaItemService.ingest_reading_work(lib.id, work) == {
                source.path: "media_source_unavailable"
            }
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter", "location"),
    [
        (format, False, location)
        for format in (
            MediaFormat.TXT,
            MediaFormat.EPUB,
            MediaFormat.CBZ,
            MediaFormat.ZIP,
        )
        for location in ("rename", "container")
    ]
    + [
        (format, True, location)
        for format in (MediaFormat.CBZ, MediaFormat.ZIP)
        for location in ("rename", "container", "collection")
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("chained", [False, True])
def test_consume_file_move(
    tmp_path, monkeypatch, format, chapter, location, reverse, chained
):
    """Apply observed file moves before ingestion regardless of work task order.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture controlling event time and notifications.
        format: The body file format whose identity must survive the move.
        chapter: Whether the file belongs to a comic collection.
        location: A rename, another container or another comic collection.
        reverse: Whether to consume the destination work's task first.
        chained: Whether the body passes through an intermediate file path.
    """

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            novel = lib.lib_type == LibType.NOVEL
            metadata = source.directory / ("metadata.opf" if novel else "ComicInfo.xml")
            xml = (
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Original</dc:title></metadata></package>"
                if novel
                else "<ComicInfo><Title>Original</Title></ComicInfo>"
            )
            metadata.write_text(xml)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            first = await MediaItem.get(path=str(source.path))
            await MediaItem.filter(id=first.id).update(visible=False)
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user,
                rel_type=HistoryType.TEXT if novel else HistoryType.IMAGE,
                rel_id=first.parent_id or first.id,
                percentage=25,
                locator={"chapter_item_id": first.id, "version": first.index_version},
            )
            history = await UserHistory.all().values()
            if location == "rename":
                directory = source.directory
            elif location == "container":
                directory = source.directory.with_name("Moved")
                directory.mkdir()
            else:
                directory = work.with_name("Other") / "Moved"
                directory.mkdir(parents=True)
            if location != "rename":
                (directory / metadata.name).write_text(
                    xml.replace("Original", "Current")
                )
            destination = directory / f"Renamed.{format.value.upper()}"
            body = source.path.read_bytes()
            previous = source.path
            if chained:
                intermediate = source.directory / f"Middle.{format}"
                previous.rename(intermediate)
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(previous),
                    dest_path=str(intermediate),
                    event_type="moved",
                )
                previous = intermediate
            previous.rename(destination)
            clock = [100.0]
            monkeypatch.setattr(media_events, "time", lambda: clock[0])
            monkeypatch.setattr(media_watcher, "time", lambda: clock[0])
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            await MediaEvent.create(
                lib=lib,
                src_path=str(previous),
                dest_path=str(destination),
                event_type="moved",
            )
            tasks = await coalesce_reading_events(lib.id)
            if reverse:
                tasks.reverse()
            for task in tasks:
                assert not await prepare_reading_event(task.id)
                clock[0] += 2
                assert await prepare_reading_event(task.id)
                prepared = await MediaEvent.get(id=task.id).select_related("lib")
                assert await media_watcher.consume_event(prepared)
                assert not await MediaEvent.filter(id=task.id).exists()
            current = await MediaItem.get(id=first.id)
            assert current.path == str(destination) and current.dir == str(directory)
            assert current.index_state == IndexState.READY and not current.visible
            assert current.index_version != first.index_version
            assert current.title == ("Original" if location == "rename" else "Current")
            if location == "collection":
                parent = await MediaItem.get(id=current.parent_id)
                assert parent.path == str(directory.parent)
                assert current.parent_id != first.parent_id
            else:
                assert current.parent_id == first.parent_id
            assert await MediaItem.filter(format__isnull=False).count() == 1
            assert await UserHistory.all().values() == history
            assert metadata.read_text() == xml
            assert destination.read_bytes() == body
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "scope"),
    [(format, "work") for format in MediaFormat]
    + [
        (format, scope)
        for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
        for scope in ("collection", "chapter", "other_collection")
    ]
    + [
        (MediaFormat.TXT, "case"),
        (MediaFormat.DIR, "case"),
        (MediaFormat.DIR, "collection_case"),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("chained", [False, True])
def test_consume_directory_move(tmp_path, monkeypatch, format, scope, reverse, chained):
    """Reconcile whole directories and chapters without duplicating old work paths.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture controlling event time and notifications.
        format: The body format within the moved directory.
        scope: A work, collection, chapter, cross-collection or case-only rename.
        reverse: Whether the destination task is consumed before the source task.
        chained: Whether the directory passes through an intermediate path.
    """

    async def run():
        async with _database():
            chapter = scope in (
                "collection",
                "chapter",
                "other_collection",
                "collection_case",
            )
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            if chapter:
                sibling = work / "Sibling"
                sibling.mkdir()
                (sibling / "1.png").write_bytes(_PNG)
            novel = lib.lib_type == LibType.NOVEL
            (
                source.directory / ("metadata.opf" if novel else "ComicInfo.xml")
            ).write_text(
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Original</dc:title></metadata></package>"
                if novel
                else "<ComicInfo><Title>Original</Title></ComicInfo>"
            )
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            first = await MediaItem.get(path=str(source.path))
            await MediaItem.filter(id=first.id).update(visible=False)
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user,
                rel_type=HistoryType.TEXT if novel else HistoryType.IMAGE,
                rel_id=first.parent_id or first.id,
                percentage=25,
                locator={"chapter_item_id": first.id, "version": first.index_version},
            )
            history = await UserHistory.all().values()
            before = {item.id: item for item in await MediaItem.all()}
            previous = (
                source.directory if scope in ("chapter", "other_collection") else work
            )
            if scope == "other_collection":
                destination = work.with_name("Other") / "Moved"
                destination.parent.mkdir()
            else:
                destination = previous.with_name(
                    previous.name.lower() if "case" in scope else "Moved"
                )
            files = {
                path.relative_to(previous): path.read_bytes()
                for path in previous.rglob("*")
                if path.is_file()
            }
            latest = previous
            if chained:
                intermediate = previous.with_name("Middle")
                previous.rename(intermediate)
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(previous),
                    dest_path=str(intermediate),
                    event_type="moved",
                    is_directory=True,
                )
                latest = intermediate
            latest.rename(destination)
            clock = [100.0]
            monkeypatch.setattr(media_events, "time", lambda: clock[0])
            monkeypatch.setattr(media_watcher, "time", lambda: clock[0])
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            await MediaEvent.create(
                lib=lib,
                src_path=str(latest),
                dest_path=str(destination),
                event_type="moved",
                is_directory=True,
            )
            tasks = await coalesce_reading_events(lib.id)
            if reverse:
                tasks.reverse()
            for task in tasks:
                assert not await prepare_reading_event(task.id)
                clock[0] += 2
                assert await prepare_reading_event(task.id)
                task = await MediaEvent.get(id=task.id).select_related("lib")
                assert await media_watcher.consume_event(task)
                assert not await MediaEvent.filter(id=task.id).exists()
            after = {item.id: item for item in await MediaItem.all()}
            assert set(before).issubset(after)
            assert len(after) == len(before) + int(scope == "other_collection")
            for id, original in before.items():
                current = after[id]
                assert current.visible == original.visible
                if Path(original.path).is_relative_to(previous):
                    assert current.path == str(
                        destination / Path(original.path).relative_to(previous)
                    )
                    assert current.dir == str(
                        destination / Path(original.dir).relative_to(previous)
                    )
                    if current.format is not None:
                        assert current.index_state == IndexState.READY
                        assert current.index_version != original.index_version
                else:
                    assert current.path == original.path and current.dir == original.dir
                    assert current.index_version == original.index_version
            current = after[first.id]
            if scope == "other_collection":
                assert current.parent_id is not None
                assert current.parent_id != first.parent_id
                assert after[current.parent_id].path == str(destination.parent)
            else:
                assert current.parent_id == first.parent_id
            assert current.title == "Original"
            assert await UserHistory.all().values() == history
            assert {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            } == files
            assert not await MediaEvent.exists()

    asyncio.run(run())


def test_ingest_empty_scope(tmp_path):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.parent_path
            assert work is not None
            other = work / "Other"
            other.mkdir()
            (other / "1.png").write_bytes(_PNG)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            sibling = await MediaItem.get(path=str(other))
            before = await MediaItem.filter(id=sibling.id).values()
            empty = work / "Empty"
            empty.mkdir()
            (empty / "ComicInfo.xml").write_text("<ComicInfo/>")
            for directory in (source.path, other):
                (directory / "1.png").unlink()
            assert not await MediaItemService.ingest_reading_work(
                lib.id, work, targets={source.directory}
            )
            assert await MediaItem.filter(id=sibling.id).values() == before
            parent = await MediaItem.get(path=str(work))
            assert parent.index_state == IndexState.READY
            assert not await MediaItem.filter(path=str(empty)).exists()
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            parent = await MediaItem.get(id=parent.id)
            assert parent.index_state == IndexState.EMPTY and parent.extra is not None
            assert parent.extra["content"]["chapter_count"] == 2
            assert await MediaItem.all().count() == 3

    asyncio.run(run())


@pytest.mark.parametrize("existing", [False, True])
def test_ingest_empty_collection(tmp_path, existing):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.parent_path
            assert work is not None
            original = None
            if existing:
                await MediaItemService.ingest_reading_work(lib.id, work)
                original = await MediaItem.get(path=str(work))
                await MediaItem.filter(parent_id=original.id).delete()
            shutil.rmtree(source.directory)
            metadata = work / "ComicInfo.xml"
            xml = "<ComicInfo><Title>Empty collection</Title></ComicInfo>"
            metadata.write_text(xml)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            assert await MediaItem.all().count() == int(existing)
            if original is not None:
                current = await MediaItem.get(id=original.id)
                assert current.title == "Empty collection"
                assert current.index_state == IndexState.EMPTY
                assert current.index_error == "empty_content"
                assert current.index_version is None and current.extra is not None
                assert current.extra["content"]["chapter_count"] == 0
            assert metadata.read_text() == xml
            source.directory.mkdir()
            (source.directory / "1.png").write_bytes(_PNG)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            current = await MediaItem.get(path=str(work))
            assert current.index_state == IndexState.READY
            assert current.index_error is None
            assert original is None or current.id == original.id
            assert await MediaItem.all().count() == 2

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["missing", "symlink", "inaccessible", "ambiguous"])
def test_ingest_directory_unavailable(tmp_path, monkeypatch, problem):
    async def run():
        async with _database():
            from app.core.media.handlers import reading

            lib, source = await _source(tmp_path, MediaFormat.DIR)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            before = await MediaItem.all().values()
            if problem == "missing":
                shutil.rmtree(source.directory)
            elif problem == "symlink":
                moved = tmp_path / "Moved"
                source.directory.rename(moved)
                source.directory.symlink_to(moved, target_is_directory=True)
            elif problem == "inaccessible":

                def denied(directory):
                    """Simulate failed enumeration without relying on permissions.

                    Args:
                        directory: The work directory being inspected.

                    Raises:
                        PermissionError: For the inaccessible work.
                    """
                    assert directory == source.directory
                    raise PermissionError(str(directory))

                monkeypatch.setattr(reading, "list_source_entries", denied)
            else:
                (source.directory / "Book.cbz").write_bytes(b"broken")
            assert await MediaItemService.ingest_reading_work(
                lib.id, source.directory
            ) == {
                source.path: "ambiguous_layout"
                if problem == "ambiguous"
                else "media_source_unavailable"
            }
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("change", ["delete", "move", "refill"])
def test_ingest_empty_changed(tmp_path, monkeypatch, change):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            first = await MediaItem.get(path=str(source.path))
            (source.path / "1.png").unlink()
            create = MediaItemService.create_reading

            async def changed(lib_id, candidate):
                """Change a saved source after discovery and before revalidation.

                Args:
                    lib_id: The library passed to source registration.
                    candidate: The saved empty container selected for revalidation.

                Returns:
                    The registered item if its current source is still valid.

                Raises:
                    ContentError: If the source or its saved ownership changed.
                """
                if change == "delete":
                    await MediaItem.filter(id=first.id).delete()
                elif change == "move":
                    moved = source.path.with_name("Moved")
                    source.path.rename(moved)
                    await MediaItem.filter(id=first.id).update(
                        dir=str(moved), path=str(moved)
                    )
                else:
                    (source.path / "1.png").write_bytes(_PNG)
                    (source.path / "2.png").write_bytes(_PNG)
                return await create(lib_id, candidate)

            monkeypatch.setattr(MediaItemService, "create_reading", changed)
            issues = await MediaItemService.ingest_reading_work(
                lib.id, source.directory
            )
            if change == "delete":
                assert issues == {source.path: "empty_content"}
                assert not await MediaItem.all().exists()
            else:
                current = await MediaItem.get(id=first.id)
                assert current.index_state == IndexState.READY
                if change == "move":
                    assert issues == {source.path: "media_source_unavailable"}
                    assert current.path == str(source.path.with_name("Moved"))
                    assert current.index_version == first.index_version
                else:
                    assert not issues
                    assert current.index_version != first.index_version

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem",
    [
        "root",
        "outside",
        "hidden",
        "nested",
        "empty_targets",
        "foreign_target",
        "novel_chapter",
        "video",
        "missing_library",
    ],
)
def test_ingest_invalid(tmp_path, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            work, targets = source.directory, None
            if problem == "root":
                work = Path(lib.dir)
            elif problem == "outside":
                work = tmp_path / "Other"
            elif problem == "hidden":
                work = Path(lib.dir) / ".hidden"
            elif problem == "nested":
                work /= "Nested"
            elif problem == "empty_targets":
                targets = set()
            elif problem == "foreign_target":
                targets = {Path(lib.dir) / "Other"}
            elif problem == "novel_chapter":
                targets = {work / "Chapter"}
            elif problem == "video":
                await MediaLib.filter(id=lib.id).update(lib_type=LibType.MOVIE)
            else:
                await lib.delete()
            with pytest.raises(
                DoesNotExist
                if problem == "missing_library"
                else ContentError
                if problem == "video"
                else ValueError
            ):
                await MediaItemService.ingest_reading_work(
                    lib.id, work, targets=targets
                )
            assert not await MediaItem.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_ingest_interrupted(tmp_path, monkeypatch, cancel):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.parent_path
            assert work is not None
            error = asyncio.CancelledError if cancel else RuntimeError
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    MediaItemService, "index_content", AsyncMock(side_effect=error)
                )
                with pytest.raises(error):
                    await MediaItemService.ingest_reading_work(lib.id, work)
            before = await MediaItem.all().values_list("id", flat=True)
            assert len(before) == 2
            assert await MediaItem.filter(index_state=IndexState.PENDING).count() == 2
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            assert await MediaItem.all().values_list("id", flat=True) == before
            assert await MediaItem.filter(index_state=IndexState.READY).count() == 2

    asyncio.run(run())


@pytest.mark.parametrize("change", ["directory", "type", "delete"])
def test_ingest_library_changed(tmp_path, monkeypatch, change):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            worker = media_service.to_thread

            async def changed(function, *args, **kwargs):
                """Change library ownership after discovery finishes in its worker.

                Args:
                    function: The discovery function being dispatched.
                    *args: The positional discovery arguments.
                    **kwargs: The selected work passed to discovery.

                Returns:
                    The completed discovery result from the original library.
                """
                result = await worker(function, *args, **kwargs)
                if change == "directory":
                    await MediaLib.filter(id=lib.id).update(dir=str(tmp_path / "Moved"))
                elif change == "type":
                    await MediaLib.filter(id=lib.id).update(lib_type=LibType.COMIC)
                else:
                    await lib.delete()
                return result

            monkeypatch.setattr(media_service, "to_thread", changed)
            with pytest.raises(DoesNotExist if change == "delete" else ContentError):
                await MediaItemService.ingest_reading_work(lib.id, source.directory)
            assert not await MediaItem.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat]
    + [
        (format, True) for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
    ],
)
def test_create_reading(tmp_path, format, chapter):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            before = {
                path: path.read_bytes()
                for path in Path(lib.dir).rglob("*")
                if path.is_file()
            }
            handler = get_handler(lib.lib_type)
            assert isinstance(handler, ReadingMediaHandler)
            scan = await asyncio.to_thread(handler.scan_sources, lib.dir)
            assert not scan.issues
            items = [
                await MediaItemService.create_reading(lib.id, candidate)
                for candidate in scan.sources
            ]
            assert len(items) == await MediaItem.all().count() == (2 if chapter else 1)
            item = items[-1]
            assert item.path == str(source.path) and item.dir == str(source.directory)
            assert item.format == format
            assert item.name == (
                source.path.name if format == MediaFormat.DIR else source.path.stem
            )
            assert item.parent_id == (items[0].id if chapter else None)
            assert item.lib.id == lib.id
            for current in items:
                assert current.index_state == IndexState.PENDING and current.visible
                assert current.hash is current.nfo_path is current.index_version is None
                assert current.title is current.extra is current.size is None
                await MediaItemService.sync_metadata(current.id)
            assert not (
                Path(KaloscopeConfig.get_workspace("temp")) / "media_index"
            ).exists()
            current = await MediaItemService.index_content(item.id)
            assert current.index_state == IndexState.READY and current.title is not None
            assert current.id == item.id and current.parent_id == item.parent_id
            if chapter:
                collection = await MediaItemService.sync_collection(items[0].id)
                assert collection.index_state == IndexState.READY
                assert collection.index_version is collection.size is None
                assert collection.extra is not None
                assert collection.extra["content"] == {
                    "chapter_count": 1,
                    "page_count": None,
                }
            cache = _cache(current)
            if format in (MediaFormat.TXT, MediaFormat.EPUB):
                index = load_text_index(cache)
                content = read_text_chapter(cache, index.chapters[0].id)
                assert (
                    content == ["Body"]
                    if format == MediaFormat.TXT
                    else isinstance(content, EpubContent)
                )
            else:
                index = load_image_index(cache)
                assert read_image_resource(source.path, cache, index.pages[0].id) == (
                    _PNG,
                    "image/png",
                )
            assert all(path.read_bytes() == data for path, data in before.items())

    asyncio.run(run())


@pytest.mark.parametrize("state", list(IndexState))
def test_create_preserves(tmp_path, state):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR)
            item = await MediaItemService.create_reading(lib.id, source)
            await MediaItem.filter(id=item.id).update(
                visible=False,
                title="Saved title",
                year=2020,
                poster="/_api/media/1/assets/cover",
                index_state=state,
                index_version="a" * 64,
                index_error="empty_content" if state == IndexState.EMPTY else None,
                extra={"schema_version": 1, "content": {"page_count": 1}},
            )
            if state == IndexState.EMPTY:
                (source.path / "1.png").unlink()
            before = await MediaItem.all().values()
            current = await MediaItemService.create_reading(lib.id, source)
            assert current.id == item.id
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("field", ["format", "dir", "parent"])
def test_create_conflict(tmp_path, field):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR)
            item = await MediaItemService.create_reading(lib.id, source)
            if field == "format":
                await MediaItem.filter(id=item.id).update(format=None)
            elif field == "dir":
                await MediaItem.filter(id=item.id).update(dir=lib.dir)
            else:
                parent = await MediaItem.create(
                    lib=lib,
                    path=str(Path(lib.dir) / "Other"),
                    dir=lib.dir,
                    name="Other",
                )
                await MediaItem.filter(id=item.id).update(parent=parent)
            before = await MediaItem.all().values()
            with pytest.raises(ContentError, match="ambiguous_layout"):
                await MediaItemService.create_reading(lib.id, source)
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("state", ["missing", "foreign", "format", "nested", "hidden"])
def test_create_parent(tmp_path, state):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            assert source.parent_path is not None
            parent = None
            if state != "missing":
                parent = await MediaItemService.create_reading(
                    lib.id, ReadingSource(source.parent_path, None)
                )
                if state == "foreign":
                    other = await MediaLib.create(
                        dir=str(tmp_path / "Other"),
                        name="Other",
                        lib_type=LibType.COMIC,
                        priority=2,
                    )
                    await MediaItem.filter(id=parent.id).update(lib_id=other.id)
                elif state == "format":
                    await MediaItem.filter(id=parent.id).update(format=MediaFormat.DIR)
                elif state == "nested":
                    ancestor = await MediaItem.create(
                        lib=lib, path=lib.dir, dir=lib.dir, name="Root"
                    )
                    await MediaItem.filter(id=parent.id).update(parent=ancestor)
                else:
                    await MediaItem.filter(id=parent.id).update(visible=False)
            before = await MediaItem.all().values()
            if state == "hidden":
                item = await MediaItemService.create_reading(lib.id, source)
                assert parent is not None and item.parent_id == parent.id
                assert item.parent is not None and not item.parent.visible
            else:
                with pytest.raises(ContentError, match="unsupported_layout"):
                    await MediaItemService.create_reading(lib.id, source)
                assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem",
    [
        "outside",
        "root",
        "symlink",
        "ancestor",
        "ambiguous",
        "format",
        "missing",
        "empty",
        "collection",
    ],
)
def test_create_source(tmp_path, problem):
    async def run():
        async with _database():
            lib, source = await _source(
                tmp_path,
                MediaFormat.DIR
                if problem in ("empty", "collection")
                else MediaFormat.TXT,
            )
            if problem == "outside":
                source = replace(source, path=tmp_path / "Book.txt")
            elif problem == "root":
                source = replace(source, path=Path(lib.dir))
            elif problem in ("symlink", "ancestor"):
                path = source.path if problem == "symlink" else Path(lib.dir)
                moved = path.with_name(path.name + "-moved")
                path.rename(moved)
                path.symlink_to(moved, target_is_directory=problem == "ancestor")
            elif problem == "ambiguous":
                (source.directory / "Other.txt").write_text("Other")
            elif problem == "format":
                source = replace(source, format=MediaFormat.EPUB)
            elif problem == "missing":
                source.path.unlink()
            else:
                (source.path / "1.png").unlink()
                if problem == "collection":
                    source = replace(source, format=None)
            with pytest.raises(ContentError):
                await MediaItemService.create_reading(lib.id, source)
            assert await MediaItem.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.EPUB, MediaFormat.CBZ])
def test_create_invalid_body(tmp_path, format):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format)
            source.path.write_bytes(b"\x00invalid")
            item = await MediaItemService.create_reading(lib.id, source)
            assert item.index_state == IndexState.PENDING
            with pytest.raises(ContentError):
                await MediaItemService.index_content(item.id)
            current = await MediaItem.get(id=item.id)
            assert current.index_state == IndexState.ERROR
            assert current.index_error == (
                "text_decode_failed" if format == MediaFormat.TXT else "invalid_archive"
            )

    asyncio.run(run())


@pytest.mark.parametrize("missing", [False, True])
def test_create_library(tmp_path, missing):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            if missing:
                await lib.delete()
            else:
                await MediaLib.filter(id=lib.id).update(lib_type=LibType.MOVIE)
            with pytest.raises(DoesNotExist if missing else ContentError):
                await MediaItemService.create_reading(lib.id, source)
            assert await MediaItem.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [
        (format, False)
        for format in (
            MediaFormat.TXT,
            MediaFormat.EPUB,
            MediaFormat.CBZ,
            MediaFormat.ZIP,
        )
    ]
    + [(format, True) for format in (MediaFormat.CBZ, MediaFormat.ZIP)],
)
@pytest.mark.parametrize("cross_container", [False, True])
def test_move_reading_file(tmp_path, monkeypatch, format, chapter, cross_container):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            novel = lib.lib_type == LibType.NOVEL
            metadata = source.directory / ("metadata.opf" if novel else "ComicInfo.xml")
            xml = (
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Manual</dc:title></metadata></package>"
                if novel
                else "<ComicInfo><Title>Manual</Title></ComicInfo>"
            )
            metadata.write_text(xml)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            first = await MediaItem.get(lib_id=lib.id, path=str(source.path))
            await MediaItem.filter(id=first.id).update(visible=False)
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            locator: dict[str, str | int | None] = {"version": first.index_version}
            if not novel:
                locator["chapter_item_id"] = first.id if chapter else None
            await UserHistory.create(
                user=user,
                rel_id=first.parent_id or first.id,
                rel_type=HistoryType.TEXT if novel else HistoryType.IMAGE,
                percentage=25,
                locator=locator,
            )
            history = await UserHistory.all().values()
            body = source.path.read_bytes()
            destination = source.path.with_name(f"Renamed.{format.value.upper()}")
            if cross_container:
                directory = source.directory.with_name("Moved")
                directory.mkdir()
                destination = directory / destination.name
                (directory / metadata.name).write_text(xml.replace("Manual", "Current"))
            source.path.rename(destination)
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            await MediaEvent.create(
                lib_id=lib.id,
                event_type="moved",
                src_path=str(source.path),
                dest_path=str(destination),
                is_directory=False,
            )
            tasks = await coalesce_reading_events(lib.id)
            target_work = source.parent_path or destination.parent
            task = next(task for task in tasks if task.src_path == str(target_work))
            payload = ReadingReconcile.model_validate(task.payload)
            move = payload.moves[0]
            current = await MediaItemService.move_reading_file(
                lib.id, Path(move.src_path), Path(move.dest_path)
            )
            assert current is not None and current.id == first.id
            assert current.path == str(destination) and current.name == "Renamed"
            assert current.dir == str(destination.parent)
            assert current.parent_id == first.parent_id
            assert current.index_state == IndexState.PENDING
            assert current.index_error is None and not current.visible
            assert current.title == "Manual" and current.extra == first.extra
            assert current.index_version == first.index_version
            assert current.size == first.size and _cache(first).is_dir()
            saved = await MediaItem.all().values()
            assert (
                await MediaItemService.move_reading_file(
                    lib.id, source.path, destination
                )
                is None
            )
            assert await MediaItem.all().values() == saved
            assert not await MediaItemService.ingest_reading_work(
                lib.id,
                target_work,
                targets={Path(path) for path in payload.targets},
                force_targets={Path(path) for path in payload.force_targets},
            )
            rebuilt = await MediaItem.get(id=first.id)
            assert rebuilt.index_state == IndexState.READY
            assert rebuilt.index_version != first.index_version
            assert rebuilt.path == str(destination) and not rebuilt.visible
            assert rebuilt.title == ("Current" if cross_container else "Manual")
            assert rebuilt.parent_id == first.parent_id
            assert await MediaItem.all().count() == (2 if chapter else 1)
            assert await UserHistory.all().values() == history
            assert destination.read_bytes() == body and metadata.read_text() == xml
            if cross_container:
                assert (destination.parent / metadata.name).read_text() == xml.replace(
                    "Manual", "Current"
                )
            assert await MediaEvent.filter(id=task.id).exists()
            assert await MediaEvent.all().count() == (
                2 if cross_container and not chapter else 1
            )

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.EPUB])
def test_rename_metadata(tmp_path, format):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format)
            metadata = source.path.with_suffix(".opf")
            xml = (
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Local</dc:title></metadata></package>"
            )
            metadata.write_text(xml)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            first = await MediaItem.get(path=str(source.path))
            assert first.title == "Local"
            destination = source.path.with_name(f"Renamed.{format}")
            source.path.rename(destination)
            await MediaItemService.move_reading_file(lib.id, source.path, destination)
            assert not await MediaItemService.ingest_reading_work(
                lib.id, source.directory
            )
            current = await MediaItem.get(id=first.id)
            assert current.title == (
                "Renamed" if format == MediaFormat.TXT else "Embedded"
            )
            assert current.extra is not None
            assert current.extra["metadata_sync"]["state"] == "none"
            assert metadata.read_text() == xml
            destination.with_suffix(".opf").write_text(xml.replace("Local", "Current"))
            assert not await MediaItemService.ingest_reading_work(
                lib.id, source.directory
            )
            updated = await MediaItem.get(id=first.id)
            assert updated.title == "Current" and updated.extra is not None
            assert updated.extra["metadata_sync"]["relative_path"] == "Renamed.opf"
            assert updated.index_version == current.index_version
            assert metadata.read_text() == xml

    asyncio.run(run())


def test_rename_case(tmp_path):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            first = await MediaItemService.create_reading(lib.id, source)
            destination = source.path.with_name(source.path.name.lower())
            source.path.rename(destination)
            current = await MediaItemService.move_reading_file(
                lib.id, source.path, destination
            )
            assert current is not None and current.id == first.id
            assert current.path == str(destination) and current.name == "book"
            assert await MediaItem.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("problem", "code"),
    [
        ("reused", "ambiguous_layout"),
        ("source_link", "content_changed"),
        ("owned", "ambiguous_layout"),
        ("missing", "media_source_unavailable"),
        ("target_link", "media_source_unavailable"),
        ("container_link", "media_source_unavailable"),
        ("ambiguous", "ambiguous_layout"),
        ("format", "unsupported_media_format"),
    ],
)
def test_rename_conflict(tmp_path, problem, code):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            destination = source.path.with_name("Renamed.txt")
            source.path.rename(destination)
            if problem == "reused":
                source.path.write_text("New source")
            elif problem == "source_link":
                source.path.symlink_to(destination)
            elif problem == "owned":
                await MediaItem.create(
                    lib=lib,
                    dir=str(source.directory),
                    path=str(destination),
                    name="Other owner",
                    format=MediaFormat.TXT,
                )
            elif problem == "missing":
                destination.unlink()
            elif problem == "target_link":
                outside = tmp_path / "Outside.txt"
                destination.rename(outside)
                destination.symlink_to(outside)
            elif problem == "container_link":
                outside = tmp_path / "Moved"
                source.directory.rename(outside)
                source.directory.symlink_to(outside, target_is_directory=True)
            elif problem == "ambiguous":
                (source.directory / "Other.txt").write_text("Body")
            else:
                new_format = destination.with_suffix(".epub")
                destination.rename(new_format)
                destination = new_format
            before = await MediaItem.all().values()
            with pytest.raises(ContentError, match=code):
                await MediaItemService.move_reading_file(
                    lib.id, source.path, destination
                )
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem", ["relative", "outside", "hidden", "same", "level", "traversal"]
)
def test_move_file_paths(tmp_path, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.create_reading(lib.id, source)
            previous, destination = source.path, source.path.with_name("Renamed.txt")
            if problem == "relative":
                previous, destination = Path(previous.name), Path(destination.name)
            elif problem == "outside":
                previous, destination = (
                    tmp_path / previous.name,
                    tmp_path / destination.name,
                )
            elif problem == "hidden":
                destination = destination.with_name(".Renamed.txt")
            elif problem == "same":
                destination = previous
            elif problem == "level":
                destination = Path(lib.dir) / "Other/Chapter/Renamed.txt"
            else:
                destination = source.directory / "../Work/Renamed.txt"
            before = await MediaItem.all().values()
            with pytest.raises(ValueError, match="move paths"):
                await MediaItemService.move_reading_file(lib.id, previous, destination)
            assert await MediaItem.all().values() == before
            assert source.path.read_text() == "Body"

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["unknown", "image_directory", "collection", "video"])
def test_rename_source_type(tmp_path, kind):
    async def run():
        async with _database():
            lib, source = await _source(
                tmp_path,
                MediaFormat.DIR
                if kind in ("image_directory", "collection")
                else MediaFormat.TXT,
                chapter=kind == "collection",
            )
            if kind != "unknown":
                await MediaItemService.ingest_reading_work(
                    lib.id, source.parent_path or source.directory
                )
            if kind == "video":
                await MediaLib.filter(id=lib.id).update(lib_type=LibType.MOVIE)
            previous = source.parent_path or source.path
            destination = previous.with_name("Renamed" + previous.suffix)
            before = await MediaItem.all().values()
            if kind == "unknown":
                assert (
                    await MediaItemService.move_reading_file(
                        lib.id, previous, destination
                    )
                    is None
                )
            else:
                with pytest.raises(ContentError, match="unsupported_media_format"):
                    await MediaItemService.move_reading_file(
                        lib.id, previous, destination
                    )
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("change", ["directory", "type", "delete"])
@pytest.mark.parametrize("work", [False, True])
def test_rename_library_changed(tmp_path, monkeypatch, change, work):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.create_reading(lib.id, source)
            previous = source.directory if work else source.path
            destination = previous.with_name("Renamed" if work else "Renamed.txt")
            previous.rename(destination)
            before = await MediaItem.all().values()

            @asynccontextmanager
            async def changed(directory):
                """Change the library before its rename lock is acquired.

                Args:
                    directory: The original library root to lock.

                Yields:
                    Control while the original library lock is held.
                """
                if change == "delete":
                    await lib.delete()
                elif change == "directory":
                    await MediaLib.filter(id=lib.id).update(dir=str(tmp_path / "Moved"))
                else:
                    await MediaLib.filter(id=lib.id).update(lib_type=LibType.COMIC)
                async with library_lock(directory):
                    yield

            monkeypatch.setattr(media_service, "library_lock", changed)
            with pytest.raises(DoesNotExist if change == "delete" else ContentError):
                rename = (
                    MediaItemService.move_reading_directory
                    if work
                    else MediaItemService.move_reading_file
                )
                await rename(lib.id, previous, destination)
            assert await MediaItem.all().values() == (
                [] if change == "delete" else before
            )

    asyncio.run(run())


@pytest.mark.parametrize("change", ["remove", "replace", "reuse", "cancel"])
@pytest.mark.parametrize("move", [False, True])
def test_move_file_unstable(tmp_path, monkeypatch, change, move):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.create_reading(lib.id, source)
            destination = source.path.with_name("Renamed.txt")
            if move:
                directory = source.directory.with_name("Moved")
                directory.mkdir()
                destination = directory / destination.name
            source.path.rename(destination)
            before = await MediaItem.all().values()
            handler = get_handler(LibType.NOVEL)
            scan = handler.scan_sources

            def changed(base_path, *, work_path=None):
                """Change the filesystem after discovery inside source validation.

                Args:
                    base_path: The root passed to source discovery.
                    work_path: The selected work, or None for a library scan.

                Returns:
                    The discovery result from before the filesystem change.

                Raises:
                    asyncio.CancelledError: If cancellation is selected for this test.
                """
                result = scan(base_path, work_path=work_path)
                if change == "cancel":
                    raise asyncio.CancelledError
                if change == "remove":
                    destination.unlink()
                elif change == "replace":
                    replacement = source.directory / "replacement.tmp"
                    replacement.write_text("Next")
                    replacement.replace(destination)
                else:
                    source.path.write_text("Reused")
                return result

            monkeypatch.setattr(handler, "scan_sources", changed)
            with pytest.raises(
                asyncio.CancelledError if change == "cancel" else ContentError
            ):
                await MediaItemService.move_reading_file(
                    lib.id, source.path, destination
                )
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    ("problem", "code"),
    [
        ("reused", "content_changed"),
        ("hardlink", "content_changed"),
        ("source_link", "content_changed"),
        ("container_link", "media_source_unavailable"),
        ("owned", "ambiguous_layout"),
        ("ambiguous", "ambiguous_layout"),
        ("directory", "unsupported_layout"),
    ],
)
def test_move_file_conflict(tmp_path, problem, code):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            first = await MediaItemService.create_reading(lib.id, source)
            directory = source.directory.with_name("Moved")
            directory.mkdir()
            destination = directory / source.path.name
            source.path.rename(destination)
            if problem == "reused":
                source.path.write_text("Reused source")
            elif problem == "hardlink":
                source.path.hardlink_to(destination)
            elif problem == "source_link":
                source.path.symlink_to(destination)
            elif problem == "container_link":
                outside = tmp_path / "Outside"
                source.directory.rename(outside)
                source.directory.symlink_to(outside, target_is_directory=True)
            elif problem == "owned":
                await MediaItem.create(
                    lib=lib,
                    path=str(directory / "Other.txt"),
                    dir=str(directory),
                    name="Other",
                    format=MediaFormat.TXT,
                    visible=False,
                )
            elif problem == "ambiguous":
                (directory / "Other.txt").write_text("Another body")
            else:
                await MediaItem.filter(id=first.id).update(dir=str(directory))
            before = await MediaItem.all().values()
            with pytest.raises(ContentError, match=code):
                await MediaItemService.move_reading_file(
                    lib.id, source.path, destination
                )
            assert await MediaItem.all().values() == before
            assert destination.read_text() == "Body"

    asyncio.run(run())


@pytest.mark.parametrize("change", ["removed", "case", "new_body"])
def test_move_file_source_container(tmp_path, change):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            first = await MediaItemService.create_reading(lib.id, source)
            directory = source.directory.with_name("Moved")
            if change == "case":
                directory = source.directory.with_name(source.directory.name.lower())
                source.directory.rename(directory)
            else:
                directory.mkdir()
                source.path.rename(directory / source.path.name)
                if change == "removed":
                    source.directory.rmdir()
                else:
                    (source.directory / "Other.txt").write_text("New work")
            destination = directory / source.path.name
            current = await MediaItemService.move_reading_file(
                lib.id, source.path, destination
            )
            assert current is not None and current.id == first.id
            assert current.dir == str(directory) and current.path == str(destination)
            assert not await MediaItemService.ingest_reading_work(lib.id, directory)
            if change == "new_body":
                assert not await MediaItemService.ingest_reading_work(
                    lib.id, source.directory
                )
                other = await MediaItem.get(path=str(source.directory / "Other.txt"))
                assert other.id != first.id
            assert destination.read_text() == "Body"

    asyncio.run(run())


def test_move_file_standalone(tmp_path):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.ZIP, chapter=True)
            await MediaItemService.ingest_reading_work(lib.id, source.directory.parent)
            other = Path(lib.dir) / "Other"
            directory = other
            directory.mkdir(parents=True)
            destination = directory / source.path.name
            source.path.rename(destination)
            before = await MediaItem.all().values()
            with pytest.raises(ValueError, match="move paths"):
                await MediaItemService.move_reading_file(
                    lib.id, source.path, destination
                )
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat]
    + [
        (format, True) for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
    ],
)
def test_rename_reading_work(tmp_path, monkeypatch, format, chapter):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            novel = lib.lib_type == LibType.NOVEL
            metadata = work / ("metadata.opf" if novel else "ComicInfo.xml")
            metadata.write_text(
                '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
                'xmlns:dc="http://purl.org/dc/elements/1.1/">'
                "<dc:title>Manual</dc:title></metadata></package>"
                if novel
                else "<ComicInfo><Title>Manual</Title>"
                "<Writer>Author</Writer></ComicInfo>"
            )
            (work / "cover.png").write_bytes(_PNG)
            if chapter:
                other = work / "Other"
                other.mkdir()
                with zipfile.ZipFile(other / "Other.zip", "w") as archive:
                    archive.writestr("1.png", _PNG)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            first = await MediaItem.get(path=str(source.path))
            await MediaItem.filter(id=first.id).update(
                visible=False, index_error="content_changed"
            )
            before = {item.id: item for item in await MediaItem.all()}
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user,
                rel_id=first.parent_id or first.id,
                rel_type=HistoryType.TEXT if novel else HistoryType.IMAGE,
                percentage=25,
                locator={"chapter_item_id": first.id, "version": first.index_version},
            )
            history = await UserHistory.all().values()
            files = {
                path.relative_to(work): path.read_bytes()
                for path in work.rglob("*")
                if path.is_file()
            }
            destination = work.with_name("Renamed")
            work.rename(destination)
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            await MediaEvent.create(
                lib_id=lib.id,
                event_type="moved",
                src_path=str(work),
                dest_path=str(destination),
                is_directory=True,
            )
            tasks = await coalesce_reading_events(lib.id)
            assert len(tasks) == 2
            payload = ReadingReconcile.model_validate(tasks[0].payload)
            move = payload.moves[0]
            renamed = await MediaItemService.move_reading_directory(
                lib.id, Path(move.src_path), Path(move.dest_path)
            )
            assert {item.id for item in renamed} == set(before)
            for item in renamed:
                previous = before[item.id]
                assert item.path == str(
                    destination / Path(previous.path).relative_to(work)
                )
                assert item.dir == str(
                    destination / Path(previous.dir).relative_to(work)
                )
                assert item.name == (
                    "Renamed" if previous.path == str(work) else previous.name
                )
                assert item.parent_id == previous.parent_id
                if item.parent_id is not None:
                    assert item.parent is not None
                    assert item.parent.path == str(destination)
                assert item.visible == previous.visible
                assert item.title == previous.title and item.extra == previous.extra
                assert item.index_version == previous.index_version
                assert item.size == previous.size
                assert item.index_state == IndexState.PENDING
                assert item.index_error is None
                if item.format is not None:
                    assert _cache(previous).is_dir()
            saved = await MediaItem.all().values()
            assert not await MediaItemService.move_reading_directory(
                lib.id, work, destination
            )
            assert await MediaItem.all().values() == saved
            assert not await MediaItemService.ingest_reading_work(lib.id, destination)
            for item in await MediaItem.all():
                assert item.index_state == IndexState.READY
                if item.format is not None:
                    assert item.index_version != before[item.id].index_version
            current = await MediaItem.get(id=first.id)
            assert current.parent_id == first.parent_id and not current.visible
            assert await MediaItem.all().count() == len(before)
            assert await UserHistory.all().values() == history
            assert {
                path.relative_to(destination): path.read_bytes()
                for path in destination.rglob("*")
                if path.is_file()
            } == files
            assert await MediaEvent.all().count() == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    ("chapter", "only_chapter"), [(False, False), (True, False), (True, True)]
)
def test_rename_directory_empty(tmp_path, chapter, only_chapter):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=chapter)
            work = source.parent_path or source.directory
            await MediaItemService.ingest_reading_work(lib.id, work)
            first = await MediaItem.get(path=str(source.path))
            (source.directory / "1.png").unlink()
            previous = source.directory if only_chapter else work
            destination = previous.with_name("Empty")
            previous.rename(destination)
            items = await MediaItemService.move_reading_directory(
                lib.id, previous, destination
            )
            assert {item.id for item in items} == (
                {first.id}
                if only_chapter
                else {item.id for item in await MediaItem.all()}
            )
            issues = await MediaItemService.ingest_reading_work(
                lib.id, work if only_chapter else destination
            )
            assert not issues
            await first.refresh_from_db()
            assert first.index_state == IndexState.EMPTY
            assert first.index_version is not None and _cache(first).is_dir()
            assert await MediaItem.all().count() == (2 if chapter else 1)

    asyncio.run(run())


@pytest.mark.parametrize("chapter", [False, True])
def test_rename_directory_case(tmp_path, chapter):
    async def run():
        async with _database():
            lib, source = await _source(
                tmp_path,
                MediaFormat.ZIP if chapter else MediaFormat.TXT,
                chapter=chapter,
            )
            if source.parent_path is not None:
                await MediaItemService.create_reading(
                    lib.id, ReadingSource(source.parent_path, None)
                )
            first = await MediaItemService.create_reading(lib.id, source)
            work = source.directory
            # similarly named rows must survive SQLite's case-insensitive LIKE
            other = await MediaItem.create(
                lib=lib,
                parent_id=first.parent_id,
                path=str(work.with_name(work.name.upper()) / "Other.txt"),
                dir=str(work.with_name(work.name.upper())),
                name="Other",
                format=MediaFormat.TXT,
            )
            saved = await MediaItem.filter(id=other.id).values()
            destination = work.with_name(work.name.lower())
            work.rename(destination)
            items = await MediaItemService.move_reading_directory(
                lib.id, work, destination
            )
            assert [item.id for item in items] == [first.id]
            assert items[0].path == str(destination / source.path.name)
            assert await MediaItem.filter(id=other.id).values() == saved

    asyncio.run(run())


@pytest.mark.parametrize(
    ("problem", "code"),
    [
        ("reused", "content_changed"),
        ("source_link", "content_changed"),
        ("owned", "ambiguous_layout"),
        ("missing", "media_source_unavailable"),
        ("chapter_missing", "media_source_unavailable"),
        ("target_link", "media_source_unavailable"),
        ("ambiguous", "ambiguous_layout"),
        ("parent", "unsupported_layout"),
        ("directory", "unsupported_layout"),
        ("outside_child", "unsupported_layout"),
    ],
)
def test_rename_work_conflict(tmp_path, problem, code):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.directory.parent
            await MediaItemService.ingest_reading_work(lib.id, work)
            destination = work.with_name("Renamed")
            work.rename(destination)
            if problem == "reused":
                work.mkdir()
            elif problem == "source_link":
                work.symlink_to(destination, target_is_directory=True)
            elif problem == "owned":
                await MediaItem.create(
                    lib=lib,
                    path=str(destination / "Unrelated"),
                    dir=str(destination / "Unrelated"),
                    name="Other",
                    format=MediaFormat.DIR,
                )
            elif problem == "missing":
                shutil.rmtree(destination)
            elif problem == "chapter_missing":
                shutil.rmtree(destination / "Chapter")
            elif problem == "target_link":
                outside = tmp_path / "Outside"
                destination.rename(outside)
                destination.symlink_to(outside, target_is_directory=True)
            elif problem == "ambiguous":
                (destination / "Chapter/Book.zip").write_bytes(b"ambiguous")
            elif problem == "parent":
                await MediaItem.filter(path=str(source.path)).update(parent_id=None)
            elif problem == "directory":
                await MediaItem.filter(path=str(source.path)).update(dir=str(tmp_path))
            else:
                outside = work.with_name("Outside") / "Chapter"
                await MediaItem.filter(path=str(source.path)).update(
                    path=str(outside), dir=str(outside)
                )
            before = await MediaItem.all().values()
            with pytest.raises(ContentError, match=code):
                await MediaItemService.move_reading_directory(lib.id, work, destination)
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem", ["relative", "outside", "hidden", "same", "chapter", "traversal"]
)
def test_rename_work_paths(tmp_path, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.create_reading(lib.id, source)
            work, destination = source.directory, source.directory.with_name("Renamed")
            if problem == "relative":
                work, destination = Path(work.name), Path(destination.name)
            elif problem == "outside":
                destination = tmp_path / "Outside"
            elif problem == "hidden":
                destination = destination.with_name(".Hidden")
            elif problem == "same":
                destination = work
            elif problem == "chapter":
                work, destination = work / "Chapter", work / "Renamed"
            else:
                destination = work / "../Renamed"
            before = await MediaItem.all().values()
            with pytest.raises(ValueError, match="move paths"):
                await MediaItemService.move_reading_directory(lib.id, work, destination)
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("video", [False, True])
def test_rename_work_unregistered(tmp_path, video):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            work = source.directory
            destination = work.with_name("Renamed")
            work.rename(destination)
            if video:
                await MediaLib.filter(id=lib.id).update(lib_type=LibType.MOVIE)
                with pytest.raises(ContentError, match="unsupported_media_format"):
                    await MediaItemService.move_reading_directory(
                        lib.id, work, destination
                    )
            else:
                assert not await MediaItemService.move_reading_directory(
                    lib.id, work, destination
                )
            assert await MediaItem.all().count() == 0
            assert (destination / source.path.name).read_text() == "Body"

    asyncio.run(run())


@pytest.mark.parametrize("change", ["replace", "reuse", "cancel", "write_failure"])
@pytest.mark.parametrize("scope", ["work", "chapter", "other_work"])
def test_move_directory_unstable(tmp_path, monkeypatch, change, scope):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.directory.parent
            await MediaItemService.ingest_reading_work(lib.id, work)
            only_chapter = scope != "work"
            previous = source.directory if only_chapter else work
            destination = previous.with_name("Renamed")
            if scope == "other_work":
                other = work.with_name("Other")
                other.mkdir()
                destination = other / "Chapter"
            previous.rename(destination)
            if scope == "other_work":
                await MediaItemService.create_reading(
                    lib.id, ReadingSource(destination.parent, None)
                )
            before = await MediaItem.all().values()
            handler = get_handler(LibType.COMIC)
            scan = handler.scan_sources

            def changed(base_path, *, work_path=None):
                """Change the directory after discovery while guards remain active.

                Args:
                    base_path: The library root passed to source discovery.
                    work_path: The selected work, or None for a library scan.

                Returns:
                    The discovery result before changing the filesystem.

                Raises:
                    asyncio.CancelledError: If cancellation is selected.
                """
                result = scan(base_path, work_path=work_path)
                if change == "cancel":
                    raise asyncio.CancelledError
                if change == "replace":
                    chapter = destination if only_chapter else destination / "Chapter"
                    chapter.rename(chapter.with_name("Moved"))
                    chapter.mkdir()
                    (chapter / "1.png").write_bytes(_PNG)
                else:
                    previous.mkdir(exist_ok=True)
                return result

            async def failed(items, fields):
                """Fail after a partial database write to verify transaction rollback.

                Args:
                    items: Proposed items from the batch update.
                    fields: Fields to persist for each renamed item.

                Raises:
                    RuntimeError: After saving the first proposed item.
                """
                await items[0].save(update_fields=fields)
                raise RuntimeError("write failed")

            if change == "write_failure":
                monkeypatch.setattr(MediaItem, "bulk_update", failed)
            else:
                monkeypatch.setattr(handler, "scan_sources", changed)
            with pytest.raises(
                asyncio.CancelledError
                if change == "cancel"
                else RuntimeError
                if change == "write_failure"
                else ContentError
            ):
                await MediaItemService.move_reading_directory(
                    lib.id, previous, destination
                )
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP])
def test_rename_reading_chapter(tmp_path, monkeypatch, format):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=True)
            work = source.directory.parent
            sibling = work / "Chapter 2"
            sibling.mkdir()
            (sibling / "1.png").write_bytes(_PNG)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            first = await MediaItem.get(path=str(source.path))
            await MediaItem.filter(id=first.id).update(visible=False)
            untouched = await MediaItem.exclude(id=first.id).values()
            sibling_row = await MediaItem.filter(path=str(sibling)).values()
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user,
                rel_id=first.parent_id,
                rel_type=HistoryType.IMAGE,
                percentage=25,
                locator={"chapter_item_id": first.id, "version": first.index_version},
            )
            history = await UserHistory.all().values()
            destination = source.directory.with_name("Chapter 10")
            source.directory.rename(destination)
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            await MediaEvent.create(
                lib_id=lib.id,
                event_type="moved",
                src_path=str(source.directory),
                dest_path=str(destination),
                is_directory=True,
            )
            task = (await coalesce_reading_events(lib.id))[0]
            payload = ReadingReconcile.model_validate(task.payload)
            assert set(payload.targets) == {str(source.directory), str(destination)}
            move = payload.moves[0]
            renamed = await MediaItemService.move_reading_directory(
                lib.id, Path(move.src_path), Path(move.dest_path)
            )
            assert [item.id for item in renamed] == [first.id]
            current = renamed[0]
            assert current.path == str(
                destination
                if format == MediaFormat.DIR
                else destination / source.path.name
            )
            assert current.dir == str(destination) and not current.visible
            assert current.name == (
                "Chapter 10" if format == MediaFormat.DIR else "Book"
            )
            assert current.parent_id == first.parent_id
            assert current.parent is not None and current.parent.path == str(work)
            assert current.index_state == IndexState.PENDING
            assert current.index_version == first.index_version
            assert await MediaItem.exclude(id=first.id).values() == untouched
            saved = await MediaItem.all().values()
            assert not await MediaItemService.move_reading_directory(
                lib.id, source.directory, destination
            )
            assert await MediaItem.all().values() == saved
            assert not await MediaItemService.ingest_reading_work(
                lib.id,
                work,
                targets={Path(path) for path in payload.targets},
                force_targets={Path(path) for path in payload.force_targets},
            )
            await current.refresh_from_db()
            assert current.index_state == IndexState.READY
            assert (
                current.index_version != first.index_version and _cache(first).is_dir()
            )
            assert current.parent_id == first.parent_id and not current.visible
            assert await MediaItem.filter(path=str(sibling)).values() == sibling_row
            parent = await MediaItem.get(id=first.parent_id)
            assert parent.index_state == IndexState.READY and parent.extra is not None
            assert parent.extra["content"]["chapter_count"] == 1
            assert await MediaItem.all().count() == 3
            assert await UserHistory.all().values() == history
            assert await MediaEvent.filter(id=task.id).exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("problem", "code"),
    [
        ("no_parent", "unsupported_layout"),
        ("wrong_parent", "unsupported_layout"),
        ("foreign_parent", "unsupported_layout"),
        ("parent_format", "unsupported_layout"),
        ("owned", "ambiguous_layout"),
        ("parent_body", "ambiguous_layout"),
        ("parent_link", "media_source_unavailable"),
    ],
)
def test_rename_chapter_conflict(tmp_path, problem, code):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.ZIP, chapter=True)
            work = source.directory.parent
            await MediaItemService.ingest_reading_work(lib.id, work)
            parent = await MediaItem.get(path=str(work))
            destination = source.directory.with_name("Renamed")
            source.directory.rename(destination)
            if problem == "no_parent":
                await MediaItem.filter(path=str(source.path)).update(parent_id=None)
            elif problem == "wrong_parent":
                await MediaItem.filter(id=parent.id).update(
                    path=str(work.with_name("Other"))
                )
            elif problem == "foreign_parent":
                other = await MediaLib.create(
                    name="Other",
                    dir=str(tmp_path / "Other"),
                    lib_type=LibType.COMIC,
                    priority=2,
                )
                await MediaItem.filter(id=parent.id).update(lib_id=other.id)
            elif problem == "parent_format":
                await MediaItem.filter(id=parent.id).update(format=MediaFormat.DIR)
            elif problem == "owned":
                await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    dir=str(destination),
                    path=str(destination / "Other.cbz"),
                    name="Other",
                    format=MediaFormat.CBZ,
                )
            elif problem == "parent_body":
                (work / "1.png").write_bytes(_PNG)
            else:
                outside = tmp_path / "Outside"
                work.rename(outside)
                work.symlink_to(outside, target_is_directory=True)
            before = await MediaItem.all().values()
            with pytest.raises(ContentError, match=code):
                await MediaItemService.move_reading_directory(
                    lib.id, source.directory, destination
                )
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["standalone", "deep", "hidden_parent"])
def test_rename_chapter_paths(tmp_path, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.directory.parent
            await MediaItemService.ingest_reading_work(lib.id, work)
            previous, destination = source.directory, work / "Renamed"
            if problem == "standalone":
                destination = work.with_name("Other")
            elif problem == "deep":
                previous, destination = previous / "Child", previous / "Renamed"
            else:
                hidden = work.with_name(".Hidden")
                previous, destination = hidden / "Chapter", hidden / "Renamed"
            before = await MediaItem.all().values()
            with pytest.raises(ValueError, match="move paths"):
                await MediaItemService.move_reading_directory(
                    lib.id, previous, destination
                )
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "directory_move"),
    [(format, True) for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)]
    + [(format, False) for format in (MediaFormat.CBZ, MediaFormat.ZIP)],
)
@pytest.mark.parametrize("visible", [False, True])
def test_move_chapter_work(tmp_path, monkeypatch, format, directory_move, visible):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=True)
            work = source.directory.parent
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            first = await MediaItem.get(path=str(source.path))
            await MediaItem.filter(id=first.id).update(visible=visible)
            other = work.with_name("Other")
            sibling = other / "Sibling"
            sibling.mkdir(parents=True)
            (sibling / "1.png").write_bytes(_PNG)
            assert not await MediaItemService.ingest_reading_work(lib.id, other)
            target = await MediaItem.get(path=str(other))
            sibling_row = await MediaItem.filter(path=str(sibling)).values()
            untouched = await MediaItem.exclude(id=first.id).values()
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            for parent_id, child_id in (
                (first.parent_id, first.id),
                (target.id, sibling_row[0]["id"]),
            ):
                await UserHistory.create(
                    user=user,
                    rel_id=parent_id,
                    rel_type=HistoryType.IMAGE,
                    percentage=25,
                    locator={"chapter_item_id": child_id},
                )
            history = await UserHistory.all().values()
            directory = other / "Moved"
            previous = source.directory if directory_move else source.path
            destination = directory if directory_move else directory / source.path.name
            if not directory_move:
                directory.mkdir()
            previous.rename(destination)
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            await MediaEvent.create(
                lib_id=lib.id,
                event_type="moved",
                src_path=str(previous),
                dest_path=str(destination),
                is_directory=directory_move,
            )
            tasks = await coalesce_reading_events(lib.id)
            assert {task.src_path for task in tasks} == {str(work), str(other)}
            operation = (
                MediaItemService.move_reading_directory
                if directory_move
                else MediaItemService.move_reading_file
            )
            move = ReadingReconcile.model_validate(tasks[0].payload).moves[0]
            result = await operation(lib.id, Path(move.src_path), Path(move.dest_path))
            current = result[0] if isinstance(result, list) else result
            assert current is not None and current.id == first.id
            assert current.parent_id == target.id and current.parent is not None
            assert current.parent.path == str(other)
            assert current.path == str(
                directory if format == MediaFormat.DIR else directory / source.path.name
            )
            assert current.dir == str(directory) and current.visible == visible
            assert current.index_state == IndexState.PENDING
            assert current.index_error is None and current.extra == first.extra
            assert current.index_version == first.index_version
            assert await MediaItem.exclude(id=first.id).values() == untouched
            saved = await MediaItem.all().values()
            assert not await operation(lib.id, previous, destination)
            assert await MediaItem.all().values() == saved
            for task in tasks:
                payload = ReadingReconcile.model_validate(task.payload)
                assert not await MediaItemService.ingest_reading_work(
                    lib.id,
                    Path(task.src_path),
                    targets={Path(path) for path in payload.targets},
                    force_targets={Path(path) for path in payload.force_targets},
                )
            await current.refresh_from_db()
            assert current.index_state == IndexState.READY
            assert current.index_version != first.index_version
            assert current.parent_id == target.id and _cache(first).is_dir()
            old = await MediaItem.get(id=first.parent_id)
            assert old.index_state == IndexState.EMPTY and old.extra is not None
            assert old.extra["content"]["chapter_count"] == 0
            await target.refresh_from_db()
            assert target.index_state == IndexState.READY and target.extra is not None
            assert target.extra["content"]["chapter_count"] == (2 if visible else 1)
            assert await MediaItem.filter(path=str(sibling)).values() == sibling_row
            assert await MediaItem.all().count() == 4
            assert await UserHistory.all().values() == history
            assert await MediaEvent.all().count() == 2

    asyncio.run(run())


@pytest.mark.parametrize("directory_move", [False, True])
@pytest.mark.parametrize(
    ("problem", "code"),
    [
        ("missing_parent", "unsupported_layout"),
        ("parent_format", "unsupported_layout"),
        ("parent_dir", "unsupported_layout"),
        ("nested_parent", "unsupported_layout"),
        ("foreign_parent", "unsupported_layout"),
        ("no_old_parent", "unsupported_layout"),
        ("wrong_old_parent", "unsupported_layout"),
        ("owned", "ambiguous_layout"),
        ("parent_body", "ambiguous_layout"),
        ("reuse", "content_changed"),
        ("old_link", "media_source_unavailable"),
        ("new_link", "media_source_unavailable"),
    ],
)
def test_move_chapter_conflict(tmp_path, directory_move, problem, code):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.ZIP, chapter=True)
            work = source.directory.parent
            await MediaItemService.ingest_reading_work(lib.id, work)
            child = await MediaItem.get(path=str(source.path))
            other = work.with_name("Other")
            directory = other / "Chapter"
            other.mkdir()
            previous = source.directory if directory_move else source.path
            destination = directory if directory_move else directory / source.path.name
            if not directory_move:
                directory.mkdir()
            previous.rename(destination)
            parent = await MediaItemService.create_reading(
                lib.id, ReadingSource(other, None)
            )
            if problem == "missing_parent":
                await parent.delete()
            elif problem == "parent_format":
                await MediaItem.filter(id=parent.id).update(format=MediaFormat.DIR)
            elif problem == "parent_dir":
                await MediaItem.filter(id=parent.id).update(dir=str(work))
            elif problem == "nested_parent":
                await MediaItem.filter(id=parent.id).update(parent_id=child.parent_id)
            elif problem == "foreign_parent":
                foreign = await MediaLib.create(
                    name="Foreign",
                    dir=str(tmp_path / "Foreign"),
                    lib_type=LibType.COMIC,
                    priority=2,
                )
                await MediaItem.filter(id=parent.id).update(lib_id=foreign.id)
            elif problem == "no_old_parent":
                await MediaItem.filter(id=child.id).update(parent_id=None)
            elif problem == "wrong_old_parent":
                await MediaItem.filter(id=child.id).update(parent_id=parent.id)
            elif problem == "owned":
                await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    dir=str(directory),
                    path=str(directory / "Missing.cbz"),
                    name="Missing",
                    format=MediaFormat.CBZ,
                    visible=False,
                )
            elif problem == "parent_body":
                (other / "1.png").write_bytes(_PNG)
            elif problem == "reuse":
                if directory_move:
                    previous.mkdir()
                else:
                    previous.write_bytes(destination.read_bytes())
            else:
                linked = work if problem == "old_link" else other
                outside = tmp_path / "Outside"
                linked.rename(outside)
                linked.symlink_to(outside, target_is_directory=True)
            before = await MediaItem.all().values()
            operation = (
                MediaItemService.move_reading_directory
                if directory_move
                else MediaItemService.move_reading_file
            )
            with pytest.raises(ContentError, match=code):
                await operation(lib.id, previous, destination)
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("parent_body", [False, True])
def test_move_empty_chapter(tmp_path, parent_body):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.directory.parent
            await MediaItemService.ingest_reading_work(lib.id, work)
            child = await MediaItem.get(path=str(source.path))
            other = work.with_name("Other")
            other.mkdir()
            destination = other / "Chapter"
            source.path.rename(destination)
            parent = await MediaItemService.create_reading(
                lib.id, ReadingSource(other, None)
            )
            (destination / "1.png").unlink()
            work.rmdir()
            if parent_body:
                (other / "1.png").write_bytes(_PNG)
                before = await MediaItem.all().values()
                with pytest.raises(ContentError, match="unsupported_layout"):
                    await MediaItemService.move_reading_directory(
                        lib.id, source.path, destination
                    )
                assert await MediaItem.all().values() == before
            else:
                moved = await MediaItemService.move_reading_directory(
                    lib.id, source.path, destination
                )
                assert [item.id for item in moved] == [child.id]
                assert moved[0].parent_id == parent.id
                assert not await MediaItemService.ingest_reading_work(lib.id, other)
                await child.refresh_from_db()
                assert child.index_state == IndexState.EMPTY

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "scope"),
    [(format, "work") for format in MediaFormat]
    + [
        (format, scope)
        for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
        for scope in ("chapter", "last_chapter", "collection")
    ],
)
@pytest.mark.parametrize("scan", [False, True])
def test_consume_directory_delete(tmp_path, monkeypatch, format, scope, scan):
    """Reconcile absent containers while preserving other works and histories.

    Args:
        tmp_path: The isolated library and cache root.
        monkeypatch: The fixture controlling notifications and event time.
        format: The body format in the removed container.
        scope: A standalone work, comic chapter, last chapter or whole collection.
        scan: Whether startup scanning rediscovers the loss without a delete event.
    """

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=scope != "work")
            work = source.parent_path or source.directory
            if scope in ("chapter", "collection"):
                sibling = work / "Sibling"
                sibling.mkdir()
                (sibling / "1.png").write_bytes(_PNG)
            note = work / "notes.md"
            note.write_text("Keep me")
            other = work.with_name("Other")
            other.mkdir()
            novel = lib.lib_type == LibType.NOVEL
            (other / ("Other.txt" if novel else "1.png")).write_bytes(
                b"Other body" if novel else _PNG
            )
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            assert not await MediaItemService.ingest_reading_work(lib.id, other)
            item = await MediaItem.get(path=str(source.path))
            await MediaItem.filter(id=item.id).update(visible=False)
            directory = source.directory if "chapter" in scope else work
            owned = [
                row
                for row in await MediaItem.all()
                if Path(row.dir).is_relative_to(directory)
            ]
            retained = await MediaItem.exclude(
                id__in=[row.id for row in owned]
                + ([item.parent_id] if item.parent_id else [])
            ).values()
            caches = [_cache(row).parent for row in owned if row.format is not None]
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user,
                rel_id=item.parent_id or item.id,
                rel_type=HistoryType.TEXT if novel else HistoryType.IMAGE,
                locator={"chapter_item_id": item.id},
            )
            history = await UserHistory.create(
                user=user,
                rel_id=item.id,
                rel_type=HistoryType.VIDEO,
            )
            shutil.rmtree(directory)
            clock = [100.0]
            monkeypatch.setattr(media_events, "time", lambda: clock[0])
            monkeypatch.setattr(media_watcher, "time", lambda: clock[0])
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            if not scan:
                await MediaEvent.create(
                    lib=lib,
                    src_path=str(directory),
                    event_type="deleted",
                    is_directory=True,
                )
            tasks = await coalesce_reading_events(
                lib.id, scan_works={work} if scan else None
            )
            assert len(tasks) == 1
            task = tasks[0]
            assert not await prepare_reading_event(task.id)
            clock[0] += 2
            assert await prepare_reading_event(task.id)
            assert await media_watcher.consume_event(
                await MediaEvent.get(id=task.id).select_related("lib")
            )
            assert not await MediaItem.filter(id__in=[row.id for row in owned]).exists()
            assert all(not cache.exists() for cache in caches)
            assert (
                await MediaItem.filter(id__in=[row["id"] for row in retained]).values()
                == retained
            )
            assert await UserHistory.all().values_list("id", flat=True) == [history.id]
            if "chapter" in scope:
                parent = await MediaItem.get(id=item.parent_id)
                assert parent.index_state == (
                    IndexState.READY if scope == "chapter" else IndexState.EMPTY
                )
                assert parent.extra is not None
                assert parent.extra["content"]["chapter_count"] == int(
                    scope == "chapter"
                )
                assert note.read_text() == "Keep me"
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat if format != MediaFormat.DIR]
    + [(MediaFormat.CBZ, True), (MediaFormat.ZIP, True)],
)
@pytest.mark.parametrize("scan", [False, True])
def test_consume_file_cleanup(tmp_path, monkeypatch, format, chapter, scan):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=chapter)
            work = source.parent_path or source.directory
            novel = lib.lib_type == LibType.NOVEL
            metadata = source.directory / ("metadata.opf" if novel else "ComicInfo.xml")
            metadata.write_text(
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                '<metadata/><manifest><item id="cover" properties="cover-image" '
                'href="art/custom.png" media-type="image/png"/></manifest></package>'
                if novel
                else "<ComicInfo><Title>Chapter</Title></ComicInfo>"
            )
            cover = source.directory / "cover.png"
            cover.write_bytes(_PNG)
            if novel:
                (source.directory / "art").mkdir()
                (source.directory / "art/custom.png").write_bytes(_PNG)
            parent_xml = work / "ComicInfo.xml"
            if chapter:
                parent_xml.write_text(
                    "<ComicInfo><Title>Collection</Title></ComicInfo>"
                )
            note = source.directory / "notes.md"
            note.write_text("Keep me")
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            item = await MediaItem.get(path=str(source.path))
            await MediaItem.filter(id=item.id).update(visible=False)
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user,
                rel_id=item.parent_id or item.id,
                rel_type=HistoryType.TEXT if novel else HistoryType.IMAGE,
                locator={"chapter_item_id": item.id},
            )
            kept = await UserHistory.create(
                user=user, rel_id=item.id, rel_type=HistoryType.VIDEO
            )
            removed = []

            def remove(path):
                """Observe cleanup while isolating application trash settings.

                Args:
                    path: The owned companion selected for deletion.
                """
                removed.append(path)
                path.unlink()

            clock = [100.0]
            monkeypatch.setattr(media_service, "delete_path", remove)
            monkeypatch.setattr(media_events, "time", lambda: clock[0])
            monkeypatch.setattr(media_watcher, "time", lambda: clock[0])
            monkeypatch.setattr(media_events, "notify_media_events", lambda _id: None)
            source.path.unlink()
            if not scan:
                await MediaEvent.create(
                    lib=lib, src_path=str(source.path), event_type="deleted"
                )
            task = (
                await coalesce_reading_events(
                    lib.id, scan_works={work} if scan else None
                )
            )[0]
            assert not await prepare_reading_event(task.id)
            clock[0] += 2
            assert await prepare_reading_event(task.id)
            assert await media_watcher.consume_event(
                await MediaEvent.get(id=task.id).select_related("lib")
            )
            assert not await MediaItem.filter(id=item.id).exists()
            assert not _cache(item).parent.exists()
            assert await UserHistory.all().values_list("id", flat=True) == [kept.id]
            assert removed[-1] == metadata
            assert set(removed) == {metadata, cover} | (
                {source.directory / "art/custom.png"} if novel else set()
            )
            assert note.read_text() == "Keep me" and Path(lib.dir).is_dir()
            if novel:
                assert not (source.directory / "art").exists()
            if chapter:
                parent = await MediaItem.get(id=item.parent_id)
                assert parent.index_state == IndexState.EMPTY
                assert (
                    parent_xml.read_text()
                    == "<ComicInfo><Title>Collection</Title></ComicInfo>"
                )
            clock[0] += 2
            assert await prepare_reading_event(task.id)
            assert await media_watcher.consume_event(
                await MediaEvent.get(id=task.id).select_related("lib")
            )
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem",
    [
        "replacement",
        "hidden_body",
        "owner",
        "nested_owner",
        "captured_path",
        "metadata_link",
    ],
)
def test_companion_ownership(tmp_path, monkeypatch, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            source.path.unlink()
            metadata = source.directory / "metadata.opf"
            metadata.write_text("<package><metadata/></package>")
            cover = source.directory / "cover.png"
            cover.write_bytes(_PNG)
            if problem in {"replacement", "hidden_body"}:
                (
                    source.directory
                    / ("Other.epub" if problem == "replacement" else ".hidden.txt")
                ).write_text("New body")
            elif problem in {"owner", "nested_owner"}:
                directory = (
                    source.directory / "Nested"
                    if problem == "nested_owner"
                    else source.directory
                )
                await MediaItem.create(
                    lib=lib,
                    dir=str(directory),
                    path=str(directory / "Other.txt"),
                    name="Other",
                    format=MediaFormat.TXT,
                )
            elif problem == "metadata_link":
                metadata.unlink()
                metadata.symlink_to(tmp_path / "missing.opf")
            before = await MediaItem.all().values()
            remove = AsyncMock(side_effect=AssertionError("unexpected deletion"))
            monkeypatch.setattr(media_service, "delete_path", remove)
            with pytest.raises(ContentError):
                await MediaItemService.remove_missing_reading_item(
                    item.id,
                    source_path=source.path.with_name("Old.txt")
                    if problem == "captured_path"
                    else source.path,
                )
            assert await MediaItem.all().values() == before
            assert _cache(item).is_dir() and cover.read_bytes() == _PNG
            assert metadata.exists() or metadata.is_symlink()
            remove.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["restore", "replace", "metadata", "permission"])
def test_companion_retry(tmp_path, monkeypatch, change):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            source.path.unlink()
            metadata = source.directory / "metadata.opf"
            metadata.write_text("<package><metadata/></package>")
            cover = source.directory / "cover.png"
            cover.write_bytes(_PNG)
            before = await MediaItem.all().values()

            def remove(path):
                """Change the source or fail between cover and metadata removal.

                Args:
                    path: The companion selected by the guarded cleanup.

                Raises:
                    PermissionError: Before deleting XML in the permission case.
                """
                if path == metadata and change == "permission":
                    raise PermissionError("denied")
                assert path == cover
                path.unlink()
                if change == "restore":
                    source.path.write_text("Restored")
                elif change == "replace":
                    source.directory.rename(source.directory.with_name("Previous"))
                    source.directory.mkdir()
                    metadata.write_text("New owner's metadata")
                elif change == "metadata":
                    metadata.write_text("Updated metadata")

            monkeypatch.setattr(media_service, "delete_path", remove)
            with pytest.raises(ContentError):
                await MediaItemService.remove_missing_reading_item(item.id)
            assert await MediaItem.all().values() == before
            assert metadata.exists() and _cache(item).is_dir()
            if change in {"permission", "metadata"}:
                monkeypatch.setattr(
                    media_service, "delete_path", lambda path: path.unlink()
                )
                assert await MediaItemService.remove_missing_reading_item(item.id) == [
                    item.id
                ]
                assert (
                    not source.directory.exists() and not _cache(item).parent.exists()
                )

    asyncio.run(run())


@pytest.mark.parametrize("trash", [False, True])
def test_companion_trash(tmp_path, monkeypatch, trash):
    async def run():
        async with _database():
            from app.utils import disk

            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            source.path.unlink()
            metadata = source.directory / "metadata.opf"
            metadata.write_text("<package><metadata/></package>")
            cover = source.directory / "cover.png"
            cover.write_bytes(_PNG)
            recycled = tmp_path / "Trash"
            recycled.mkdir()

            def recycle(path):
                """Model the OS trash without writing outside the isolated test root.

                Args:
                    path: The owned file passed to send2trash.
                """
                path.rename(recycled / path.name)

            monkeypatch.setattr(
                KaloscopeConfig,
                "get",
                lambda: SimpleNamespace(filesystem_trash_mode=trash),
            )
            monkeypatch.setattr(disk, "send2trash", recycle)
            assert await MediaItemService.remove_missing_reading_item(item.id) == [
                item.id
            ]
            assert not source.directory.exists() and Path(lib.dir).is_dir()
            assert {path.name for path in recycled.iterdir()} == (
                {"metadata.opf", "cover.png"} if trash else set()
            )

    asyncio.run(run())


def test_companion_interrupted(tmp_path, monkeypatch):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            source.path.unlink()
            metadata = source.directory / "metadata.opf"
            metadata.write_text("<package><metadata/></package>")
            before = await MediaItem.all().values()
            original = media_service.write_in_thread

            async def interrupted(function, *args, **kwargs):
                """Cancel after companion writes finish and before database cleanup.

                Args:
                    function: The filesystem worker being executed.
                    *args: Positional worker arguments.
                    **kwargs: Keyword worker arguments.

                Returns:
                    The result of workers other than companion cleanup.

                Raises:
                    asyncio.CancelledError: After companions have been removed.
                """
                result = await original(function, *args, **kwargs)
                if function is media_service._remove_reading_companions:
                    raise asyncio.CancelledError()
                return result

            monkeypatch.setattr(
                media_service, "delete_path", lambda path: path.unlink()
            )
            with monkeypatch.context() as patch:
                patch.setattr(media_service, "write_in_thread", interrupted)
                with pytest.raises(asyncio.CancelledError):
                    await MediaItemService.remove_missing_reading_item(item.id)
            assert await MediaItem.all().values() == before
            assert not metadata.exists() and _cache(item).is_dir()
            assert await MediaItemService.remove_missing_reading_item(item.id) == [
                item.id
            ]
            assert not _cache(item).parent.exists() and not source.directory.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "scope"),
    [(format, "work") for format in MediaFormat]
    + [
        (format, scope)
        for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
        for scope in ("chapter", "last_chapter", "collection")
    ],
)
@pytest.mark.parametrize("local", [False, True])
def test_delete_reading(tmp_path, monkeypatch, format, scope, local):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=scope != "work")
            work = source.parent_path or source.directory
            novel = lib.lib_type == LibType.NOVEL
            bodies = [
                source.path / "1.png" if format == MediaFormat.DIR else source.path
            ]
            directories = [source.directory]
            if scope in {"chapter", "collection"}:
                other = work / "Other"
                other.mkdir()
                (other / "1.png").write_bytes(_PNG)
                directories.append(other)
                bodies.append(other / "1.png")
            if source.parent_path is not None:
                directories.append(work)
            for directory in directories:
                (directory / "notes.md").write_text("Keep me")
                (directory / "cover.png").write_bytes(_PNG)
                (directory / ("metadata.opf" if novel else "ComicInfo.xml")).write_text(
                    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                    '<metadata/><manifest><item id="cover" properties="cover-image" '
                    'href="art/front.png" media-type="image/png"/></manifest></package>'
                    if novel
                    else "<ComicInfo><Title>Owned</Title></ComicInfo>"
                )
            if novel:
                (source.directory / "art").mkdir()
                (source.directory / "art/front.png").write_bytes(_PNG)
                (source.directory / "unrelated.png").write_bytes(_PNG)
            assert not await MediaItemService.ingest_reading_work(lib.id, work)
            item = await MediaItem.get(path=str(source.path))
            target = item.parent_id if scope == "collection" else item.id
            assert target is not None
            selected = await MediaItem.filter(Q(id=target) | Q(parent_id=target))
            selected_ids = {row.id for row in selected}
            selected_dirs = {Path(row.dir) for row in selected}
            caches = [_cache(row).parent for row in selected if row.format is not None]
            if scope == "collection":
                await MediaItem.filter(id=item.id).update(visible=False)
            before = await MediaItem.all().count()
            files = {
                path: path.read_bytes() for path in work.rglob("*") if path.is_file()
            }
            owned = {
                path
                for path in files
                if any(path.is_relative_to(directory) for directory in selected_dirs)
            }
            owned -= {
                path for path in owned if path.name in {"notes.md", "unrelated.png"}
            }
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user,
                rel_id=item.parent_id or item.id,
                rel_type=HistoryType.TEXT if novel else HistoryType.IMAGE,
                locator={"chapter_item_id": item.id},
            )
            video = await UserHistory.create(
                user=user, rel_id=item.id, rel_type=HistoryType.VIDEO
            )
            history_before = await UserHistory.all().values()
            removed = []
            main_thread = threading.get_ident()

            def remove(path):
                """Delete only files in a filesystem worker.

                Args:
                    path: The body or companion selected by the real deletion path.
                """
                assert threading.get_ident() != main_thread
                assert path.is_file()
                removed.append(path)
                path.unlink()

            recover = AsyncMock(side_effect=AssertionError("unexpected video recovery"))
            monkeypatch.setattr("app.core.media.organizer.recover_organizing", recover)
            monkeypatch.setattr(media_service, "delete_path", remove)
            await MediaItemService.delete(target, local=local)
            recover.assert_not_awaited()
            assert Path(lib.dir).is_dir()
            if local:
                assert set(removed) == owned
                selected_bodies = set(bodies) & owned
                assert set(removed[: len(selected_bodies)]) == selected_bodies
                assert await MediaItem.all().count() == before - len(selected_ids)
                assert not await MediaItem.filter(id__in=selected_ids).exists()
                assert not await UserHistory.filter(id=history.id).exists()
                assert await UserHistory.filter(id=video.id).exists()
                assert all(not cache.exists() for cache in caches)
                for path, content in files.items():
                    assert (
                        not path.exists()
                        if path in owned
                        else path.read_bytes() == content
                    )
                if novel:
                    assert not (source.directory / "art").exists()
            else:
                assert not removed and await MediaItem.all().count() == before
                assert not await MediaItem.filter(
                    id__in=selected_ids, visible=True
                ).exists()
                assert await UserHistory.all().values() == history_before
                assert all(cache.is_dir() for cache in caches)
                assert all(
                    path.read_bytes() == content for path, content in files.items()
                )
                assert not await MediaItemService.ingest_reading_work(lib.id, work)
                assert not await MediaItem.filter(
                    id__in=selected_ids, visible=True
                ).exists()
            if scope in {"chapter", "last_chapter"}:
                parent = await MediaItem.get(id=item.parent_id)
                assert parent.index_state == (
                    IndexState.READY if scope == "chapter" else IndexState.EMPTY
                )
                assert parent.extra is not None
                assert parent.extra["content"]["chapter_count"] == int(
                    scope == "chapter"
                )
                assert parent.visible == (local or scope == "chapter")
            if local:
                clock = [100.0]
                monkeypatch.setattr(media_events, "time", lambda: clock[0])
                monkeypatch.setattr(media_watcher, "time", lambda: clock[0])
                monkeypatch.setattr(
                    media_events, "notify_media_events", lambda _id: None
                )
                await MediaEvent.bulk_create(
                    [
                        MediaEvent(lib=lib, src_path=str(path), event_type="deleted")
                        for path in removed
                    ]
                )
                tasks = await coalesce_reading_events(lib.id)
                assert len(tasks) == 1
                task = tasks[0]
                assert not await prepare_reading_event(task.id)
                clock[0] += 2
                assert await prepare_reading_event(task.id)
                assert await media_watcher.consume_event(
                    await MediaEvent.get(id=task.id).select_related("lib")
                )
                assert not await MediaEvent.exists()
                assert await MediaItem.all().count() == before - len(selected_ids)
                assert not await MediaItem.filter(id__in=selected_ids).exists()

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.DIR])
def test_delete_reading_shared_cover(tmp_path, monkeypatch, format):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format)
            metadata = source.directory / (
                "metadata.opf" if format == MediaFormat.TXT else "ComicInfo.xml"
            )
            metadata.write_text(
                '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                '<metadata/><manifest><item id="cover" properties="cover-image" '
                'href="custom.png" media-type="image/png"/></manifest></package>'
                if format == MediaFormat.TXT
                else "<ComicInfo/>"
            )
            preserved = {"Other.nfo": b"<movie/>", "cover.png": _PNG}
            if format == MediaFormat.TXT:
                preserved["custom.png"] = _PNG
            for name, content in preserved.items():
                (source.directory / name).write_bytes(content)
            assert not await MediaItemService.ingest_reading_work(
                lib.id, source.directory
            )
            item = await MediaItem.get(path=str(source.path))
            monkeypatch.setattr(
                media_service, "delete_path", lambda path: path.unlink()
            )
            await MediaItemService.delete(item.id, local=True)
            assert not await MediaItem.exists()
            assert not metadata.exists() and not _cache(item).parent.exists()
            assert {
                path.name: path.read_bytes() for path in source.directory.iterdir()
            } == preserved

    asyncio.run(run())


@pytest.mark.parametrize("format", [None, *MediaFormat])
@pytest.mark.parametrize("trash", [False, True])
def test_delete_empty_container(tmp_path, monkeypatch, format, trash):
    async def run():
        async with _database():
            from app.utils import disk

            lib, source = await _source(tmp_path, format or MediaFormat.DIR)
            if format is None:
                (source.path / "1.png").unlink()
                item = await MediaItem.create(
                    lib=lib,
                    path=str(source.path),
                    dir=str(source.directory),
                    name="Empty",
                    format=None,
                )
            else:
                await MediaItemService.ingest_reading_work(lib.id, source.directory)
                item = await MediaItem.get(path=str(source.path))
            metadata = source.directory / (
                "metadata.opf" if lib.lib_type == LibType.NOVEL else "ComicInfo.xml"
            )
            metadata.write_text("<metadata/>")
            before = {
                path.name: path.read_bytes()
                for path in source.directory.iterdir()
                if path.is_file()
            }
            recycled = tmp_path / "Trash"
            recycled.mkdir()

            def recycle(path):
                """Emulate file recycling inside the isolated test directory.

                Args:
                    path: The body or companion passed to the shared trash helper.
                """
                assert path.is_file()
                path.rename(recycled / path.name)

            monkeypatch.setattr(
                KaloscopeConfig,
                "get",
                lambda: SimpleNamespace(filesystem_trash_mode=trash),
            )
            monkeypatch.setattr(disk, "send2trash", recycle)
            await MediaItemService.delete(item.id, local=True)
            assert not await MediaItem.exists()
            assert not source.directory.exists() and Path(lib.dir).is_dir()
            assert {path.name: path.read_bytes() for path in recycled.iterdir()} == (
                before if trash else {}
            )

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["body", "companion", "cache", "database", "cancel"])
def test_delete_reading_retry(tmp_path, monkeypatch, stage):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.directory.parent
            metadata = source.directory / "ComicInfo.xml"
            metadata.write_text("<ComicInfo/>")
            (source.path / "2.png").write_bytes(_PNG)
            await MediaItemService.ingest_reading_work(lib.id, work)
            item = await MediaItem.get(path=str(source.path))
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user,
                rel_id=item.parent_id,
                rel_type=HistoryType.IMAGE,
                locator={"chapter_item_id": item.id},
            )
            histories = await UserHistory.all().values()
            before = await MediaItem.all().values()
            delete_query = QuerySet.delete
            write = media_service.write_in_thread

            def remove(path):
                """Fail one selected file after earlier writes have completed.

                Args:
                    path: The selected body or companion.

                Raises:
                    PermissionError: At the requested body or companion stage.
                """
                if path.name == (
                    "2.png"
                    if stage == "body"
                    else "ComicInfo.xml"
                    if stage == "companion"
                    else ""
                ):
                    raise PermissionError("denied")
                path.unlink()

            async def interrupted(function, *args, **kwargs):
                """Interrupt after the writer has finished selected file deletion.

                Args:
                    function: The filesystem worker being awaited.
                    *args: Its positional arguments.
                    **kwargs: Its keyword arguments.

                Returns:
                    The filesystem worker result unless cancellation is injected.

                Raises:
                    asyncio.CancelledError: After media files have been removed.
                """
                result = await write(function, *args, **kwargs)
                if function is media_service._delete_reading_files:
                    raise asyncio.CancelledError()
                return result

            def fail_cache(_ids):
                """Keep cache owners available for retry.

                Args:
                    _ids: The selected cache owner IDs.

                Raises:
                    ContentError: For the simulated cache failure.
                """
                raise ContentError("content_not_ready")

            def fail_database(query):
                """Fail media deletion after history changes enter the transaction.

                Args:
                    query: The deletion query under test.

                Returns:
                    The history deletion query.

                Raises:
                    RuntimeError: When removing media records.
                """
                if query.model == MediaItem:
                    raise RuntimeError("database unavailable")
                return delete_query(query)

            monkeypatch.setattr(media_service, "delete_path", remove)
            with monkeypatch.context() as patch:
                if stage == "cache":
                    patch.setattr(media_service, "_remove_reading_caches", fail_cache)
                elif stage == "database":
                    patch.setattr(QuerySet, "delete", fail_database)
                elif stage == "cancel":
                    patch.setattr(media_service, "write_in_thread", interrupted)
                with pytest.raises(
                    asyncio.CancelledError
                    if stage == "cancel"
                    else RuntimeError
                    if stage == "database"
                    else ContentError
                ):
                    await MediaItemService.delete(item.id, local=True)
            assert await MediaItem.all().values() == before
            assert await UserHistory.all().values() == histories
            if stage == "body":
                assert metadata.is_file() and (source.directory / "cover.png").is_file()
            monkeypatch.setattr(
                media_service, "delete_path", lambda path: path.unlink()
            )
            await MediaItemService.delete(item.id, local=True)
            assert not await MediaItem.filter(id=item.id).exists()
            assert not await UserHistory.exists() and not _cache(item).parent.exists()
            assert not source.directory.exists()
            parent = await MediaItem.get(id=item.parent_id)
            assert parent.index_state == IndexState.EMPTY

    asyncio.run(run())


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("change", ["new_child", "moved_child", "library"])
def test_delete_reading_scope(tmp_path, monkeypatch, local, change):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.directory.parent
            metadata = work / "ComicInfo.xml"
            metadata.write_text("<ComicInfo><Title>Collection</Title></ComicInfo>")
            await MediaItemService.ingest_reading_work(lib.id, work)
            parent = await MediaItem.get(path=str(work))
            child = await MediaItem.get(path=str(source.path))
            waiting = asyncio.Event()

            def waiting_lock(directory):
                """Notify the test when deletion has captured the requested IDs.

                Args:
                    directory: The library lock path.

                Returns:
                    The real library lock.
                """
                waiting.set()
                return library_lock(directory)

            monkeypatch.setattr(media_service, "library_lock", waiting_lock)
            monkeypatch.setattr(
                media_service, "delete_path", lambda path: path.unlink()
            )
            extra = None
            current = source.path
            async with library_lock(lib.dir):
                task = asyncio.create_task(
                    MediaItemService.delete(parent.id, local=local)
                )
                try:
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    assert not task.done()
                    if change == "new_child":
                        other = work / "New"
                        other.mkdir()
                        (other / "1.png").write_bytes(_PNG)
                        extra = await MediaItem.create(
                            lib=lib,
                            parent=parent,
                            name="New",
                            path=str(other),
                            dir=str(other),
                            format=MediaFormat.DIR,
                            index_state=IndexState.READY,
                        )
                    elif change == "moved_child":
                        other = work.with_name("Other")
                        other.mkdir()
                        (other / "ComicInfo.xml").write_text("<ComicInfo/>")
                        extra = await MediaItem.create(
                            lib=lib,
                            name="Other",
                            path=str(other),
                            dir=str(other),
                            format=None,
                        )
                        current = other / source.path.name
                        source.path.rename(current)
                        await MediaItem.filter(id=child.id).update(
                            parent_id=extra.id, path=str(current), dir=str(current)
                        )
                    else:
                        await MediaLib.filter(id=lib.id).update(
                            dir=str(work.with_name("Changed"))
                        )
                except BaseException:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    raise
            if change == "library":
                with pytest.raises(ContentError, match="content_changed"):
                    await task
                assert (current / "1.png").read_bytes() == _PNG and metadata.exists()
                assert (await MediaItem.get(id=child.id)).visible
                return
            await asyncio.wait_for(task, timeout=3)
            assert extra is not None and await MediaItem.filter(id=extra.id).exists()
            if local:
                assert not await MediaItem.filter(id=child.id).exists()
                assert not current.exists()
            else:
                assert not (await MediaItem.get(id=child.id)).visible
                assert (current / "1.png").read_bytes() == _PNG
            if change == "new_child":
                saved = await MediaItem.get(id=parent.id)
                assert saved.visible and saved.extra is not None
                assert saved.extra["content"]["chapter_count"] == 1
                assert metadata.exists() and (work / "New/1.png").read_bytes() == _PNG
            else:
                assert (Path(extra.dir) / "ComicInfo.xml").exists()
                saved = await MediaItem.get(id=extra.id)
                assert saved.index_state == IndexState.EMPTY

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem",
    [
        "body_link",
        "metadata_link",
        "directory_link",
        "replacement",
        "foreign_child",
        "unregistered_chapter",
    ],
)
def test_delete_reading_guard(tmp_path, monkeypatch, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            metadata = source.directory / "ComicInfo.xml"
            metadata.write_text("<ComicInfo/>")
            outside = tmp_path / "Outside"
            outside.mkdir()
            sentinel = outside / "keep"
            sentinel.write_bytes(b"Keep me")
            if problem == "body_link":
                (source.directory / "2.png").symlink_to(sentinel)
            elif problem == "metadata_link":
                metadata.unlink()
                metadata.symlink_to(sentinel)
            elif problem == "directory_link":
                source.directory.rename(outside / "Source")
                source.directory.symlink_to(
                    outside / "Source", target_is_directory=True
                )
            elif problem == "replacement":
                (source.directory / "New.cbz").write_bytes(b"new body")
            elif problem == "foreign_child":
                foreign = await MediaLib.create(
                    name="Foreign", dir=str(outside), lib_type=LibType.COMIC, priority=2
                )
                await MediaItem.create(
                    lib=foreign,
                    parent=item,
                    path=str(outside),
                    dir=str(outside),
                    name="Foreign",
                    format=MediaFormat.DIR,
                )
            else:
                chapter = source.directory / "New"
                chapter.mkdir()
                (chapter / "1.png").write_bytes(_PNG)
            before = await MediaItem.all().values()
            files = {
                path: path.read_bytes()
                for path in source.directory.rglob("*")
                if path.is_file()
            }

            def refuse(_path):
                """Reject any write before ownership checks have succeeded.

                Args:
                    _path: The unexpected deletion candidate.

                Raises:
                    AssertionError: For every attempted write.
                """
                raise AssertionError("unexpected deletion")

            monkeypatch.setattr(media_service, "delete_path", refuse)
            with pytest.raises(ContentError):
                await MediaItemService.delete(item.id, local=True)
            assert await MediaItem.all().values() == before
            assert all(path.read_bytes() == content for path, content in files.items())
            assert sentinel.read_bytes() == b"Keep me" and _cache(item).is_dir()

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["new_page", "body_restore", "directory", "database"]
)
def test_delete_reading_changed(tmp_path, monkeypatch, change):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            metadata = source.directory / "ComicInfo.xml"
            metadata.write_text("<ComicInfo/>")
            write = media_service.write_in_thread

            def remove(path):
                """Change the source immediately after deleting its original page.

                Args:
                    path: The selected file being removed.
                """
                path.unlink()
                if path.name != "1.png":
                    return
                if change == "new_page":
                    (source.directory / "2.png").write_bytes(_PNG)
                elif change == "body_restore":
                    path.write_bytes(_PNG)
                elif change == "directory":
                    source.directory.rename(source.directory.with_name("Previous"))
                    source.directory.mkdir()
                    metadata.write_text("New metadata")

            async def changed(function, *args, **kwargs):
                """Change database ownership after the filesystem writer returns.

                Args:
                    function: The completed filesystem worker.
                    *args: Its positional arguments.
                    **kwargs: Its keyword arguments.

                Returns:
                    The original filesystem result.
                """
                result = await write(function, *args, **kwargs)
                if function is media_service._delete_reading_files:
                    await MediaItem.filter(id=item.id).update(
                        path=str(source.path.with_name("Moved")),
                        dir=str(source.path.with_name("Moved")),
                    )
                return result

            monkeypatch.setattr(media_service, "delete_path", remove)
            if change == "database":
                monkeypatch.setattr(media_service, "write_in_thread", changed)
            with pytest.raises(ContentError):
                await MediaItemService.delete(item.id, local=True)
            assert await MediaItem.filter(id=item.id).exists()
            if change != "database":
                assert metadata.is_file() and _cache(item).is_dir()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "directory"),
    [(format, True) for format in MediaFormat]
    + [(format, False) for format in MediaFormat if format != MediaFormat.DIR],
)
def test_remove_missing_reading(tmp_path, format, directory):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            if directory:
                other = source.directory.with_name("Other")
                shutil.copytree(source.directory, other)
                await MediaItemService.ingest_reading_work(lib.id, other)
            retained = await MediaItem.exclude(id=item.id).values()
            await MediaItem.filter(id=item.id).update(visible=False)
            cache = _cache(item).parent
            (cache / "old-version").mkdir()
            (cache / "old-version" / "index.json").write_text("old")
            note = source.directory / "notes.md"
            note.write_text("Keep me")
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            for kind in HistoryType:
                await UserHistory.create(user=user, rel_id=item.id, rel_type=kind)
            kind = (
                HistoryType.TEXT if lib.lib_type == LibType.NOVEL else HistoryType.IMAGE
            )
            preserved = await UserHistory.exclude(rel_type=kind).values()
            event = await MediaEvent.create(
                lib=lib,
                src_path=str(source.directory),
                event_type="reconcile",
                is_directory=True,
                payload={"targets": [str(source.directory)]},
            )
            if directory:
                shutil.rmtree(source.directory)
            else:
                source.path.unlink()
            assert await MediaItemService.remove_missing_reading_item(item.id) == [
                item.id
            ]
            assert await MediaItem.all().values() == retained and not cache.exists()
            assert await UserHistory.all().values() == preserved
            assert await MediaEvent.filter(id=event.id).exists()
            assert not await MediaItemService.remove_missing_reading_item(item.id)
            if not directory:
                assert note.read_text() == "Keep me"

    asyncio.run(run())


@pytest.mark.parametrize("collection", [False, True])
@pytest.mark.parametrize("format", [MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP])
def test_remove_missing_comic(tmp_path, format, collection):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format, chapter=True)
            work = source.directory.parent
            sibling = work / "Sibling"
            sibling.mkdir()
            (sibling / "1.png").write_bytes(_PNG)
            await MediaItemService.ingest_reading_work(lib.id, work)
            parent = await MediaItem.get(path=str(work))
            child = await MediaItem.get(path=str(source.path))
            second = await MediaItem.get(path=str(sibling))
            if collection:
                other = work.with_name("Other")
                other.mkdir()
                (other / "1.png").write_bytes(_PNG)
                await MediaItemService.ingest_reading_work(lib.id, other)
            retained = await MediaItem.exclude(
                id__in=[parent.id, child.id, second.id]
            ).values()
            await MediaItem.filter(id=child.id).update(visible=False)
            preserved = await MediaItem.exclude(id=child.id).values()
            histories = []
            for index, chapter_id in enumerate(
                (child.id, second.id, None, str(child.id), [child.id], True)
            ):
                user = await User.create(
                    username=str(index), password="unused", role=UserRole.USER
                )
                history = await UserHistory.create(
                    user=user,
                    rel_id=parent.id,
                    rel_type=HistoryType.IMAGE,
                    locator={"chapter_item_id": chapter_id},
                )
                if chapter_id != child.id:
                    histories.append(history.id)
                await UserHistory.create(
                    user=user,
                    rel_id=child.id,
                    rel_type=HistoryType.VIDEO,
                )
            video = await UserHistory.filter(rel_type=HistoryType.VIDEO).values()
            shutil.rmtree(work if collection else source.directory)
            removed = await MediaItemService.remove_missing_reading_item(
                parent.id if collection else child.id
            )
            assert set(removed) == (
                {parent.id, child.id, second.id} if collection else {child.id}
            )
            assert not _cache(child).parent.exists()
            assert (
                await UserHistory.filter(rel_type=HistoryType.VIDEO).values() == video
            )
            if collection:
                assert (
                    await MediaItem.all().values() == retained
                    and not _cache(second).parent.exists()
                )
                assert not await UserHistory.filter(rel_type=HistoryType.IMAGE).exists()
            else:
                assert await MediaItem.all().values() == preserved
                assert set(
                    await UserHistory.filter(rel_type=HistoryType.IMAGE).values_list(
                        "id", flat=True
                    )
                ) == set(histories)
                assert _cache(second).is_dir()
                current = await MediaItemService.sync_collection(parent.id)
                assert (
                    current.extra is not None
                    and current.extra["content"]["chapter_count"] == 1
                )
                shutil.rmtree(sibling)
                await MediaItemService.remove_missing_reading_item(second.id)
                current = await MediaItemService.sync_collection(parent.id)
                assert current.index_state == IndexState.EMPTY
                assert (
                    current.extra is not None
                    and current.extra["content"]["chapter_count"] == 0
                )
                assert await MediaItem.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.DIR])
@pytest.mark.parametrize(
    "problem", ["empty", "unknown", "linked", "unreadable", "last_work"]
)
def test_remove_reading_library(tmp_path, monkeypatch, format, problem):
    """Retain records, histories and caches when registered works all disappear.

    Args:
        tmp_path: The isolated library and cache root.
        monkeypatch: The fixture checking enumeration and simulating access errors.
        format: The novel or comic source format.
        problem: The unavailable library state to simulate.
    """

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, format)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            if problem != "last_work":
                other = source.directory.with_name("Other")
                shutil.copytree(source.directory, other)
                await MediaItemService.ingest_reading_work(lib.id, other)
                await MediaItem.filter(dir=str(other)).update(visible=False)
            item = await MediaItem.get(path=str(source.path))
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user,
                rel_id=item.id,
                rel_type=HistoryType.TEXT
                if format == MediaFormat.TXT
                else HistoryType.IMAGE,
            )
            await MediaEvent.create(
                lib=lib,
                src_path=str(source.directory),
                event_type="reconcile",
                is_directory=True,
                payload=ReadingReconcile(
                    targets=[str(source.directory)], force_targets=[]
                ).model_dump(mode="json", exclude_none=True),
            )
            rows = await MediaItem.all().values()
            histories = await UserHistory.all().values()
            events = await MediaEvent.all().values()
            root, offline = Path(lib.dir), tmp_path / "Offline"
            root.rename(offline)
            root.mkdir()
            if problem == "unknown":
                (root / "Unknown").mkdir()
                (root / ".hidden").mkdir()
                (root / "copy.part").mkdir()
                (root / "notes.txt").write_text("Not a registered work")
            elif problem == "linked":
                (root / "Other").symlink_to(offline / "Other", target_is_directory=True)
            elif problem == "unreadable":
                (root / "Other").mkdir()
            main_thread = threading.get_ident()
            list_entries = reading_handler.list_source_entries

            def inspect(directory):
                """Check library access in a worker thread.

                Args:
                    directory: The library root being listed.

                Returns:
                    The visible files and directories.

                Raises:
                    PermissionError: For the simulated unreadable library.
                """
                assert threading.get_ident() != main_thread
                assert directory == root
                if problem == "unreadable":
                    raise PermissionError("denied")
                return list_entries(directory)

            with monkeypatch.context() as patch:
                patch.setattr(reading_handler, "list_source_entries", inspect)
                for _ in range(2):
                    with pytest.raises(ContentError, match="media_source_unavailable"):
                        await MediaItemService.remove_missing_reading_item(item.id)
            assert await MediaItem.all().values() == rows
            assert await UserHistory.all().values() == histories
            assert await MediaEvent.all().values() == events
            for current in await MediaItem.all():
                assert _cache(current).is_dir()
            shutil.rmtree(root)
            offline.rename(root)
            assert not await MediaItemService.remove_missing_reading_item(item.id)
            if problem != "last_work":
                shutil.rmtree(source.directory)
                assert await MediaItemService.remove_missing_reading_item(item.id) == [
                    item.id
                ]
                assert not _cache(item).parent.exists()
                assert not await UserHistory.exists()
                assert await MediaItem.all().count() == 1
                assert await MediaEvent.all().values() == events

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["enumeration", "cleanup"])
def test_remove_reading_library_change(tmp_path, monkeypatch, stage):
    """Recheck library disappearance before caches and database rows are removed.

    Args:
        tmp_path: The isolated library and cache root.
        monkeypatch: The fixture replacing the library during cleanup.
        stage: Whether the root changes during enumeration or after cache cleanup.
    """

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            other = source.directory.with_name("Other")
            shutil.copytree(source.directory, other)
            await MediaItemService.ingest_reading_work(lib.id, other)
            item = await MediaItem.get(path=str(source.path))
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user, rel_id=item.id, rel_type=HistoryType.TEXT
            )
            shutil.rmtree(source.directory)
            rows = await MediaItem.all().values()
            histories = await UserHistory.all().values()
            root, offline = Path(lib.dir), tmp_path / "Offline"
            list_entries = reading_handler.list_source_entries
            write = media_service.write_in_thread

            def replaced(directory):
                """Replace the root after reading its previous entries.

                Args:
                    directory: The library root being listed.

                Returns:
                    Entries from the original root.
                """
                entries = list_entries(directory)
                root.rename(offline)
                root.mkdir()
                return entries

            async def changed(function, *args, **kwargs):
                """Replace the root after removing rebuildable caches.

                Args:
                    function: The cache cleanup operation.
                    *args: Positional cleanup arguments.
                    **kwargs: Keyword cleanup arguments.
                """
                await write(function, *args, **kwargs)
                root.rename(offline)
                root.mkdir()

            if stage == "enumeration":
                monkeypatch.setattr(reading_handler, "list_source_entries", replaced)
            else:
                monkeypatch.setattr(media_service, "write_in_thread", changed)
            with pytest.raises(
                ContentError,
                match="content_changed"
                if stage == "enumeration"
                else "media_source_unavailable",
            ):
                await MediaItemService.remove_missing_reading_item(item.id)
            assert await MediaItem.all().values() == rows
            assert await UserHistory.all().values() == histories
            assert _cache(item).is_dir() == (stage == "enumeration")
            assert _cache(await MediaItem.get(dir=str(other))).is_dir()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["present", "restore", "ownership"])
def test_remove_directory_guard(tmp_path, monkeypatch, stage):
    """Keep ownership and histories if an observed missing directory is restored.

    Args:
        tmp_path: The isolated library and cache root.
        monkeypatch: The fixture restoring a directory during cache cleanup.
        stage: An existing container, a late restoration or a changed item directory.
    """

    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            other = source.directory.with_name("Other")
            shutil.copytree(source.directory, other)
            await MediaItemService.ingest_reading_work(lib.id, other)
            item = await MediaItem.get(path=str(source.path))
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user, rel_id=item.id, rel_type=HistoryType.TEXT
            )
            history = await UserHistory.all().values()
            xml = "<package><metadata/></package>"
            if stage == "present":
                source.path.unlink()
                (source.directory / "metadata.opf").write_text(xml)
            else:
                shutil.rmtree(source.directory)
            if stage == "ownership":
                directory = source.directory.with_name("Changed")
                await MediaItem.filter(id=item.id).update(
                    dir=str(directory), path=str(directory / source.path.name)
                )
            elif stage == "restore":
                write = media_service.write_in_thread

                async def restored(function, *args, **kwargs):
                    """Restore only the container and its XML after cache cleanup.

                    Args:
                        function: The cache cleanup operation.
                        args: Positional cleanup arguments.
                        kwargs: Keyword cleanup arguments.
                    """
                    await write(function, *args, **kwargs)
                    source.directory.mkdir()
                    (source.directory / "metadata.opf").write_text(xml)

                monkeypatch.setattr(media_service, "write_in_thread", restored)
            rows = await MediaItem.all().values()
            if stage == "present":
                assert not await MediaItemService.remove_missing_reading_item(
                    item.id, directory=source.directory
                )
            else:
                with pytest.raises(ContentError, match="content_changed"):
                    await MediaItemService.remove_missing_reading_item(
                        item.id, directory=source.directory
                    )
            assert await MediaItem.all().values() == rows
            assert await UserHistory.all().values() == history
            assert _cache(item).exists() == (stage != "restore")
            if stage != "ownership":
                assert (source.directory / "metadata.opf").read_text() == xml

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind", ["file", "replacement", "empty_directory", "empty_collection"]
)
def test_remove_reading_present(tmp_path, kind):
    async def run():
        async with _database():
            format = (
                MediaFormat.TXT if kind in ("file", "replacement") else MediaFormat.DIR
            )
            lib, source = await _source(
                tmp_path, format, chapter=kind == "empty_collection"
            )
            work = source.parent_path or source.directory
            await MediaItemService.ingest_reading_work(lib.id, work)
            item = await MediaItem.get(
                path=str(work if kind == "empty_collection" else source.path)
            )
            if kind == "replacement":
                source.path.unlink()
                source.path.write_text("Replacement")
            elif kind == "empty_collection":
                shutil.rmtree(source.directory)
            elif kind == "empty_directory":
                (source.path / "1.png").unlink()
            before = await MediaItem.all().values()
            assert not await MediaItemService.remove_missing_reading_item(item.id)
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem",
    [
        "root_missing",
        "root_link",
        "work_link",
        "source_link",
        "permission",
        "file_type",
    ],
)
def test_remove_reading_unavailable(tmp_path, monkeypatch, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            source.path.unlink()
            root = Path(lib.dir)
            if problem in ("root_missing", "root_link", "work_link"):
                target = source.directory if problem == "work_link" else root
                outside = tmp_path / "Outside"
                target.rename(outside)
                if problem != "root_missing":
                    target.symlink_to(outside, target_is_directory=True)
            elif problem == "source_link":
                source.path.symlink_to(tmp_path / "Absent.txt")
            elif problem == "file_type":
                source.path.mkdir()
            else:
                original = Path.stat

                def denied(path, *args, **kwargs):
                    """Reject source access without hiding the permission failure.

                    Args:
                        path: The path whose attributes are requested.
                        *args: Positional stat arguments.
                        **kwargs: Keyword stat arguments.

                    Returns:
                        Unmodified attributes for other paths.

                    Raises:
                        PermissionError: For the selected source.
                    """
                    if path == source.path:
                        raise PermissionError("denied")
                    return original(path, *args, **kwargs)

                monkeypatch.setattr(Path, "stat", denied)
            before = await MediaItem.all().values()
            with pytest.raises(ContentError, match="media_source_unavailable"):
                await MediaItemService.remove_missing_reading_item(item.id)
            assert await MediaItem.all().values() == before and _cache(item).is_dir()

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem", ["foreign_child", "outside_child", "nested_child", "parent_format"]
)
def test_remove_reading_ownership(tmp_path, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.DIR, chapter=True)
            work = source.directory.parent
            await MediaItemService.ingest_reading_work(lib.id, work)
            parent = await MediaItem.get(path=str(work))
            child = await MediaItem.get(path=str(source.path))
            if problem == "foreign_child":
                other = await MediaLib.create(
                    name="Other",
                    dir=str(tmp_path / "Other"),
                    lib_type=LibType.COMIC,
                    priority=2,
                )
                await MediaItem.filter(id=child.id).update(lib_id=other.id)
            elif problem == "outside_child":
                await MediaItem.filter(id=child.id).update(
                    path=str(work.with_name("Other") / "Chapter")
                )
            elif problem == "nested_child":
                await MediaItem.create(
                    lib=lib,
                    parent=child,
                    path=str(source.path / "Nested"),
                    dir=str(source.path / "Nested"),
                    name="Nested",
                    format=MediaFormat.DIR,
                )
            else:
                await MediaItem.filter(id=parent.id).update(format=MediaFormat.DIR)
            shutil.rmtree(work)
            before = await MediaItem.all().values()
            with pytest.raises(
                ContentError,
                match="media_source_unavailable"
                if problem == "foreign_child"
                else "unsupported_layout",
            ):
                await MediaItemService.remove_missing_reading_item(parent.id)
            assert await MediaItem.all().values() == before and _cache(child).is_dir()

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem", ["cache", "restore", "ancestor", "ownership", "new_child", "write"]
)
def test_remove_reading_failure(tmp_path, monkeypatch, problem):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            user = await User.create(
                username="reader", password="unused", role=UserRole.USER
            )
            await UserHistory.create(
                user=user, rel_id=item.id, rel_type=HistoryType.TEXT
            )
            source.path.unlink()
            rows, history = (
                await MediaItem.all().values(),
                await UserHistory.all().values(),
            )
            original = media_service.write_in_thread

            async def changed(function, *args, **kwargs):
                """Change state after cleanup to verify deletion revalidation.

                Args:
                    function: The cache cleanup operation.
                    *args: Positional cleanup arguments.
                    **kwargs: Keyword cleanup arguments.

                Returns:
                    The completed filesystem worker result.
                """
                result = await original(function, *args, **kwargs)
                if function is not media_service._remove_reading_caches:
                    return result
                if problem == "restore":
                    source.path.write_text("Restored")
                elif problem == "ancestor":
                    source.directory.rename(source.directory.with_name("Previous"))
                    source.directory.mkdir()
                elif problem == "ownership":
                    await MediaItem.filter(id=item.id).update(
                        path=str(source.path.with_name("Moved.txt"))
                    )
                elif problem == "new_child":
                    await MediaItem.create(
                        lib=lib,
                        parent=item,
                        path=str(source.directory / "New.txt"),
                        dir=str(source.directory),
                        name="New",
                        format=MediaFormat.TXT,
                    )
                return result

            def failed_cache(_path):
                """Simulate a cache permission error.

                Args:
                    _path: The cache directory requested for removal.

                Raises:
                    PermissionError: To retain database owners for retry.
                """
                raise PermissionError("denied")

            delete = QuerySet.delete

            def failed_write(query):
                """Fail item deletion after history deletion in the same transaction.

                Args:
                    query: The queryset selecting histories or media items.

                Returns:
                    The original deletion query for histories.

                Raises:
                    RuntimeError: When deleting media items.
                """
                if query.model == MediaItem:
                    raise RuntimeError("write failed")
                return delete(query)

            if problem == "cache":
                monkeypatch.setattr(media_service.shutil, "rmtree", failed_cache)
            elif problem == "write":
                monkeypatch.setattr(QuerySet, "delete", failed_write)
            else:
                monkeypatch.setattr(media_service, "write_in_thread", changed)
            with pytest.raises(RuntimeError if problem == "write" else ContentError):
                await MediaItemService.remove_missing_reading_item(item.id)
            if problem == "new_child":
                assert await MediaItem.all().count() == 2
            elif problem != "ownership":
                assert await MediaItem.all().values() == rows
            else:
                assert (await MediaItem.get(id=item.id)).path.endswith("Moved.txt")
            assert await UserHistory.all().values() == history
            assert _cache(item).exists() == (problem == "cache")

    asyncio.run(run())


@pytest.mark.parametrize("base", [False, True])
def test_remove_reading_cache_link(tmp_path, base):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            source.path.unlink()
            directory = _cache(item).parent.parent if base else _cache(item).parent
            outside = tmp_path / "Outside"
            directory.rename(outside)
            directory.symlink_to(outside, target_is_directory=True)
            before = await MediaItem.all().values()
            with pytest.raises(ContentError, match="content_not_ready"):
                await MediaItemService.remove_missing_reading_item(item.id)
            assert await MediaItem.all().values() == before
            assert _cache(item).is_dir() and outside.is_dir()

    asyncio.run(run())


def test_remove_reading_cancel(tmp_path, monkeypatch):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            item = await MediaItem.get(path=str(source.path))
            source.path.unlink()
            original = media_service._remove_reading_caches
            started, finish = threading.Event(), threading.Event()
            loop_thread = threading.get_ident()

            def blocked(ids):
                """Keep cache cleanup active while verifying cancellation and locks.

                Args:
                    ids: The item IDs selected for cache cleanup.
                """
                assert threading.get_ident() != loop_thread
                started.set()
                assert finish.wait(timeout=5)
                original(ids)

            monkeypatch.setattr(media_service, "_remove_reading_caches", blocked)
            task = asyncio.create_task(
                MediaItemService.remove_missing_reading_item(item.id)
            )
            try:
                assert await asyncio.to_thread(started.wait, 5)
                task.cancel()
                with pytest.raises(Timeout):
                    async with await library_lock(lib.dir).acquire(timeout=0):
                        pass
                assert not task.done()
            finally:
                finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert await MediaItem.filter(id=item.id).exists()
            assert not _cache(item).parent.exists()
            assert await MediaItemService.remove_missing_reading_item(item.id) == [
                item.id
            ]

    asyncio.run(run())


def test_create_concurrent(tmp_path):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            first, second = await asyncio.gather(
                MediaItemService.create_reading(lib.id, source),
                MediaItemService.create_reading(lib.id, source),
            )
            assert first.id == second.id
            assert await MediaItem.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_create_worker(tmp_path, monkeypatch, cancel):
    async def run():
        async with _database():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            started, finish, stopped = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )
            operation = media_service._validate_reading_source
            loop_thread = threading.get_ident()

            def blocked(*args, **kwargs):
                """Keep validation active while inspecting locks and cancellation.

                Args:
                    *args: The proposed item passed to source validation.
                    **kwargs: The candidate requirement passed to validation.
                """
                try:
                    assert threading.get_ident() != loop_thread
                    operation(*args, **kwargs)
                    started.set()
                    assert finish.wait(timeout=5)
                finally:
                    stopped.set()

            monkeypatch.setattr(media_service, "_validate_reading_source", blocked)
            task = asyncio.create_task(MediaItemService.create_reading(lib.id, source))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                assert await MediaItem.all().count() == 0
                with pytest.raises(Timeout):
                    async with await library_lock(lib.dir).acquire(timeout=0):
                        pass
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    async with await library_lock(lib.dir).acquire(timeout=0):
                        pass
            finally:
                finish.set()
                assert await asyncio.to_thread(stopped.wait, 5)
            if cancel:
                assert await MediaItem.all().count() == 0
            else:
                assert (await task).path == str(source.path)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat]
    + [
        (format, True) for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
    ],
)
def test_publish_content(tmp_path, format, chapter):
    async def run():
        async with _database():
            item = await _item(tmp_path, format, chapter=chapter)
            await MediaItem.filter(id=item.id).update(visible=False)
            source = Path(item.path)
            before = {
                path: path.read_bytes()
                for path in Path(item.lib.dir).rglob("*")
                if path.is_file()
            }
            current = await MediaItemService.index_content(item.id)
            assert current.id == item.id and not current.visible
            assert current.index_state == IndexState.READY
            assert current.index_error is None and current.index_version is not None
            assert len(current.index_version) == 64
            assert current.title == "Saved title" and current.year == 2000
            assert current.nfo_path is current.hash is None
            assert current.parent_id == item.parent_id
            novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
            assert current.extra == {
                "metadata_sync": {"state": "ready"},
                "schema_version": 1,
                "content": {
                    "chapter_count": 1 if novel else None,
                    "page_count": None if novel else 1,
                },
            }
            assert current.size == (
                len(_PNG) if format == MediaFormat.DIR else source.stat().st_size
            )
            cache = _cache(current)
            if novel:
                index = load_text_index(cache)
                body = read_text_chapter(cache, index.chapters[0].id)
                assert (
                    body == ["Body"]
                    if format == MediaFormat.TXT
                    else isinstance(body, EpubContent)
                )
            else:
                index = load_image_index(cache)
                assert read_image_resource(source, cache, index.pages[0].id) == (
                    _PNG,
                    "image/png",
                )
            assert index.index_version == current.index_version
            assert list(cache.parent.iterdir()) == [cache]
            assert all(path.read_bytes() == data for path, data in before.items())
            if chapter:
                parent = await MediaItem.get(id=item.parent_id)
                assert parent.index_state is parent.index_version is None

    asyncio.run(run())


def test_rebuild_content(tmp_path):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            first = await MediaItemService.index_content(item.id)
            path = Path(item.path)
            info = path.stat()
            path.write_text("Next")
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            reused = await MediaItemService.index_content(item.id, force=False)
            assert reused.index_version == first.index_version
            current = await MediaItemService.index_content(item.id)
            assert current.index_version != first.index_version
            for row, expected in ((first, "Body"), (current, "Next")):
                cache = _cache(row)
                index = load_text_index(cache)
                assert read_text_chapter(cache, index.chapters[0].id) == [expected]
            assert len(list(_cache(current).parent.iterdir())) == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "chapter"),
    [(format, False) for format in MediaFormat]
    + [
        (format, True) for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
    ],
)
def test_reuse_content(tmp_path, monkeypatch, format, chapter):
    async def run():
        async with _database():
            item = await _item(tmp_path, format, chapter=chapter)
            await MediaItem.filter(id=item.id).update(visible=False)
            first = await MediaItemService.index_content(item.id)
            before = await MediaItem.all().values()
            cached = {path.name: path.read_bytes() for path in _cache(first).iterdir()}
            monkeypatch.setattr(
                media_service,
                "_build_index",
                lambda *_args: pytest.fail("unchanged bodies must not be parsed"),
            )
            metadata = Path(item.dir) / (
                "metadata.opf"
                if item.lib.lib_type == LibType.NOVEL
                else "ComicInfo.xml"
            )
            cover = Path(item.dir) / "cover.png"
            for change in ("none", "create", "modify", "delete"):
                if change in ("create", "modify"):
                    metadata.write_text(f"invalid metadata: {change}")
                    cover.write_bytes(_PNG + change.encode())
                elif change == "delete":
                    metadata.unlink()
                    cover.unlink()
                current = await MediaItemService.index_content(item.id, force=False)
                assert current.index_version == first.index_version
                assert current.index_state == IndexState.READY and not current.visible
                assert await MediaItem.all().values() == before
                assert list(_cache(first).parent.iterdir()) == [_cache(first)]
                assert {
                    path.name: path.read_bytes() for path in _cache(first).iterdir()
                } == cached

    asyncio.run(run())


@pytest.mark.parametrize("format", list(MediaFormat))
@pytest.mark.parametrize("change", ["size", "mtime"])
def test_reuse_changed_source(tmp_path, format, change):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            first = await MediaItemService.index_content(item.id)
            path = Path(item.path)
            if format == MediaFormat.DIR:
                path /= "1.png"
            before = path.stat()
            if change == "mtime":
                os.utime(
                    path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000)
                )
            elif format in (MediaFormat.CBZ, MediaFormat.ZIP, MediaFormat.EPUB):
                with zipfile.ZipFile(path, "a") as archive:
                    archive.writestr("2.png", _PNG)
            else:
                path.write_bytes(
                    b"Updated body" if format == MediaFormat.TXT else _PNG * 2
                )
            if change == "size":
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            current = await MediaItemService.index_content(item.id, force=False)
            assert current.index_version != first.index_version
            assert (
                current.index_state == IndexState.READY and current.index_error is None
            )
            assert len(list(_cache(first).parent.iterdir())) == 2
            assert current.extra is not None
            if change == "size" and format in (MediaFormat.CBZ, MediaFormat.ZIP):
                assert current.extra["content"]["page_count"] == 2
            reused = await MediaItemService.index_content(item.id, force=False)
            assert reused.index_version == current.index_version

    asyncio.run(run())


@pytest.mark.parametrize("change", ["add", "remove", "rename"])
def test_reuse_changed_pages(tmp_path, change):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.DIR)
            second = Path(item.path) / "2.png"
            second.write_bytes(_PNG)
            first = await MediaItemService.index_content(item.id)
            if change == "add":
                (Path(item.path) / "10.png").write_bytes(_PNG)
            elif change == "remove":
                second.unlink()
            else:
                second.rename(second.with_name("10.png"))
            current = await MediaItemService.index_content(item.id, force=False)
            assert current.index_version != first.index_version
            index = load_image_index(_cache(current))
            assert [page.relative_path for page in index.pages] == (
                ["1.png", "2.png", "10.png"]
                if change == "add"
                else ["1.png"]
                if change == "remove"
                else ["1.png", "10.png"]
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "problem"),
    [
        (format, problem)
        for format in MediaFormat
        for problem in ("missing", "json", "version")
    ]
    + [
        (format, problem)
        for format in (MediaFormat.TXT, MediaFormat.EPUB)
        for problem in ("body_missing", "body_truncated")
    ]
    + [(format, "format") for format in (MediaFormat.CBZ, MediaFormat.ZIP)]
    + [(MediaFormat.TXT, "pointer")],
)
def test_reuse_invalid_cache(tmp_path, format, problem):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            first = await MediaItemService.index_content(item.id)
            cache = _cache(first)
            if problem == "missing":
                shutil.rmtree(cache)
            elif problem == "json":
                (cache / "index.json").write_text("{")
            elif problem.startswith("body_"):
                body = cache / (
                    "content.txt" if format == MediaFormat.TXT else "content.jsonl"
                )
                if problem == "body_missing":
                    body.unlink()
                else:
                    body.write_bytes(b"")
            elif problem == "pointer":
                await MediaItem.filter(id=item.id).update(index_version="../outside")
            else:
                index = json.loads((cache / "index.json").read_text())
                index[problem if problem == "format" else "index_version"] = (
                    ("zip" if format == MediaFormat.CBZ else "cbz")
                    if problem == "format"
                    else "0" * 64
                )
                (cache / "index.json").write_text(json.dumps(index))
            current = await MediaItemService.index_content(item.id, force=False)
            assert current.index_version != first.index_version
            assert current.index_state == IndexState.READY
            assert current.extra == first.extra and current.size == first.size
            reused = await MediaItemService.index_content(item.id, force=False)
            assert reused.index_version == current.index_version

    asyncio.run(run())


@pytest.mark.parametrize(
    "state", [IndexState.PENDING, IndexState.ERROR, IndexState.EMPTY]
)
def test_reuse_unready(tmp_path, state):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            first = await MediaItemService.index_content(item.id)
            await MediaItem.filter(id=item.id).update(
                index_state=state, index_error="old_error"
            )
            current = await MediaItemService.index_content(item.id, force=False)
            assert current.index_version != first.index_version
            assert (
                current.index_state == IndexState.READY and current.index_error is None
            )

    asyncio.run(run())


def test_reuse_unstable(tmp_path, monkeypatch):
    from app.core.media import text

    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            first = await MediaItemService.index_content(item.id)
            load = text.load_text_index
            loop_thread = threading.get_ident()

            def changed(cache):
                """Change the source after loading its index in a worker.

                Args:
                    cache: The published index directory being checked for reuse.

                Returns:
                    The previously loaded index.
                """
                assert threading.get_ident() != loop_thread
                index = load(cache)
                path = Path(item.path)
                before = path.stat()
                path.write_text("Next")
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                return index

            with monkeypatch.context() as patcher:
                patcher.setattr(text, "load_text_index", changed)
                current = await MediaItemService.index_content(item.id, force=False)
            assert current.index_version != first.index_version
            index = load(_cache(current))
            assert read_text_chapter(_cache(current), index.chapters[0].id) == ["Next"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "problem", "state", "code"),
    [
        (MediaFormat.TXT, "empty", IndexState.EMPTY, "empty_content"),
        (MediaFormat.DIR, "empty", IndexState.EMPTY, "empty_content"),
        (MediaFormat.DIR, "invalid", IndexState.ERROR, "invalid_image"),
        (MediaFormat.TXT, "invalid", IndexState.ERROR, "text_decode_failed"),
        (MediaFormat.CBZ, "invalid", IndexState.ERROR, "invalid_archive"),
        (MediaFormat.EPUB, "invalid", IndexState.ERROR, "invalid_archive"),
        (MediaFormat.TXT, "missing", IndexState.PENDING, "media_source_unavailable"),
    ],
)
@pytest.mark.parametrize("force", [False, True])
def test_failed_rebuild(tmp_path, format, problem, state, code, force):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            first = await MediaItemService.index_content(item.id)
            path = Path(item.path)
            if format == MediaFormat.DIR:
                path /= "1.png"
            if problem == "missing" or (
                problem == "empty" and format == MediaFormat.DIR
            ):
                path.unlink()
            else:
                path.write_bytes(b"" if problem == "empty" else b"\x00invalid")
            with pytest.raises(ContentError, match=code):
                await MediaItemService.index_content(item.id, force=force)
            current = await MediaItem.get(id=item.id)
            assert current.index_state == state and current.index_error == code
            assert current.index_version == first.index_version
            assert current.size == first.size and current.extra == first.extra
            assert list(_cache(current).parent.iterdir()) == [_cache(first)]

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["build", "publish", "database"])
def test_publish_failure(tmp_path, monkeypatch, phase):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            first = await MediaItemService.index_content(item.id)
            save = MediaItem.save

            async def fail_save(row, *args, **kwargs):
                """Fail only the database pointer update after cache publication.

                Args:
                    row: The item being saved.
                    *args: The positional save arguments.
                    **kwargs: The save options, including selected fields.

                Returns:
                    The normal save result for pending state updates.

                Raises:
                    RuntimeError: If the published version is being saved.
                """
                if "index_version" in kwargs.get("update_fields", []):
                    raise RuntimeError("database unavailable")
                return await save(row, *args, **kwargs)

            def fail_write(*args, **kwargs):
                """Simulate an inaccessible cache destination.

                Args:
                    *args: The unused writer arguments.
                    **kwargs: The unused writer options.

                Raises:
                    OSError: For every attempted write.
                """
                raise OSError("cache disk unavailable")

            with monkeypatch.context() as patcher:
                if phase == "build":
                    from app.core.media import text

                    patcher.setattr(text, "build_text_index", fail_write)
                elif phase == "publish":
                    patcher.setattr(media_service, "rename_exclusive", fail_write)
                else:
                    patcher.setattr(MediaItem, "save", fail_save)
                with pytest.raises(
                    RuntimeError if phase == "database" else ContentError
                ):
                    await MediaItemService.index_content(item.id)
            current = await MediaItem.get(id=item.id)
            assert current.index_state == IndexState.PENDING
            assert current.index_version == first.index_version
            assert current.extra == first.extra
            versions = list(_cache(first).parent.iterdir())
            assert all(path.suffix != ".tmp" for path in versions)
            assert len(versions) == (2 if phase == "database" else 1)
            assert _cache(first).is_dir()
            recovered = await MediaItemService.index_content(item.id)
            assert recovered.index_state == IndexState.READY
            assert recovered.index_version != first.index_version

    asyncio.run(run())


@pytest.mark.parametrize("change", ["edit", "move", "delete", "page"])
def test_changed_build(tmp_path, monkeypatch, change):
    async def run():
        async with _database():
            item = await _item(
                tmp_path, MediaFormat.DIR if change == "page" else MediaFormat.TXT
            )
            first = await MediaItemService.index_content(item.id)
            worker = media_service.write_in_thread

            async def write(function, *args, **kwargs):
                """Apply a concurrent change after the source has been parsed.

                Args:
                    function: The filesystem operation being dispatched.
                    *args: The positional operation arguments.
                    **kwargs: The operation options.

                Returns:
                    The original operation result.
                """
                result = await worker(function, *args, **kwargs)
                if function is media_service._build_index:
                    if change in ("edit", "page"):
                        path = Path(item.path)
                        if change == "page":
                            path /= "1.png"
                        before = path.stat()
                        path.write_bytes(
                            b"Next" if change == "edit" else _PNG[:-1] + b"!"
                        )
                        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                    elif change == "move":
                        path = Path(item.path)
                        moved = path.with_name("Moved.txt")
                        path.rename(moved)
                        await MediaItem.filter(id=item.id).update(path=str(moved))
                    elif change == "delete":
                        await MediaItem.filter(id=item.id).delete()
                return result

            monkeypatch.setattr(media_service, "write_in_thread", write)
            with pytest.raises(ContentError, match="content_changed"):
                await MediaItemService.index_content(item.id)
            assert _cache(first).is_dir()
            assert not list(_cache(first).parent.glob("building_*.tmp"))
            current = await MediaItem.get_or_none(id=item.id)
            if change == "delete":
                assert current is None
            else:
                assert current is not None
                assert current.index_state == IndexState.PENDING
                assert current.index_version == first.index_version
                assert current.extra == first.extra

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["build", "publish"])
def test_cancel_publication(tmp_path, monkeypatch, phase):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            first = await MediaItemService.index_content(item.id)
            started, finish = threading.Event(), threading.Event()
            name = "_build_index" if phase == "build" else "_publish_index"
            operation = getattr(media_service, name)
            loop_thread = threading.get_ident()

            def blocked(*args):
                """Keep the worker active until cancellation can be inspected.

                Args:
                    *args: The original filesystem operation arguments.

                Returns:
                    The completed filesystem operation result.
                """
                assert threading.get_ident() != loop_thread
                result = operation(*args)
                started.set()
                assert finish.wait(timeout=5)
                return result

            monkeypatch.setattr(media_service, name, blocked)
            task = asyncio.create_task(MediaItemService.index_content(item.id))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                pending = await MediaItem.get(id=item.id)
                assert pending.index_state == IndexState.PENDING
                assert pending.extra == first.extra
                if phase == "build":
                    async with await library_lock(item.lib.dir).acquire(timeout=0):
                        pass
                    assert list(_cache(first).parent.glob("building_*.tmp"))
                else:
                    with pytest.raises(Timeout):
                        async with await library_lock(item.lib.dir).acquire(timeout=0):
                            pass
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            finally:
                finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            current = await MediaItem.get(id=item.id)
            assert current.index_state == IndexState.PENDING
            assert current.index_version == first.index_version
            assert current.extra == first.extra
            assert _cache(first).is_dir()
            assert not list(_cache(first).parent.glob("building_*.tmp"))
            async with await library_lock(item.lib.dir).acquire(timeout=0):
                pass

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["video", "collection", "missing"])
def test_unindexable_item(tmp_path, kind):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            if kind == "missing":
                await item.delete()
            elif kind == "video":
                await MediaLib.filter(id=item.lib_id).update(lib_type=LibType.MOVIE)
            else:
                await MediaLib.filter(id=item.lib_id).update(lib_type=LibType.COMIC)
                await MediaItem.filter(id=item.id).update(format=None)
            before = await MediaItem.all().values()
            with pytest.raises(DoesNotExist if kind == "missing" else ContentError):
                await MediaItemService.index_content(item.id)
            assert await MediaItem.all().values() == before
            root = Path(KaloscopeConfig.get_workspace("temp")) / "media_index"
            assert not root.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], IndexState.EMPTY),
        ([IndexState.EMPTY, IndexState.EMPTY], IndexState.EMPTY),
        ([IndexState.PENDING], IndexState.PENDING),
        ([IndexState.READY], IndexState.READY),
        ([IndexState.ERROR], IndexState.ERROR),
        ([None], IndexState.PENDING),
        ([IndexState.EMPTY, None], IndexState.PENDING),
        ([IndexState.EMPTY, IndexState.PENDING], IndexState.PENDING),
        ([IndexState.EMPTY, IndexState.ERROR], IndexState.ERROR),
        ([IndexState.PENDING, IndexState.ERROR], IndexState.ERROR),
        ([None, IndexState.ERROR], IndexState.ERROR),
        ([IndexState.READY, IndexState.EMPTY], IndexState.READY),
        ([IndexState.READY, IndexState.PENDING], IndexState.READY),
        ([IndexState.READY, IndexState.ERROR], IndexState.READY),
        ([None, IndexState.READY], IndexState.READY),
        (list(IndexState), IndexState.READY),
    ],
)
def test_collection_state(tmp_path, states, expected):
    async def run():
        async with _database():
            child = await _item(tmp_path, MediaFormat.DIR, chapter=True)
            collection = await MediaItem.get(id=child.parent_id)
            await child.delete()
            await MediaItem.filter(id=collection.id).update(
                title="Saved title",
                visible=False,
                index_state=IndexState.ERROR,
                index_error="old_error",
                extra={
                    "metadata_sync": {"state": "ready"},
                    "content": {"chapter_count": 99, "page_count": 99},
                },
            )
            for number, state in enumerate(states):
                path = str(Path(collection.path) / str(number))
                await MediaItem.create(
                    lib_id=collection.lib_id,
                    parent=collection,
                    path=path,
                    dir=path,
                    name=str(number),
                    format=MediaFormat.DIR,
                    index_state=state,
                    index_error="invalid_archive"
                    if state == IndexState.ERROR
                    else None,
                    index_version="a" * 64,
                    extra={"content": {"chapter_count": None, "page_count": 10}},
                )
            before = await MediaItem.filter(parent_id=collection.id).values()
            current = await MediaItemService.sync_collection(collection.id)
            assert current.index_state == expected
            assert current.index_error == (
                "invalid_archive"
                if expected == IndexState.ERROR
                else "empty_content"
                if expected == IndexState.EMPTY
                else None
            )
            assert not current.visible and current.title == "Saved title"
            assert current.index_version is current.size is None
            assert current.extra == {
                "schema_version": 1,
                "metadata_sync": {"state": "ready"},
                "content": {"chapter_count": len(states), "page_count": None},
            }
            assert await MediaItem.filter(parent_id=collection.id).values() == before
            saved = await MediaItem.filter(id=current.id).values()
            await MediaItemService.sync_collection(collection.id)
            assert await MediaItem.filter(id=current.id).values() == saved
            assert not (
                Path(KaloscopeConfig.get_workspace("temp")) / "media_index"
            ).exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind", ["hidden", "foreign", "unrelated", "text", "collection"]
)
def test_collection_children(tmp_path, kind):
    async def run():
        async with _database():
            child = await _item(tmp_path, MediaFormat.DIR, chapter=True)
            collection = await MediaItem.get(id=child.parent_id)
            await MediaItem.filter(id=child.id).update(
                index_state=IndexState.ERROR, index_error="invalid_image"
            )
            lib_id = child.lib_id
            if kind == "foreign":
                other = await MediaLib.create(
                    name="Other",
                    dir=str(tmp_path / "Other"),
                    priority=2,
                    lib_type=LibType.COMIC,
                )
                lib_id = other.id
            await MediaItem.create(
                lib_id=lib_id,
                parent_id=None if kind == "unrelated" else collection.id,
                path=str(Path(collection.path) / "Other"),
                dir=str(Path(collection.path) / "Other"),
                name="Other",
                format=MediaFormat.TXT
                if kind == "text"
                else None
                if kind == "collection"
                else MediaFormat.CBZ,
                visible=kind != "hidden",
                index_state=IndexState.READY,
                index_version="a" * 64,
            )
            before = await MediaItem.exclude(id=collection.id).values()
            current = await MediaItemService.sync_collection(collection.id)
            assert current.index_state == IndexState.ERROR
            assert current.index_error == "invalid_image"
            assert current.extra is not None
            assert current.extra["content"]["chapter_count"] == 1
            assert await MediaItem.exclude(id=collection.id).values() == before

    asyncio.run(run())


def test_collection_recovery(tmp_path):
    async def run():
        async with _database():
            child = await _item(tmp_path, MediaFormat.DIR, chapter=True)
            assert child.parent_id is not None
            page = Path(child.path) / "1.png"
            await MediaItemService.index_content(child.id)
            current = await MediaItemService.sync_collection(child.parent_id)
            assert current.index_state == IndexState.READY
            for visible in (False, True):
                await MediaItem.filter(id=child.id).update(visible=visible)
                current = await MediaItemService.sync_collection(child.parent_id)
                assert current.index_state == (
                    IndexState.READY if visible else IndexState.EMPTY
                )
                assert current.extra is not None
                assert current.extra["content"]["chapter_count"] == int(visible)
            page.unlink()
            with pytest.raises(ContentError, match="empty_content"):
                await MediaItemService.index_content(child.id)
            current = await MediaItemService.sync_collection(child.parent_id)
            assert current.index_state == IndexState.EMPTY
            assert current.index_error == "empty_content"
            page.write_bytes(b"broken image")
            with pytest.raises(ContentError):
                await MediaItemService.index_content(child.id)
            current = await MediaItemService.sync_collection(child.parent_id)
            failed = await MediaItem.get(id=child.id)
            assert current.index_state == IndexState.ERROR
            assert current.index_error == failed.index_error
            page.write_bytes(_PNG)
            await MediaItemService.index_content(child.id)
            current = await MediaItemService.sync_collection(child.parent_id)
            assert (
                current.index_state == IndexState.READY and current.index_error is None
            )
            await child.delete()
            current = await MediaItemService.sync_collection(child.parent_id)
            assert current.index_state == IndexState.EMPTY
            assert current.extra is not None
            assert current.extra["content"] == {"chapter_count": 0, "page_count": None}
            assert current.index_version is current.size is None

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["video", "novel", "body", "nested", "missing"])
def test_invalid_collection(tmp_path, kind):
    async def run():
        async with _database():
            child = await _item(tmp_path, MediaFormat.DIR, chapter=True)
            assert child.parent_id is not None
            if kind in ("video", "novel"):
                await MediaLib.filter(id=child.lib_id).update(
                    lib_type=LibType.MOVIE if kind == "video" else LibType.NOVEL
                )
            elif kind == "body":
                await MediaItem.filter(id=child.parent_id).update(
                    format=MediaFormat.DIR
                )
            elif kind == "nested":
                await MediaItem.filter(id=child.parent_id).update(parent_id=child.id)
            else:
                await MediaItem.filter(id=child.parent_id).delete()
            before = await MediaItem.all().values()
            with pytest.raises(DoesNotExist if kind == "missing" else ContentError):
                await MediaItemService.sync_collection(child.parent_id)
            assert await MediaItem.all().values() == before

    asyncio.run(run())


def test_collection_errors(tmp_path):
    async def run():
        async with _database():
            child = await _item(tmp_path, MediaFormat.DIR, chapter=True)
            assert child.parent_id is not None
            await MediaItem.filter(id=child.id).update(
                index_state=IndexState.ERROR, index_error="invalid_image"
            )
            path = str(Path(child.dir).with_name("Another"))
            other = await MediaItem.create(
                lib_id=child.lib_id,
                parent_id=child.parent_id,
                path=path,
                dir=path,
                name="Another",
                format=MediaFormat.DIR,
                index_state=IndexState.ERROR,
                index_error="invalid_archive",
            )
            current = await MediaItemService.sync_collection(child.parent_id)
            assert current.index_state == IndexState.ERROR
            assert current.index_error == "invalid_archive"
            await MediaItem.filter(id=other.id).update(index_error=None)
            current = await MediaItemService.sync_collection(child.parent_id)
            assert current.index_error == "invalid_image"

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["chapter", "directory", "library", "remove", "cancel"]
)
def test_collection_lock(tmp_path, monkeypatch, change):
    async def run():
        async with _database():
            child = await _item(tmp_path, MediaFormat.DIR, chapter=True)
            assert child.parent_id is not None
            lock = media_service.library_lock
            started = asyncio.Event()

            @asynccontextmanager
            async def observed(directory: str) -> AsyncGenerator[None]:
                """Signal that the initial lookup has completed before acquiring.

                Args:
                    directory: The library directory identifying the shared lock.

                Yields:
                    Control while the real library lock is held.
                """
                started.set()
                async with lock(directory):
                    yield

            monkeypatch.setattr(media_service, "library_lock", observed)
            before = await MediaItem.filter(id=child.parent_id).values()
            async with lock(child.lib.dir):
                task = asyncio.create_task(
                    MediaItemService.sync_collection(child.parent_id)
                )
                try:
                    await asyncio.wait_for(started.wait(), timeout=5)
                    assert not task.done()
                    assert await MediaItem.filter(id=child.parent_id).values() == before
                    if change == "chapter":
                        await MediaItem.filter(id=child.id).update(
                            index_state=IndexState.ERROR, index_error="invalid_image"
                        )
                    elif change == "directory":
                        await MediaLib.filter(id=child.lib_id).update(
                            dir=str(tmp_path / "Moved")
                        )
                    elif change == "library":
                        other = await MediaLib.create(
                            name="Other",
                            dir=str(tmp_path / "Other"),
                            priority=2,
                            lib_type=LibType.COMIC,
                        )
                        await MediaItem.filter(id=child.parent_id).update(lib=other)
                    elif change == "remove":
                        await MediaItem.filter(id=child.parent_id).delete()
                    else:
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
                except BaseException:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    raise
            if change == "chapter":
                current = await task
                assert current.index_state == IndexState.ERROR
                assert current.index_error == "invalid_image"
            else:
                before = await MediaItem.all().values()
                if change == "remove":
                    with pytest.raises(DoesNotExist):
                        await task
                elif change != "cancel":
                    with pytest.raises(ContentError, match="content_changed"):
                        await task
                assert await MediaItem.all().values() == before

    asyncio.run(run())
