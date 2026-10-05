"""Tests for reading details, summaries, covers, novel content and source boundaries."""

import asyncio
import hashlib
import os
import threading
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from sanic import Sanic
from sanic.response import BaseHTTPResponse
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist

from app.core.config import KaloscopeConfig
from app.core.exceptions import ForbiddenException, NotFoundException, error_handler
from app.core.media import metadata_reader
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.handlers.reading import ReadingSource
from app.core.middleware import on_request, on_response
from app.models.media import (
    EpubContent,
    IndexState,
    LibType,
    MediaContentQuery,
    MediaFormat,
    MediaItem,
    MediaLib,
    TextContent,
)
from app.models.user import (
    Permissions,
    PermType,
    User,
    UserInfo,
    UserPermission,
    UserRole,
)
from app.routes.media import media as media_blueprint
from app.services import media as media_service
from app.services.media import MediaItemService
from app.utils.json import dumps

_PNG = b"\x89PNG\r\n\x1a\nimage"


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Keep synchronization locks outside the application's workspace.

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


def _user(lib_ids: list[int] | None = None) -> UserInfo:
    """Build an authenticated test user.

    Args:
        lib_ids: Granted library IDs, or None for an administrator.

    Returns:
        User information with a current session and explicit library permissions.
    """
    now = datetime.now(UTC)
    return UserInfo(
        id=1,
        login_id="test",
        username="Reader",
        avatar=None,
        role=UserRole.ADMIN if lib_ids is None else UserRole.USER,
        preferences={},
        client_ip="127.0.0.1",
        login_at=now,
        expire_at=now + timedelta(hours=1),
        last_activity=now,
        perms=None if lib_ids is None else Permissions(media_lib_ids=lib_ids),
    )


def _opf(title: str, fields: str = "") -> bytes:
    """Build external metadata without a readable EPUB body.

    Args:
        title: The title value in a Dublin Core element.
        fields: Additional metadata XML, empty by default.

    Returns:
        Encoded OPF bytes.
    """
    return (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<metadata><dc:title>{title}</dc:title>{fields}</metadata></package>"
    ).encode()


async def _item(tmp_path: Path, format: MediaFormat | None) -> MediaItem:
    """Create one indexed source with deliberately outdated database summaries.

    Args:
        tmp_path: The isolated filesystem root.
        format: The reading format, or None for an empty comic collection.

    Returns:
        The item with its library assigned and body file or directory created.
    """
    root = tmp_path / "Library"
    directory = root / "Work"
    directory.mkdir(parents=True)
    lib = await MediaLib.create(
        name="Reading",
        dir=str(root),
        priority=1,
        lib_type=LibType.NOVEL
        if format in (MediaFormat.TXT, MediaFormat.EPUB)
        else LibType.COMIC,
    )
    path = (
        directory if format in (None, MediaFormat.DIR) else directory / f"Book.{format}"
    )
    if format == MediaFormat.TXT:
        path.write_text("Body")
    elif format == MediaFormat.DIR:
        (path / "1.png").write_bytes(_PNG)
    elif format in (MediaFormat.CBZ, MediaFormat.ZIP):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("1.png", _PNG)
            archive.writestr(
                "ComicInfo.xml", "<ComicInfo><Title>Embedded</Title></ComicInfo>"
            )
    elif format == MediaFormat.EPUB:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr(
                "META-INF/container.xml",
                (
                    "<container "
                    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                    'version="1.0"><rootfiles><rootfile full-path="book.opf" '
                    'media-type="application/oebps-package+xml"/>'
                    "</rootfiles></container>"
                ),
            )
            archive.writestr(
                "book.opf", _opf("Embedded", "<dc:creator>Embedded author</dc:creator>")
            )
    return await MediaItem.create(
        lib=lib,
        path=str(path),
        dir=str(directory),
        name="Book",
        format=format,
        title="Old database title",
        year=1980,
        rating=9,
        poster="https://old.example/cover.png",
        backdrop="https://old.example/backdrop.png",
        index_state=IndexState.PENDING,
        extra={"metadata_sync": {"state": "ready"}, "plot": "Old cached plot"},
    )


@pytest.mark.parametrize("format", [None, *MediaFormat])
def test_sync_metadata(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            await MediaItem.filter(id=item.id).update(
                visible=False,
                index_state=IndexState.READY,
                index_version="a" * 64,
                extra={"content": {"chapter_count": 2, "page_count": None}},
            )
            novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
            xml = Path(item.dir) / ("metadata.opf" if novel else "ComicInfo.xml")
            xml.write_bytes(
                _opf(
                    "文" * 300,
                    "<dc:date>2024</dc:date><dc:description>Plot</dc:description>"
                    "<dc:creator>Author</dc:creator>"
                    "<meta name='calibre:rating' content='0'/>",
                )
                if novel
                else (
                    "<ComicInfo><Title>" + "文" * 300 + "</Title><Year>2024</Year>"
                    "<CommunityRating>0</CommunityRating><Summary>Plot</Summary>"
                    "<Writer>Author</Writer></ComicInfo>"
                ).encode()
            )
            before = await MediaItem.get(id=item.id).values()
            files = {
                path: path.read_bytes()
                for path in Path(item.lib.dir).rglob("*")
                if path.is_file()
            }
            current = await MediaItemService.sync_metadata(item.id)
            assert current.title == "文" * 255
            assert current.year == 2024 and current.rating == 0
            assert current.poster == f"/_api/media/{item.id}/assets/cover"
            assert current.extra == {
                "schema_version": 1,
                "content": {"chapter_count": 2, "page_count": None},
                "metadata_sync": {
                    "state": "ready",
                    "format": "opf" if novel else "comicinfo",
                    "relative_path": xml.name,
                    "file_signature": hashlib.sha256(xml.read_bytes()).hexdigest(),
                    "error": None,
                },
            }
            after = await MediaItem.get(id=item.id).values()
            changed = {"title", "year", "rating", "poster", "extra", "updated_at"}
            assert {
                key: value for key, value in before.items() if key not in changed
            } == {key: value for key, value in after.items() if key not in changed}
            assert all(path.read_bytes() == data for path, data in files.items())
            assert not (
                Path(KaloscopeConfig.get_workspace("temp")) / "media_index"
            ).exists()
            dumped = await MediaItemService.dump(
                current, exclude={"lib", "parent", "children"}
            )
            assert "extra" not in dumped and "metadata" not in dumped

    asyncio.run(run())


@pytest.mark.parametrize("format", [None, *MediaFormat])
def test_sync_updates(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
            xml = Path(item.dir) / ("metadata.opf" if novel else "ComicInfo.xml")
            xml.write_bytes(
                _opf("First")
                if novel
                else b"<ComicInfo><Title>First</Title></ComicInfo>"
            )
            first = await MediaItemService.sync_metadata(item.id)
            assert first.title == "First" and first.year is first.rating is None
            before = xml.stat()
            xml.write_bytes(xml.read_bytes().replace(b"First", b"Other"))
            os.utime(xml, ns=(before.st_atime_ns, before.st_mtime_ns))
            current = await MediaItemService.sync_metadata(item.id)
            assert current.title == "Other" and current.extra != first.extra
            xml.unlink()
            current = await MediaItemService.sync_metadata(item.id)
            expected = (
                "Embedded"
                if format in (MediaFormat.EPUB, MediaFormat.CBZ, MediaFormat.ZIP)
                else "Work"
                if format in (None, MediaFormat.DIR)
                else "Book"
            )
            assert current.title == expected and current.year is current.rating is None
            assert current.extra is not None
            assert current.extra["metadata_sync"] == {
                "state": "none",
                "format": None,
                "relative_path": None,
                "file_signature": None,
                "error": None,
            }
            assert current.index_state == item.index_state
            assert current.index_version is None and not xml.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("problem", "code"),
    [
        ("invalid", "invalid_metadata"),
        ("ambiguous", "ambiguous_metadata"),
        ("embedded", "invalid_archive"),
        ("missing", "media_source_unavailable"),
    ],
)
def test_sync_failure(tmp_path, monkeypatch, problem, code):
    def ambiguous(directory, names):
        """Simulate case-variant conflicts on case-insensitive test filesystems.

        Args:
            directory: The metadata container being searched.
            names: The preferred metadata filenames.

        Raises:
            ContentError: To report multiple candidates at the same priority.
        """
        raise ContentError("ambiguous_metadata")

    async def run():
        async with _database():
            item = await _item(
                tmp_path, MediaFormat.EPUB if problem == "embedded" else MediaFormat.TXT
            )
            path = Path(item.path)
            body = path.read_bytes()
            xml = Path(item.dir) / "metadata.opf"
            if problem in ("invalid", "ambiguous"):
                xml.write_bytes(b"invalid" if problem == "invalid" else _opf("Current"))
            elif problem == "embedded":
                path.write_bytes(b"invalid")
            else:
                path.unlink()
            with monkeypatch.context() as patcher:
                if problem == "ambiguous":
                    patcher.setattr(metadata_reader, "_find_external", ambiguous)
                current = await MediaItemService.sync_metadata(item.id)
            assert (current.title, current.year, current.rating, current.poster) == (
                item.title,
                item.year,
                item.rating,
                item.poster,
            )
            assert current.index_state == item.index_state
            assert current.index_version == item.index_version
            assert current.extra is not None
            sync = current.extra["metadata_sync"]
            assert sync["state"] == "error" and sync["error"] == code
            assert sync["file_signature"] is None
            if problem == "ambiguous":
                assert sync["relative_path"] is None
            if problem in ("invalid", "ambiguous"):
                xml.write_bytes(_opf("Recovered"))
            else:
                path.write_bytes(body)
            recovered = await MediaItemService.sync_metadata(item.id)
            assert recovered.extra is not None
            assert recovered.extra["metadata_sync"]["error"] is None
            assert recovered.title != item.title

    asyncio.run(run())


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("broken", [False, True])
def test_sync_parent(tmp_path, external, broken):
    async def run():
        async with _database():
            parent = await _item(tmp_path, None)
            (Path(parent.path) / "ComicInfo.xml").write_bytes(
                b"broken"
                if broken
                else b"<ComicInfo><Title>Parent</Title>"
                b"<Writer>Author</Writer><Year>2000</Year></ComicInfo>"
            )
            directory = Path(parent.path) / "Chapter"
            directory.mkdir()
            (directory / "1.png").write_bytes(_PNG)
            if external:
                (directory / "ComicInfo.xml").write_bytes(
                    b"<ComicInfo><Title>Current</Title></ComicInfo>"
                )
            child = await MediaItem.create(
                lib_id=parent.lib_id,
                parent=parent,
                path=str(directory),
                dir=str(directory),
                name="Chapter",
                format=MediaFormat.DIR,
                year=1980,
                rating=9,
            )
            before = await MediaItem.get(id=parent.id).values()
            current = await MediaItemService.sync_metadata(child.id)
            assert current.title == ("Current" if external else "Chapter")
            assert current.year is current.rating is None
            assert current.extra is not None
            assert set(current.extra) == {"schema_version", "metadata_sync"}
            assert current.extra["metadata_sync"]["state"] == (
                "ready" if external else "none"
            )
            assert current.extra["metadata_sync"]["error"] is None
            assert await MediaItem.get(id=parent.id).values() == before
            details = await MediaItemService.get_details(child.id, _user())
            assert details["metadata"]["authors"] == (() if broken else ("Author",))
            assert details["metadata_state"] == ("error" if broken else "ready")

    asyncio.run(run())


@pytest.mark.parametrize("change", ["move", "delete", "library", "state"])
def test_sync_ownership(tmp_path, monkeypatch, change):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            worker = media_service.to_thread

            async def modify(function, *args, **kwargs):
                """Change ownership or unrelated fields after the read completes.

                Args:
                    function: The reader dispatched to the worker.
                    *args: The reader's positional arguments.
                    **kwargs: The reader's keyword options.

                Returns:
                    The original read result before the concurrent change.
                """
                result = await worker(function, *args, **kwargs)
                if change == "move":
                    moved = Path(item.dir).with_name("Moved")
                    Path(item.dir).rename(moved)
                    await MediaItem.filter(id=item.id).update(
                        path=str(moved / "Book.txt"), dir=str(moved)
                    )
                elif change == "delete":
                    await item.delete()
                elif change == "library":
                    await MediaLib.filter(id=item.lib_id).update(dir=str(tmp_path))
                else:
                    await MediaItem.filter(id=item.id).update(
                        visible=False,
                        index_state=IndexState.ERROR,
                        index_error="empty_content",
                        extra={"content": {"chapter_count": 7}},
                    )
                return result

            monkeypatch.setattr(media_service, "to_thread", modify)
            if change == "state":
                current = await MediaItemService.sync_metadata(item.id)
                assert not current.visible
                assert current.index_state == IndexState.ERROR
                assert current.index_error == "empty_content"
                assert current.extra is not None
                assert current.extra["content"] == {"chapter_count": 7}
            else:
                with pytest.raises(ContentError, match="content_changed"):
                    await MediaItemService.sync_metadata(item.id)
                current = await MediaItem.get_or_none(id=item.id)
                if change == "delete":
                    assert current is None
                else:
                    assert current is not None
                    assert current.extra == item.extra and current.title == item.title

    asyncio.run(run())


@pytest.mark.parametrize("problem", ["video", "missing", "outside", "symlink"])
def test_sync_source(tmp_path, problem):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            if problem == "video":
                await MediaLib.filter(id=item.lib_id).update(lib_type=LibType.MOVIE)
            elif problem == "missing":
                await item.delete()
            elif problem == "outside":
                await MediaItem.filter(id=item.id).update(
                    path=str(tmp_path / "Book.txt")
                )
            else:
                path = Path(item.path)
                moved = path.with_name("Other.txt")
                path.rename(moved)
                path.symlink_to(moved)
            before = await MediaItem.all().values()
            with pytest.raises(DoesNotExist if problem == "missing" else ContentError):
                await MediaItemService.sync_metadata(item.id)
            assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_sync_worker(tmp_path, monkeypatch, cancel):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            before = await MediaItem.get(id=item.id).values()
            started, finish, stopped = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )
            loop_thread = threading.get_ident()
            reader = media_service._read_reading

            def blocked(*args, **kwargs):
                """Keep a read active while checking cancellation and library locking.

                Args:
                    *args: The reader's positional arguments.
                    **kwargs: The reader's keyword options.

                Returns:
                    The original reader result after the test releases the worker.
                """
                try:
                    assert threading.get_ident() != loop_thread
                    result = reader(*args, **kwargs)
                    started.set()
                    assert finish.wait(timeout=5)
                    return result
                finally:
                    stopped.set()

            monkeypatch.setattr(media_service, "_read_reading", blocked)
            task = asyncio.create_task(MediaItemService.sync_metadata(item.id))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                async with await library_lock(item.lib.dir).acquire(timeout=0):
                    assert await MediaItem.get(id=item.id).values() == before
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
            finally:
                finish.set()
                assert await asyncio.to_thread(stopped.wait, 5)
            if cancel:
                assert await MediaItem.get(id=item.id).values() == before
            else:
                assert (await task).title == "Book"

    asyncio.run(run())


@pytest.mark.parametrize("format", [None, *MediaFormat])
def test_current_details(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
            xml = Path(item.dir) / ("metadata.opf" if novel else "ComicInfo.xml")
            xml.write_bytes(
                _opf("Current", "<dc:description>Fresh plot</dc:description>")
                if novel
                else b"<ComicInfo><Title>Current</Title>"
                b"<Summary>Fresh plot</Summary></ComicInfo>"
            )
            before = await MediaItem.filter(id=item.id).values()
            data = await MediaItemService.get_details(item.id, _user([item.lib_id]))
            assert (
                data["title"] == "Current" and data["metadata"]["plot"] == "Fresh plot"
            )
            assert data["metadata_state"] == "ready" and data["metadata_issues"] == []
            assert data["year"] is data["rating"] is data["backdrop"] is None
            assert data["poster"] == f"/_api/media/{item.id}/assets/cover"
            assert data["media_type"] == ("text" if novel else "image")
            assert data["index_state"] == "pending" and "extra" not in data
            assert (
                "cover" not in data["metadata"] and "nfo_path" not in data["metadata"]
            )
            assert (
                not {"external", "embedded", "signature", "member"}
                & data["metadata"].keys()
            )
            assert await MediaItem.filter(id=item.id).values() == before
            if format == MediaFormat.EPUB:
                assert data["metadata"]["authors"] == ("Embedded author",)

    asyncio.run(run())


def test_detail_updates(tmp_path):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            path = Path(item.dir) / "metadata.opf"
            path.write_bytes(_opf("First"))
            assert (await MediaItemService.get_details(item.id, _user()))[
                "title"
            ] == "First"
            before = path.stat()
            path.write_bytes(_opf("Other"))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            assert (await MediaItemService.get_details(item.id, _user()))[
                "title"
            ] == "Other"
            path.unlink()
            data = await MediaItemService.get_details(item.id, _user())
            assert data["title"] == "Book" and data["metadata_state"] == "missing"
            assert data["metadata"]["plot"] is None

    asyncio.run(run())


@pytest.mark.parametrize("broken", [False, True])
def test_metadata_issues(tmp_path, broken):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.EPUB)
            (Path(item.dir) / "metadata.opf").write_bytes(
                b"broken"
                if broken
                else _opf("Title", "<meta name='calibre:rating' content='200'/>")
            )
            data = await MediaItemService.get_details(item.id, _user())
            assert data["metadata_state"] == ("error" if broken else "ready")
            assert data["title"] == ("Embedded" if broken else "Title")
            assert data["metadata_issues"] == [
                {
                    "source": "external",
                    "error": "invalid_metadata" if broken else None,
                    "invalid_fields": () if broken else ("rating",),
                }
            ]

    asyncio.run(run())


@pytest.mark.parametrize("access", ["denied", "unloaded", "hidden", "missing"])
@pytest.mark.parametrize("method", ["get_details", "get_cover"])
def test_access_before_files(tmp_path, monkeypatch, access, method):
    def unexpected(*args, **kwargs):
        """Fail if an inaccessible request reaches the filesystem reader.

        Args:
            args: Reader positional arguments.
            kwargs: Reader keyword options.

        Raises:
            AssertionError: If file reading was attempted.
        """
        raise AssertionError("unauthorized file read")

    monkeypatch.setattr(media_service, "_read_reading", unexpected)

    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            user = _user([] if access in ("denied", "unloaded") else None)
            if access == "unloaded":
                user.perms = None
            elif access == "hidden":
                await MediaItem.filter(id=item.id).update(visible=False)
            elif access == "missing":
                await item.delete()
            error = (
                ForbiddenException
                if access in ("denied", "unloaded")
                else NotFoundException
            )
            with pytest.raises(error):
                await getattr(MediaItemService, method)(item.id, user)

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem",
    [
        "outside",
        "root",
        "symlink",
        "ancestor",
        "ambiguous",
        "missing",
        "format",
        "dir",
        "depth",
    ],
)
def test_source_boundary(tmp_path, problem):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            path = Path(item.path)
            (path.parent / "metadata.opf").write_bytes(_opf("Private"))
            if problem == "outside":
                outside = tmp_path / "Outside"
                path.parent.rename(outside)
                await MediaItem.filter(id=item.id).update(
                    path=str(outside / path.name), dir=str(outside)
                )
            elif problem == "root":
                await MediaItem.filter(id=item.id).update(path=item.lib.dir)
            elif problem in ("symlink", "ancestor"):
                target = path if problem == "symlink" else path.parent.parent
                moved = target.with_name(target.name + "-moved")
                target.rename(moved)
                target.symlink_to(moved, target_is_directory=problem == "ancestor")
            elif problem == "ambiguous":
                (path.parent / "Other.txt").write_text("Other")
            elif problem == "missing":
                path.unlink()
            elif problem == "format":
                await MediaItem.filter(id=item.id).update(format=MediaFormat.CBZ)
            elif problem == "dir":
                await MediaItem.filter(id=item.id).update(dir=str(tmp_path))
            else:
                nested = path.parent / "Nested"
                nested.mkdir()
                path.rename(nested / path.name)
                await MediaItem.filter(id=item.id).update(
                    path=str(nested / path.name), dir=str(nested)
                )
            data = await MediaItemService.get_details(item.id, _user())
            assert data["metadata_state"] == "error"
            assert (data["title"] == "Private") is (problem == "missing")
            assert data["metadata_issues"][0]["source"] == "source"
            with pytest.raises(ContentError):
                await MediaItemService.get_cover(item.id, _user())

    asyncio.run(run())


@pytest.mark.parametrize("parent_state", ["valid", "hidden", "foreign", "wrong_path"])
def test_parent_scope(tmp_path, parent_state):
    async def run():
        async with _database():
            parent = await _item(tmp_path, None)
            directory = Path(parent.path) / "Chapter"
            directory.mkdir()
            (directory / "1.png").write_bytes(_PNG)
            (directory / "ComicInfo.xml").write_text(
                "<ComicInfo><Title>Chapter</Title></ComicInfo>"
            )
            (Path(parent.path) / "ComicInfo.xml").write_text(
                "<ComicInfo><Title>Work</Title><Writer>Author</Writer></ComicInfo>"
            )
            child = await MediaItem.create(
                lib_id=parent.lib_id,
                parent=parent,
                path=str(directory),
                dir=str(directory),
                name="Chapter",
                format=MediaFormat.DIR,
            )
            if parent_state == "hidden":
                await MediaItem.filter(id=parent.id).update(visible=False)
            elif parent_state == "foreign":
                other = await MediaLib.create(
                    name="Other",
                    dir=str(tmp_path / "Other"),
                    lib_type=LibType.COMIC,
                    priority=2,
                )
                await MediaItem.filter(id=parent.id).update(lib_id=other.id)
            elif parent_state == "wrong_path":
                await MediaItem.filter(id=parent.id).update(
                    path=str(Path(parent.path).with_name("Different"))
                )
            if parent_state in ("hidden", "foreign"):
                with pytest.raises(NotFoundException):
                    await MediaItemService.get_details(child.id, _user())
            else:
                data = await MediaItemService.get_details(child.id, _user())
                assert data["metadata_state"] == (
                    "ready" if parent_state == "valid" else "error"
                )
                assert data["metadata"]["authors"] == (
                    ("Author",) if parent_state == "valid" else ()
                )
                assert data["title"] == "Chapter"

    asyncio.run(run())


@pytest.mark.parametrize(
    "format", [MediaFormat.TXT, MediaFormat.EPUB, MediaFormat.CBZ, MediaFormat.ZIP]
)
@pytest.mark.parametrize("external", [False, True])
def test_moved_file_metadata(tmp_path, format, external):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            novel = item.lib.lib_type == LibType.NOVEL
            previous = Path(item.path)
            old_metadata = Path(item.dir) / (
                "metadata.opf" if novel else "ComicInfo.xml"
            )
            xml = (
                _opf("Original")
                if novel
                else b"<ComicInfo><Title>Original</Title></ComicInfo>"
            )
            old_metadata.write_bytes(xml)
            old_cover = Path(item.dir) / "cover.png"
            old_cover.write_bytes(_PNG + b"old")
            directory = Path(item.lib.dir) / "Moved"
            directory.mkdir()
            destination = directory / f"Moved.{format}"
            previous.rename(destination)
            if external:
                (directory / old_metadata.name).write_bytes(
                    xml.replace(b"Original", b"Current")
                )
                (directory / "cover.png").write_bytes(_PNG + b"new")
            moved = await MediaItemService.move_reading_file(
                item.lib_id, previous, destination
            )
            assert moved is not None and moved.id == item.id
            details = await MediaItemService.get_details(item.id, _user([item.lib_id]))
            title = (
                "Current"
                if external
                else "Moved"
                if format == MediaFormat.TXT
                else "Embedded"
            )
            assert details["title"] == title
            cover = await MediaItemService.get_cover(item.id, _user([item.lib_id]))
            if external:
                assert cover is not None and cover.path == directory / "cover.png"
                assert cover.data == _PNG + b"new"
            else:
                assert cover is None or (
                    cover.path == destination and cover.data == _PNG
                )
            current = await MediaItemService.sync_metadata(item.id)
            assert current.title == title and current.extra is not None
            assert current.extra["metadata_sync"]["state"] == (
                "ready" if external else "none"
            )
            assert old_metadata.read_bytes() == xml
            assert old_cover.read_bytes() == _PNG + b"old"
            assert await MediaItem.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("format", "move_type"),
    [
        (format, move_type)
        for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP)
        for move_type in ("rename", "directory")
    ]
    + [(format, "file") for format in (MediaFormat.CBZ, MediaFormat.ZIP)],
)
def test_moved_chapter_metadata(tmp_path, format, move_type):
    async def run():
        async with _database():
            parent = await _item(tmp_path, None)
            work = Path(parent.path)
            directory = work / "Chapter"
            directory.mkdir()
            path = (
                directory if format == MediaFormat.DIR else directory / f"Book.{format}"
            )
            if format == MediaFormat.DIR:
                (directory / "1.png").write_bytes(_PNG)
            else:
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("1.png", _PNG)
            (work / "ComicInfo.xml").write_text(
                "<ComicInfo><Writer>Author</Writer></ComicInfo>"
            )
            xml = "<ComicInfo><Title>Chapter</Title></ComicInfo>"
            (directory / "ComicInfo.xml").write_text(xml)
            (directory / "cover.png").write_bytes(_PNG)
            child = await MediaItem.create(
                lib_id=parent.lib_id,
                parent=parent,
                dir=str(directory),
                path=str(path),
                name="Chapter",
                format=format,
            )
            destination = directory.with_name("Renamed")
            target = parent
            if move_type != "rename":
                other = work.with_name("Other")
                other.mkdir()
                (other / "ComicInfo.xml").write_text(
                    "<ComicInfo><Writer>New author</Writer></ComicInfo>"
                )
                destination = other / "Chapter"
            if move_type == "file":
                destination.mkdir()
                path.rename(destination / path.name)
                (destination / "ComicInfo.xml").write_text(
                    xml.replace("Chapter", "New chapter")
                )
                (destination / "cover.png").write_bytes(_PNG + b"new")
            else:
                directory.rename(destination)
            if move_type != "rename":
                target = await MediaItemService.create_reading(
                    parent.lib_id, ReadingSource(destination.parent, None)
                )
            if move_type == "file":
                await MediaItemService.move_reading_file(
                    parent.lib_id, path, destination / path.name
                )
                assert (directory / "ComicInfo.xml").read_text() == xml
                assert (directory / "cover.png").read_bytes() == _PNG
            else:
                await MediaItemService.move_reading_directory(
                    parent.lib_id, directory, destination
                )
            data = await MediaItemService.get_details(child.id, _user([parent.lib_id]))
            title = "New chapter" if move_type == "file" else "Chapter"
            assert data["title"] == title and data["metadata_state"] == "ready"
            assert data["metadata"]["authors"] == (
                "Author" if move_type == "rename" else "New author",
            )
            assert data["parent"]["id"] == target.id
            cover = await MediaItemService.get_cover(child.id, _user([parent.lib_id]))
            assert cover is not None and cover.path == destination / "cover.png"
            assert cover.data == (_PNG + b"new" if move_type == "file" else _PNG)
            (destination / "ComicInfo.xml").write_text(xml.replace("Chapter", "Fresh"))
            (Path(target.path) / "ComicInfo.xml").write_text(
                "<ComicInfo><Writer>Current author</Writer></ComicInfo>"
            )
            if move_type != "rename":
                (work / "ComicInfo.xml").write_text(
                    "<ComicInfo><Writer>Old author</Writer></ComicInfo>"
                )
            data = await MediaItemService.get_details(child.id, _user([parent.lib_id]))
            assert data["title"] == "Fresh"
            assert data["metadata"]["authors"] == ("Current author",)
            synced = await MediaItemService.sync_metadata(child.id)
            assert synced.title == "Fresh" and synced.parent_id == target.id
            assert await MediaItem.all().count() == (2 if move_type == "rename" else 3)

    asyncio.run(run())


def test_related_items(tmp_path):
    async def run():
        async with _database():
            parent = await _item(tmp_path, None)
            other = await MediaLib.create(
                name="Other",
                dir=str(tmp_path / "Other"),
                lib_type=LibType.COMIC,
                priority=2,
            )
            for name, visible, lib_id in (
                ("Visible", True, parent.lib_id),
                ("Hidden", False, parent.lib_id),
                ("Foreign", True, other.id),
            ):
                directory = Path(parent.path) / name
                directory.mkdir()
                await MediaItem.create(
                    lib_id=lib_id,
                    parent=parent,
                    path=str(directory),
                    dir=str(directory),
                    name=name,
                    visible=visible,
                    format=MediaFormat.DIR,
                )
            data = await MediaItemService.get_details(parent.id, _user([parent.lib_id]))
            assert [child["name"] for child in data["children"]] == ["Visible"]

    asyncio.run(run())


def test_cover_freshness(tmp_path):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            assert await MediaItemService.get_cover(item.id, _user()) is None
            path = Path(item.dir) / "cover.png"
            path.write_bytes(_PNG)
            result = await MediaItemService.get_cover(item.id, _user())
            assert (
                result is not None
                and result.data == _PNG
                and result.mime_type == "image/png"
            )
            path.unlink()
            assert await MediaItemService.get_cover(item.id, _user()) is None

    asyncio.run(run())


@pytest.mark.parametrize("change", ["move", "hide", "repeat"])
def test_read_revalidation(tmp_path, monkeypatch, change):
    original = media_service.to_thread
    calls = 0

    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            main_thread = threading.get_ident()
            reader = metadata_reader.read_metadata

            def in_worker(source):
                """Assert that synchronous metadata I/O runs off the event loop.

                Args:
                    source: The validated reading source.

                Returns:
                    Current metadata from the original reader.
                """
                assert threading.get_ident() != main_thread
                return reader(source)

            async def modify(function, *args, **kwargs):
                """Change database ownership immediately after worker completion.

                Args:
                    function: The worker function being awaited.
                    args: Its positional arguments.
                    kwargs: Its keyword arguments.

                Returns:
                    The original worker result before the concurrent change.
                """
                nonlocal calls
                result = await original(function, *args, **kwargs)
                calls += 1
                if change == "hide":
                    await MediaItem.filter(id=item.id).update(visible=False)
                elif calls == 1 or change == "repeat":
                    current = await MediaItem.get(id=item.id)
                    moved = Path(current.dir).with_name(f"Moved{calls}")
                    Path(current.dir).rename(moved)
                    (moved / "metadata.opf").write_bytes(_opf("Moved"))
                    await MediaItem.filter(id=item.id).update(
                        path=str(moved / "Book.txt"), dir=str(moved)
                    )
                return result

            monkeypatch.setattr(metadata_reader, "read_metadata", in_worker)
            monkeypatch.setattr(media_service, "to_thread", modify)
            if change == "hide":
                with pytest.raises(NotFoundException):
                    await MediaItemService.get_details(item.id, _user())
            else:
                data = await MediaItemService.get_details(item.id, _user())
                assert calls == 2
                assert data["metadata_state"] == (
                    "ready" if change == "move" else "error"
                )
                assert data["title"] == ("Moved" if change == "move" else "Book")

    asyncio.run(run())


@asynccontextmanager
async def _client(user: UserInfo) -> AsyncGenerator[httpx.AsyncClient]:
    """Exercise real routes and authentication without production startup listeners.

    Args:
        user: The authenticated session available to the isolated application.

    Yields:
        An in-process HTTP client using the application's middleware and serializer.
    """
    original_dumps = BaseHTTPResponse._dumps
    app = Sanic(f"reading_{uuid4().hex}", configure_logging=False, dumps=dumps)
    app.config.AUTO_EXTEND = False
    app.config.TOUCHUP = False
    app.config.AUTH_EXCLUDE_PATHS = []
    app.config.TOKEN_EXPIRATION_HOURS = 1
    app.shared_ctx.sessions = {"test-token": user}
    app.blueprint(media_blueprint, url_prefix="/_api/media")
    app.register_middleware(on_request, "request")
    app.register_middleware(on_response, "response")
    app.error_handler.add(Exception, error_handler)
    app.asgi = True
    try:
        await app._startup()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as client:
            yield client
    finally:
        Sanic.unregister_app(app)
        BaseHTTPResponse._dumps = original_dumps


@pytest.mark.parametrize("format", [None, *MediaFormat])
def test_reading_http(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            directory = Path(item.dir)
            novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
            xml = directory / ("metadata.opf" if novel else "ComicInfo.xml")
            xml.write_bytes(
                _opf("HTTP", '<meta name="calibre:rating" content="0"/>')
                if novel
                else b"<ComicInfo><Title>HTTP</Title>"
                b"<CommunityRating>0</CommunityRating></ComicInfo>"
            )
            (directory / "cover.png").write_bytes(_PNG)
            user = _user([item.lib_id])
            await User.create(
                id=user.id, username="Reader", password="unused", role=UserRole.USER
            )
            await UserPermission.create(
                user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=item.lib_id
            )
            async with _client(user) as client:
                url = f"/_api/media/{item.id}"
                response = await client.get(url)
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["title"] == "HTTP" and data["metadata"]["rating"] == 0
                assert data["rating"] == 0 and data["metadata_state"] == "ready"
                assert data["metadata_issues"] == [] and data["lib"]["triggers"] == []
                assert response.headers["cache-control"] == "private, no-store"
                image = await client.get(
                    data["poster"], params={"path": "/untrusted.png"}
                )
                assert image.status_code == 200 and image.content == _PNG
                assert image.headers["content-type"] == "image/png"
                assert image.headers["cache-control"] == "private, no-store"
                assert image.headers["x-content-type-options"] == "nosniff"
                await UserPermission.all().delete()
                assert (await client.get(url)).status_code == 403
                assert (await client.get(data["poster"])).status_code == 403
                client.headers.clear()
                client.cookies.clear()
                assert (await client.get(url)).status_code == 401
                assert (await client.get(data["poster"])).status_code == 401

    asyncio.run(run())


@pytest.mark.parametrize("lib_type", [LibType.MOVIE, LibType.TV_SHOW])
def test_video_details_http(tmp_path, lib_type):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            await MediaLib.filter(id=item.lib_id).update(lib_type=lib_type)
            path = Path(item.dir) / "video.nfo"
            root = "movie" if lib_type == LibType.MOVIE else "tvshow"
            path.write_text(
                f"<{root}><title>Video metadata</title><plot>NFO plot</plot></{root}>"
            )
            await MediaItem.filter(id=item.id).update(format=None, nfo_path=str(path))
            async with _client(_user()) as client:
                url = f"/_api/media/{item.id}"
                response = await client.get(url)
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["title"] == "Old database title"
                assert data["metadata"]["title"] == "Video metadata"
                assert data["metadata"]["plot"] == "NFO plot"
                assert data["media_type"] == "video" and "metadata_state" not in data
                assert (await client.get(url + "/assets/cover")).status_code == 404
                await MediaItem.filter(id=item.id).update(visible=False)
                assert (await client.get(url)).status_code == 404

    asyncio.run(run())


def test_cover_errors_http(tmp_path):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            async with _client(_user()) as client:
                url = f"/_api/media/{item.id}/assets/cover"
                response = await client.get(url)
                assert response.status_code == 404
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["x-content-type-options"] == "nosniff"
                (Path(item.dir) / "cover.png").write_bytes(_PNG)
                assert (await client.get(url)).content == _PNG
                (Path(item.dir) / "Other.txt").write_text("Conflicting body")
                response = await client.get(url)
                assert (
                    response.status_code == 422
                    and response.json()["message"] == "ambiguous_layout"
                )
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["x-content-type-options"] == "nosniff"
                Path(item.path).unlink()
                (Path(item.dir) / "Other.txt").unlink()
                response = await client.get(url)
                assert (
                    response.status_code == 503
                    and response.json()["message"] == "media_source_unavailable"
                )
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["x-content-type-options"] == "nosniff"
                await MediaItem.filter(id=item.id).update(visible=False)
                response = await client.get(url)
                assert response.status_code == 404
                assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(run())


async def _indexed_text(
    tmp_path: Path, body: str = "Chapter 1\n\nFirst\n\nChapter 2\n\nSecond"
) -> MediaItem:
    """Publish a real TXT index without starting a library consumer.

    Args:
        tmp_path: The isolated source and cache root.
        body: The novel text, defaulting to two short chapters.

    Returns:
        The ready item whose source and cache may be changed by a test.
    """
    item = await _item(tmp_path, MediaFormat.TXT)
    Path(item.path).write_text(body)
    return await MediaItemService.index_content(item.id)


def test_text_content_http(tmp_path):
    """Read chapters through authentication and retain live metadata semantics.

    Args:
        tmp_path: The isolated source and cache root.
    """

    async def run():
        async with _database():
            body = "Chapter 1\n\n<script>alert(1)</script>\n\nChapter 2\n\n中文正文"
            item = await _indexed_text(tmp_path, body)
            metadata = Path(item.dir) / "metadata.opf"
            metadata.write_bytes(_opf("Current title"))
            user = _user([item.lib_id])
            await User.create(
                id=user.id, username="Reader", password="unused", role=UserRole.USER
            )
            await UserPermission.create(
                user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=item.lib_id
            )
            async with _client(user) as client:
                url = f"/_api/media/{item.id}/content"
                response = await client.get(url)
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data == {
                    "item_id": item.id,
                    "source_item_id": item.id,
                    "media_type": "text",
                    "format": "txt",
                    "content_type": "text",
                    "title": "Current title",
                    "version": item.index_version,
                    "chapter_id": data["chapters"][0]["id"],
                    "chapters": data["chapters"],
                    "text": ["Chapter 1", "<script>alert(1)</script>", ""],
                }
                assert [entry["title"] for entry in data["chapters"]] == [
                    "Chapter 1",
                    "Chapter 2",
                ]
                assert all(
                    set(entry) == {"id", "title", "part", "volume"}
                    for entry in data["chapters"]
                )
                assert all(
                    entry["part"] == 1 and entry["volume"] is None
                    for entry in data["chapters"]
                )
                assert str(tmp_path) not in response.text
                assert response.headers["cache-control"] == "private, no-store"
                assert response.headers["x-content-type-options"] == "nosniff"
                metadata.write_bytes(_opf("Changed title"))
                response = await client.get(
                    url,
                    params={
                        "chapter_id": data["chapters"][1]["id"],
                        "version": data["version"],
                    },
                )
                assert response.status_code == 200, response.text
                second = response.json()["data"]
                assert second["title"] == "Changed title"
                assert "\n\n".join(data["text"]) + "\n\n".join(second["text"]) == body
                assert second["version"] == data["version"]
                metadata.write_bytes(b"invalid metadata")
                assert (await client.get(url)).json()["data"]["title"] == "Book"
                await UserPermission.all().delete()
                assert (await client.get(url)).status_code == 403
                client.headers.clear()
                client.cookies.clear()
                assert (await client.get(url)).status_code == 401
            saved = await MediaItem.get(id=item.id)
            assert saved.title == "Old database title"
            assert saved.index_version == item.index_version

    asyncio.run(run())


@pytest.mark.parametrize(
    "params,status,code",
    [
        ({"chapter_id": "f" * 32}, 404, "not_found"),
        ({"version": "f" * 64}, 409, "content_changed"),
        ({"chapter_id": "../content.txt"}, 400, "bad_request"),
        ({"chapter_id": "invalid"}, 400, "bad_request"),
        ({"version": "../index.json"}, 400, "bad_request"),
        ({"path": "/untrusted.txt"}, 400, "bad_request"),
    ],
)
def test_text_query_http(tmp_path, params, status, code):
    """Reject unknown chapters and malformed or stale selections.

    Args:
        tmp_path: The isolated source and cache root.
        params: The query values under test.
        status: The expected HTTP status.
        code: The expected application error code.
    """

    async def run():
        async with _database():
            item = await _indexed_text(tmp_path)
            async with _client(_user()) as client:
                response = await client.get(
                    f"/_api/media/{item.id}/content", params=params
                )
                assert response.status_code == status, response.text
                assert response.json()["message"] == code
                if status != 400:
                    assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(run())


@pytest.mark.parametrize(
    "change,code",
    [
        ("source", "content_changed"),
        ("missing_source", "media_source_unavailable"),
        ("source_link", "media_source_unavailable"),
        ("cache", "content_not_ready"),
        ("truncated", "content_not_ready"),
        ("cache_link", "content_not_ready"),
        ("cache_version", "content_not_ready"),
    ],
)
def test_text_content_sources(tmp_path, change, code):
    """Prevent cached text from masking changed or unavailable sources.

    Args:
        tmp_path: The isolated source and cache root.
        change: The source or published cache mutation.
        code: The expected controlled read error.
    """

    async def run():
        async with _database():
            item = await _indexed_text(tmp_path)
            source = Path(item.path)
            cache = (
                tmp_path / "cache/media_index" / str(item.id) / str(item.index_version)
            )
            if change == "source":
                source.write_text("Replaced body")
            elif change == "missing_source":
                source.unlink()
            elif change == "source_link":
                target = tmp_path / "outside.txt"
                source.rename(target)
                source.symlink_to(target)
            elif change == "cache":
                (cache / "index.json").unlink()
            elif change == "truncated":
                (cache / "content.txt").write_text("truncated")
            elif change == "cache_link":
                target = tmp_path / "outside-cache.txt"
                (cache / "content.txt").rename(target)
                (cache / "content.txt").symlink_to(target)
            else:
                path = cache / "index.json"
                path.write_text(
                    path.read_text().replace(str(item.index_version), "f" * 64)
                )
            with pytest.raises(ContentError, match=code):
                await MediaItemService.get_content(
                    item.id, _user(), MediaContentQuery()
                )
            saved = await MediaItem.get(id=item.id)
            assert saved.index_version == item.index_version
            assert saved.index_state == IndexState.READY

    asyncio.run(run())


@pytest.mark.parametrize(
    "change,code",
    [
        ("pending", "content_not_ready"),
        ("empty", "empty_content"),
        ("error", "content_not_ready"),
        ("version", "content_not_ready"),
        ("hidden", "not_found"),
        ("deleted", "not_found"),
        ("denied", "permission_denied"),
        ("video", "not_found"),
        ("format", "unsupported_media_format"),
        ("comic", "unsupported_media_format"),
    ],
)
def test_text_content_access(tmp_path, monkeypatch, change, code):
    """Reject inaccessible and unready requests before reading cache or source files.

    Args:
        tmp_path: The isolated source and cache root.
        monkeypatch: The fixture replacing file reads.
        change: The access, readiness or format change.
        code: The expected error message.
    """

    async def run():
        async with _database():
            item = await _indexed_text(tmp_path)
            user = _user()
            if change in {"pending", "empty", "error"}:
                await MediaItem.filter(id=item.id).update(
                    index_state=IndexState(change)
                )
            elif change == "version":
                await MediaItem.filter(id=item.id).update(index_version="../invalid")
            elif change == "hidden":
                await MediaItem.filter(id=item.id).update(visible=False)
            elif change == "deleted":
                await item.delete()
            elif change == "denied":
                user = _user([])
            elif change == "format":
                await MediaItem.filter(id=item.id).update(format=MediaFormat.ZIP)
            else:
                await MediaLib.filter(id=item.lib_id).update(
                    lib_type=LibType.MOVIE if change == "video" else LibType.COMIC
                )
            reader = Mock(side_effect=AssertionError("unexpected content read"))
            monkeypatch.setattr(media_service, "_read_text_content", reader)
            with pytest.raises(
                (ContentError, NotFoundException, ForbiddenException), match=code
            ):
                await MediaItemService.get_content(item.id, user, MediaContentQuery())
            reader.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["hidden", "deleted", "moved", "version", "pending", "permission"]
)
@pytest.mark.parametrize("mode", ["txt", "epub", "asset"])
def test_content_race(tmp_path, monkeypatch, change, mode):
    """Revalidate database access and source ownership after unlocked file reading.

    Args:
        tmp_path: The isolated source and cache root.
        monkeypatch: The fixture replacing the worker dispatch.
        change: The database mutation occurring during file reading.
        mode: The content or resource read whose result must be discarded.
    """

    async def run():
        async with _database():
            item = await (
                _indexed_text(tmp_path) if mode == "txt" else _indexed_epub(tmp_path)
            )
            user = _user([item.lib_id])
            await User.create(
                id=user.id, username="Reader", password="unused", role=UserRole.USER
            )
            await UserPermission.create(
                user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=item.lib_id
            )
            dispatch = asyncio.to_thread

            async def changed(func, *args):
                """Dispatch the actual read and mutate ownership before it returns.

                Args:
                    func: The synchronous content reader.
                    *args: The reader arguments.

                Returns:
                    The original content result before database revalidation.
                """
                result = await dispatch(func, *args)
                if change == "permission":
                    await UserPermission.all().delete()
                elif change == "deleted":
                    await item.delete()
                else:
                    fields = {
                        "hidden": {"visible": False},
                        "moved": {"path": str(Path(item.dir) / "Moved.txt")},
                        "version": {"index_version": "f" * 64},
                        "pending": {"index_state": IndexState.PENDING},
                    }
                    await MediaItem.filter(id=item.id).update(**fields[change])
                return result

            monkeypatch.setattr(media_service, "to_thread", changed)
            expected = (
                NotFoundException
                if change in {"hidden", "deleted"}
                else ForbiddenException
                if change == "permission"
                else ContentError
            )
            with pytest.raises(expected):
                if mode == "asset":
                    await MediaItemService.get_asset(
                        item.id, user, _epub_asset_id(), str(item.index_version)
                    )
                else:
                    await MediaItemService.get_content(
                        item.id, user, MediaContentQuery()
                    )

    asyncio.run(run())


def test_text_content_limit(tmp_path):
    """Include chapter labels when enforcing the HTTP response size limit.

    Args:
        tmp_path: The isolated source and cache root.
    """

    async def run():
        async with _database():
            body = "\n\n".join(
                f"Chapter {index} " + "长" * 100 + "\n\nBody" for index in range(3500)
            )
            item = await _indexed_text(tmp_path, body)
            async with _client(_user()) as client:
                response = await client.get(f"/_api/media/{item.id}/content")
                assert response.status_code == 422, response.text
                assert response.json()["message"] == "media_limit_exceeded"
                assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(run())


@pytest.mark.parametrize("change", ["source", "index", "body", "sibling"])
@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.EPUB])
def test_text_content_files_change(tmp_path, monkeypatch, change, format):
    """Guard unlocked reads without treating other cache writes as content changes.

    Args:
        tmp_path: The isolated source and cache root.
        monkeypatch: The fixture replacing the chapter reader.
        change: The file changed while reading the selected chapter.
        format: The novel format whose source and cache must remain stable.
    """
    from app.core.media import text as text_media

    async def run():
        async with _database():
            item = await (
                _indexed_text(tmp_path)
                if format == MediaFormat.TXT
                else _indexed_epub(tmp_path)
            )
            reader = text_media.read_text_chapter
            loop_thread = threading.get_ident()

            def changed(cache, chapter_id):
                """Read one chapter and simulate an external writer before returning.

                Args:
                    cache: The current published cache directory.
                    chapter_id: The section selected by the request.

                Returns:
                    The original section read before the external change.
                """
                assert threading.get_ident() != loop_thread
                result = reader(cache, chapter_id)
                if change == "sibling":
                    (cache.parent / "unrelated.tmp").write_text("unrelated")
                else:
                    path = (
                        Path(item.path)
                        if change == "source"
                        else cache
                        / (
                            "index.json"
                            if change == "index"
                            else "content.txt"
                            if format == MediaFormat.TXT
                            else "content.jsonl"
                        )
                    )
                    before = path.stat()
                    path.write_bytes(path.read_bytes().replace(b"Chapter", b"Changed"))
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                return result

            monkeypatch.setattr(text_media, "read_text_chapter", changed)
            # file reading does not need the library lock held by writers
            async with library_lock(item.lib.dir):
                request = MediaItemService.get_content(
                    item.id, _user(), MediaContentQuery()
                )
                if change == "sibling":
                    result = await asyncio.wait_for(request, timeout=3)
                    if isinstance(result, TextContent):
                        assert result.text[0] == "Chapter 1"
                    else:
                        assert isinstance(result, EpubContent) and result.blocks
                else:
                    with pytest.raises(ContentError, match="content_changed"):
                        await asyncio.wait_for(request, timeout=3)

    asyncio.run(run())


def _epub_asset_id(name: str = "picture.png") -> str:
    """Resolve a fixture image's opaque ID without putting its path in requests.

    Args:
        name: The member basename, defaulting to the fixture's first valid image.

    Returns:
        The ID generated by the actual EPUB index builder.
    """
    return hashlib.sha256(f"epub:Book/images/{name}".encode()).hexdigest()[:32]


async def _indexed_epub(tmp_path: Path) -> MediaItem:
    """Publish a real EPUB with mixed blocks, two chapters and unavailable images.

    Args:
        tmp_path: The isolated source and cache root.

    Returns:
        The ready EPUB item, built through the normal indexing service.
    """
    item = await _item(tmp_path, MediaFormat.EPUB)
    bodies = (
        "<h1>Chapter 1</h1><p>Before<strong>Bold</strong><em>Emphasis</em>After</p>"
        '<blockquote>Quote</blockquote><ol start="3"><li>List item</li></ol>'
        '<img src="../images/picture.png" alt="Art"/>'
        '<img src="https://example.test/private.png" alt="Remote"/>'
        '<img src="../images/missing.png" alt="Missing"/>'
        "<script>untrusted_script()</script>",
        '<h1>Chapter 2</h1><p>End</p><img src="../images/other.png" alt="Other"/>',
    )
    manifest = "".join(
        f'<item id="c{i}" href="Text/{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(len(bodies))
    ) + "".join(
        f'<item id="{name}" href="images/{name}.png" media-type="image/png"/>'
        for name in ("picture", "other", "missing", "unused")
    )
    with zipfile.ZipFile(item.path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
            'version="1.0"><rootfiles><rootfile full-path="Book/book.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "Book/book.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Embedded</dc:title></metadata>"
            f"<manifest>{manifest}</manifest><spine>"
            + "".join(f'<itemref idref="c{i}"/>' for i in range(len(bodies)))
            + "</spine></package>",
        )
        for index, body in enumerate(bodies):
            archive.writestr(
                f"Book/Text/{index}.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Fallback</title></head>'
                f"<body>{body}</body></html>",
            )
        archive.writestr("Book/images/picture.png", _PNG)
        archive.writestr("Book/images/other.png", _PNG + b"other")
        archive.writestr("Book/images/unused.png", b"not an indexed image")
    return await MediaItemService.index_content(item.id)


def test_epub_content_http(tmp_path):
    """Read safe blocks and their images through the shared content and asset routes.

    Args:
        tmp_path: The isolated source and cache root.
    """

    async def run():
        async with _database():
            item = await _indexed_epub(tmp_path)
            user = _user([item.lib_id])
            await User.create(
                id=user.id, username="Reader", password="unused", role=UserRole.USER
            )
            await UserPermission.create(
                user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=item.lib_id
            )
            async with _client(user) as client:
                url = f"/_api/media/{item.id}/content"
                response = await client.get(url)
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert (data["media_type"], data["format"], data["content_type"]) == (
                    "text",
                    "epub",
                    "blocks",
                )
                assert data["title"] == "Embedded" and "text" not in data
                assert [entry["title"] for entry in data["chapters"]] == [
                    "Chapter 1",
                    "Chapter 2",
                ]
                assert [block["type"] for block in data["blocks"]] == [
                    "heading",
                    "paragraph",
                    "quote",
                    "list",
                    "image",
                    "image",
                    "image",
                ]
                assert [run["marks"] for run in data["blocks"][1]["runs"]] == [
                    [],
                    ["strong"],
                    ["em"],
                    [],
                ]
                assert data["blocks"][0]["level"] == 1
                assert data["blocks"][3]["start"] == 3 and data["blocks"][3]["ordered"]
                assert {warning["code"] for warning in data["warnings"]} == {
                    "external_image",
                    "missing_image",
                }
                images = [block for block in data["blocks"] if block["type"] == "image"]
                assert all(
                    block["asset_id"] is None and block["url"] is None
                    for block in images[1:]
                )
                for hidden in (
                    str(tmp_path),
                    "Book/images",
                    "Book/Text",
                    "example.test",
                    "untrusted_script",
                ):
                    assert hidden not in response.text
                asset_url = images[0]["url"]
                assert (
                    asset_url == f"/_api/media/{item.id}/assets/{_epub_asset_id()}"
                    f"?v={item.index_version}"
                )
                image = await client.get(asset_url)
                assert image.status_code == 200 and image.content == _PNG
                assert image.headers["content-type"] == "image/png"
                assert image.headers["x-content-type-options"] == "nosniff"
                assert (
                    image.headers["cache-control"]
                    == "private, max-age=0, must-revalidate"
                )
                etag = image.headers["etag"]
                for condition in (etag, f'"unrelated", W/{etag}', "*"):
                    cached = await client.get(
                        asset_url, headers={"If-None-Match": condition}
                    )
                    assert cached.status_code == 304 and cached.content == b""
                    assert cached.headers["etag"] == etag
                assert (
                    await client.get(
                        asset_url, headers={"If-None-Match": '"unrelated"'}
                    )
                ).status_code == 200
                (Path(item.dir) / "metadata.opf").write_bytes(_opf("External"))
                second = await client.get(
                    url,
                    params={
                        "chapter_id": data["chapters"][1]["id"],
                        "version": data["version"],
                    },
                )
                assert (
                    second.status_code == 200
                    and second.json()["data"]["title"] == "External"
                )
                last_image = second.json()["data"]["blocks"][-1]["url"]
                assert (await client.get(last_image)).content == _PNG + b"other"
                # the fixed cover route takes precedence and needs no version
                (Path(item.dir) / "cover.png").write_bytes(_PNG)
                assert (
                    await client.get(f"/_api/media/{item.id}/assets/cover")
                ).content == _PNG
                await UserPermission.all().delete()
                denied = await client.get(asset_url, headers={"If-None-Match": etag})
                assert denied.status_code == 403
                assert (await client.get(url)).status_code == 403
                client.headers.clear()
                client.cookies.clear()
                assert (
                    await client.get(asset_url, headers={"If-None-Match": etag})
                ).status_code == 401
            assert (await MediaItem.get(id=item.id)).index_version == item.index_version

    asyncio.run(run())


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("missing_version", 400, "bad_request"),
        ("invalid_version", 400, "bad_request"),
        ("stale_version", 409, "content_changed"),
        ("path_query", 400, "bad_request"),
        ("unknown", 404, "not_found"),
        ("unindexed", 404, "not_found"),
        ("filename", 404, "not_found"),
        ("txt", 404, "not_found"),
        ("missing_source", 503, "media_source_unavailable"),
        ("source_changed", 409, "content_changed"),
        ("crc_changed", 409, "content_changed"),
        ("missing_cache", 409, "content_not_ready"),
        ("hidden", 404, "not_found"),
        ("pending", 409, "content_not_ready"),
    ],
)
def test_epub_asset_errors(tmp_path, case, status, code):
    """Never let conditional requests bypass access, membership or source checks.

    Args:
        tmp_path: The isolated source and cache root.
        case: The invalid query, access or source condition.
        status: The expected HTTP status.
        code: The stable error exposed to the client.
    """

    async def run():
        async with _database():
            item = await (
                _indexed_text(tmp_path) if case == "txt" else _indexed_epub(tmp_path)
            )
            asset_id = _epub_asset_id()
            params = {"v": str(item.index_version)}
            if case == "missing_version":
                params.clear()
            elif case == "invalid_version":
                params["v"] = "../invalid"
            elif case == "stale_version":
                params["v"] = "f" * 64
            elif case == "path_query":
                params["path"] = "/untrusted.png"
            elif case == "unknown":
                asset_id = "f" * 32
            elif case == "unindexed":
                asset_id = _epub_asset_id("unused.png")
            elif case == "filename":
                asset_id = "picture.png"
            elif case == "missing_source":
                Path(item.path).unlink()
            elif case == "source_changed":
                Path(item.path).write_bytes(b"replaced source")
            elif case == "crc_changed":
                path = Path(item.path)
                before = path.stat()
                with zipfile.ZipFile(path) as archive:
                    entries = [
                        (entry, archive.read(entry)) for entry in archive.infolist()
                    ]
                with zipfile.ZipFile(path, "w") as archive:
                    for entry, data in entries:
                        if entry.filename == "Book/images/picture.png":
                            data = data.replace(b"image", b"other")
                        archive.writestr(entry, data)
                assert path.stat().st_size == before.st_size
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            elif case == "missing_cache":
                cache = (
                    tmp_path
                    / "cache/media_index"
                    / str(item.id)
                    / str(item.index_version)
                )
                (cache / "index.json").unlink()
            elif case == "hidden":
                await MediaItem.filter(id=item.id).update(visible=False)
            elif case == "pending":
                await MediaItem.filter(id=item.id).update(
                    index_state=IndexState.PENDING
                )
            async with _client(_user()) as client:
                response = await client.get(
                    f"/_api/media/{item.id}/assets/{asset_id}",
                    params=params,
                    headers={"If-None-Match": "*"},
                )
                assert response.status_code == status, response.text
                assert response.json()["message"] == code
                assert "etag" not in response.headers
                if status != 400:
                    assert response.headers["cache-control"] == "private, no-store"
                    assert response.headers["x-content-type-options"] == "nosniff"

    asyncio.run(run())


def test_epub_content_corrupt(tmp_path):
    """Reject modified cached blocks and invalid chapter selections through HTTP.

    Args:
        tmp_path: The isolated source and cache root.
    """

    async def run():
        async with _database():
            item = await _indexed_epub(tmp_path)
            async with _client(_user()) as client:
                url = f"/_api/media/{item.id}/content"
                assert (
                    await client.get(url, params={"chapter_id": "f" * 32})
                ).status_code == 404
                cache = (
                    tmp_path
                    / "cache/media_index"
                    / str(item.id)
                    / str(item.index_version)
                )
                path = cache / "content.jsonl"
                path.write_bytes(path.read_bytes().replace(b"Before", b"Broken"))
                response = await client.get(url)
                assert response.status_code == 409, response.text
                assert response.json()["message"] == "content_not_ready"

    asyncio.run(run())
