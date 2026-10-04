"""Tests for reading ingestion, index publication and collection summaries."""

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
from unittest.mock import AsyncMock

import pytest
from filelock import Timeout
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist

from app.core.config import KaloscopeConfig
from app.core.media import events as media_events
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.epub.cache import EpubContent
from app.core.media.events import ReadingReconcile, coalesce_reading_events
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
                "_build_content",
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
                assert await MediaItemService.ingest_reading_work(
                    lib.id,
                    work,
                    targets={Path(path) for path in payload.targets},
                    force_targets={Path(path) for path in payload.force_targets},
                ) == {source.path: "empty_content"}
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
            assert await MediaItemService.ingest_reading_work(
                lib.id, work, targets={source.directory}
            ) == {source.path: "empty_content"}
            assert await MediaItem.filter(id=sibling.id).values() == before
            parent = await MediaItem.get(path=str(work))
            assert parent.index_state == IndexState.READY
            assert not await MediaItem.filter(path=str(empty)).exists()
            assert await MediaItemService.ingest_reading_work(lib.id, work) == {
                source.path: "empty_content",
                other: "empty_content",
            }
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
                "_build_content",
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
                if function is media_service._build_content:
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
            name = "_build_content" if phase == "build" else "_publish_content"
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
