"""Tests for reading index publication, failure recovery and cancellation."""

import asyncio
import os
import threading
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from filelock import Timeout
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist

from app.core.config import KaloscopeConfig
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.epub.cache import EpubContent
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


async def _item(
    tmp_path: Path, format: MediaFormat, *, chapter: bool = False
) -> MediaItem:
    """Create a real reading source and a pending row without running the watcher.

    Args:
        tmp_path: The isolated filesystem root.
        format: The body format to construct.
        chapter: Whether to create a comic chapter; defaults to a standalone work.

    Returns:
        The pending item with its library assigned.
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
    parent = (
        await MediaItem.create(
            lib=lib, path=str(work), dir=str(work), name="Work", format=None
        )
        if chapter
        else None
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
                    '<manifest><item id="body" href="body.xhtml" '
                    'media-type="application/xhtml+xml"/></manifest>'
                    '<spine><itemref idref="body"/></spine></package>',
                )
                archive.writestr(
                    "body.xhtml",
                    '<html xmlns="http://www.w3.org/1999/xhtml">'
                    "<body><p>Body</p></body></html>",
                )
    return await MediaItem.create(
        lib=lib,
        parent=parent,
        path=str(path),
        dir=str(directory),
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
