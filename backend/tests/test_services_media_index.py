"""Tests for reading source registration, index publication and recovery."""

import asyncio
import os
import threading
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from filelock import Timeout
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist

from app.core.config import KaloscopeConfig
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.epub.cache import EpubContent
from app.core.media.handlers.base import get_handler
from app.core.media.handlers.reading import ReadingMediaHandler, ReadingSource
from app.core.media.image import load_image_index, read_image_resource
from app.core.media.text import load_text_index, read_text_chapter
from app.models.media import IndexState, LibType, MediaFormat, MediaItem, MediaLib
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
            current = await MediaItemService.index_content(item.id)
            assert current.index_version != first.index_version
            for row, expected in ((first, "Body"), (current, "Next")):
                cache = _cache(row)
                index = load_text_index(cache)
                assert read_text_chapter(cache, index.chapters[0].id) == [expected]
            assert len(list(_cache(current).parent.iterdir())) == 2

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
def test_failed_rebuild(tmp_path, format, problem, state, code):
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
                await MediaItemService.index_content(item.id)
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
