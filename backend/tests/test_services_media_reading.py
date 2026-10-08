"""Tests for reading details, summaries, covers, content and source boundaries."""

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
from filelock import Timeout
from sanic import Sanic
from sanic.response import BaseHTTPResponse
from tortoise import Tortoise
from tortoise.exceptions import DoesNotExist

from app.core.config import KaloscopeConfig
from app.core.exceptions import ForbiddenException, NotFoundException, error_handler
from app.core.media import reader, shelver, watcher, writer
from app.core.media.common import ContentError
from app.core.media.coordination import library_lock
from app.core.media.handlers.reading import ReadingSource
from app.core.media.metadata import parse_comicinfo, parse_opf
from app.core.middleware import on_request, on_response
from app.models.media import (
    EpubContent,
    ImageContent,
    IndexState,
    LibType,
    MediaContentQuery,
    MediaEvent,
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
                    patcher.setattr(reader, "_find_external", ambiguous)
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
            reader = media_service._read_metadata

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

            monkeypatch.setattr(media_service, "_read_metadata", blocked)
            task = asyncio.create_task(MediaItemService.sync_metadata(item.id))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                with pytest.raises(Timeout):
                    async with await library_lock(item.lib.dir).acquire(timeout=0):
                        pass
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
            assert data["item_role"] == ("collection" if format is None else "book")
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

    monkeypatch.setattr(media_service, "_read_metadata", unexpected)

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
            read_metadata = reader.read_metadata

            def in_worker(source):
                """Assert that synchronous metadata I/O runs off the event loop.

                Args:
                    source: The validated reading source.

                Returns:
                    Current metadata from the original reader.
                """
                assert threading.get_ident() != main_thread
                return read_metadata(source)

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

            monkeypatch.setattr(reader, "read_metadata", in_worker)
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
def test_delete_reading_http(tmp_path, monkeypatch, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            directory = Path(item.dir)
            novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
            xml = directory / ("metadata.opf" if novel else "ComicInfo.xml")
            xml.write_bytes(_opf("Owned") if novel else b"<ComicInfo/>")
            (directory / "cover.png").write_bytes(_PNG)
            (directory / "notes.md").write_text("Keep me")
            monkeypatch.setattr(
                media_service, "delete_path", lambda path: path.unlink()
            )
            user = _user()
            await User.create(
                id=user.id, username=user.username, password="unused", role=user.role
            )
            async with _client(user) as client:
                response = await client.post(
                    "/_api/media/delete", json={"ids": [item.id], "local": True}
                )
                assert response.status_code == 204, response.text
                assert not await MediaItem.filter(id=item.id).exists()
                assert not xml.exists() and not (directory / "cover.png").exists()
                assert (directory / "notes.md").read_text() == "Keep me"
                assert (await client.get(f"/_api/media/{item.id}")).status_code == 404

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem", ["permission", "unauthenticated", "changed", "unavailable", "partial"]
)
def test_delete_reading_http_error(tmp_path, monkeypatch, problem):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.DIR)
            directory = Path(item.dir)
            metadata = directory / "ComicInfo.xml"
            metadata.write_text("<ComicInfo/>")
            user = _user([item.lib_id] if problem == "permission" else None)
            await User.create(
                id=user.id, username=user.username, password="unused", role=user.role
            )
            if problem == "changed":
                (directory / "New.cbz").write_bytes(b"new body")
            elif problem == "unavailable":
                directory.rename(directory.with_name("Offline"))
                directory.symlink_to(
                    directory.with_name("Offline"), target_is_directory=True
                )
            else:
                (directory / "2.png").write_bytes(_PNG)

            def fail(path):
                """Allow one body deletion before a controlled I/O failure.

                Args:
                    path: The current selected file.

                Raises:
                    PermissionError: When the second page is reached.
                """
                assert problem == "partial"
                if path.name == "2.png":
                    raise PermissionError("denied")
                path.unlink()

            monkeypatch.setattr(media_service, "delete_path", fail)
            async with _client(user) as client:
                if problem == "unauthenticated":
                    client.headers.clear()
                response = await client.post(
                    "/_api/media/delete", json={"ids": [item.id], "local": True}
                )
                status = {
                    "permission": 403,
                    "unauthenticated": 401,
                    "changed": 409,
                    "unavailable": 503,
                    "partial": 503,
                }[problem]
                assert response.status_code == status, response.text
                if problem in {"changed", "unavailable", "partial"}:
                    assert response.json()["message"] == (
                        "content_changed"
                        if problem == "changed"
                        else "media_source_unavailable"
                    )
                assert await MediaItem.filter(id=item.id).exists()
                assert metadata.exists()
                if problem != "partial":
                    assert (directory / "1.png").read_bytes() == _PNG

    asyncio.run(run())


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


@pytest.mark.parametrize(
    ("lib_type", "root"),
    [
        (LibType.MOVIE, "movie"),
        (LibType.TV_SHOW, "tvshow"),
        (LibType.TV_SHOW, "episodedetails"),
    ],
)
def test_video_details_http(tmp_path, lib_type, root):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            await MediaLib.filter(id=item.lib_id).update(lib_type=lib_type)
            path = Path(item.dir) / "video.nfo"
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
                assert "item_role" not in data
                assert data["metadata"]["title"] == "Video metadata"
                assert data["metadata"]["plot"] == "NFO plot"
                assert data["media_type"] == "video"
                assert response.headers["cache-control"] == "private, no-store"
                assert (await client.get(url + "/assets/cover")).status_code == 404

                # unchanged timestamps do not hide external NFO edits
                before = path.stat()
                path.write_text(
                    f"<{root}><title>Fresh metadata</title><plot>New plot</plot>"
                    f"<year>unknown</year><rating>NaN</rating></{root}>"
                )
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                response = await client.get(url)
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["metadata"]["title"] == "Fresh metadata"
                assert data["metadata"]["plot"] == "New plot"
                assert data["metadata"]["rating"] is None
                assert data["metadata"]["year"] is None

                # malformed NFO contributes no detail fields
                damaged = f"<{root}><title>Recovered</title><plot>Partial plot</plot>"
                path.write_text(damaged)
                response = await client.get(url)
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["metadata"] is None
                assert path.read_text() == damaged

                path.unlink()
                for nfo_path in (str(path), None):
                    await MediaItem.filter(id=item.id).update(nfo_path=nfo_path)
                    data = (await client.get(url)).json()["data"]
                    assert data["metadata"] is None
                    assert data["title"] == "Old database title"
                await item.refresh_from_db()
                assert item.title == "Old database title" and item.year == 1980
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
        ({"offset": "0"}, 400, "bad_request"),
        ({"limit": "20"}, 400, "bad_request"),
        ({"chapter_id": "item:1"}, 400, "bad_request"),
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
@pytest.mark.parametrize("mode", ["txt", "epub", "asset", "comic", "comic_asset"])
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
                _indexed_text(tmp_path)
                if mode == "txt"
                else _indexed_comic(tmp_path, MediaFormat.DIR)
                if mode.startswith("comic")
                else _indexed_epub(tmp_path)
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
                if mode in {"asset", "comic_asset"}:
                    await MediaItemService.get_asset(
                        item.id,
                        user,
                        hashlib.sha256(b"image:1.png").hexdigest()[:32]
                        if mode == "comic_asset"
                        else _epub_asset_id(),
                        str(item.index_version),
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


async def _indexed_comic(
    tmp_path: Path, format: MediaFormat, *, chapter: bool = False
) -> MediaItem:
    """Publish a comic with natural page order and a separate cover.

    Args:
        tmp_path: The isolated source and cache root.
        format: The directory, CBZ or ZIP source format.
        chapter: Whether to create a collection child; defaults to a standalone work.

    Returns:
        The ready source built through the normal indexing service.
    """
    item = await _item(tmp_path, None if chapter else format)
    if chapter:
        directory = Path(item.path) / "Chapter"
        directory.mkdir()
        item = await MediaItem.create(
            lib_id=item.lib_id,
            parent=item,
            path=str(
                directory if format == MediaFormat.DIR else directory / f"Book.{format}"
            ),
            dir=str(directory),
            name="Chapter",
            format=format,
        )
    images = {
        "10.png": _PNG + b"10",
        "2.png": _PNG + b"2",
        "1.png": _PNG + b"1",
        "cover.png": _PNG + b"cover",
    }
    if format == MediaFormat.DIR:
        for name, data in images.items():
            (Path(item.path) / name).write_bytes(data)
    else:
        with zipfile.ZipFile(item.path, "w") as archive:
            for name, data in images.items():
                archive.writestr(f"pages/{name}", data)
            archive.writestr(
                "ComicInfo.xml", "<ComicInfo><Title>Embedded</Title></ComicInfo>"
            )
    (Path(item.dir) / "ComicInfo.xml").write_text(
        "<ComicInfo><Title>Current</Title></ComicInfo>"
    )
    return await MediaItemService.index_content(item.id)


@pytest.mark.parametrize("format", [MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP])
@pytest.mark.parametrize("chapter", [False, True])
def test_comic_content_http(tmp_path, format, chapter):
    """Read comic pages, live titles and conditional images through the shared API.

    Args:
        tmp_path: The isolated source and cache root.
        format: The comic source format.
        chapter: Whether to access a child source directly.
    """

    async def run():
        async with _database():
            item = await _indexed_comic(tmp_path, format, chapter=chapter)
            before = await MediaItem.get(id=item.id).values()
            user = _user([item.lib_id])
            await User.create(
                id=user.id, username="Reader", password="unused", role=UserRole.USER
            )
            await UserPermission.create(
                user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=item.lib_id
            )
            async with _client(user) as client:
                url = f"/_api/media/{item.id}/content"
                response = await client.get(url, params={"limit": 2})
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data == {
                    "item_id": item.id,
                    "source_item_id": item.id,
                    "media_type": "image",
                    "format": format,
                    "content_type": "images",
                    "title": "Current",
                    "version": item.index_version,
                    "chapter_id": f"item:{item.id}",
                    "chapters": [
                        {
                            "id": f"item:{item.id}",
                            "title": "Current",
                            "part": 1,
                            "volume": None,
                        }
                    ],
                    "images": data["images"],
                    "image_count": 3,
                    "next_offset": 2,
                }
                assert len(data["images"]) == 2
                assert response.headers["cache-control"] == "private, no-store"
                assert (
                    str(tmp_path) not in response.text and "pages/" not in response.text
                )
                last = await client.get(
                    url,
                    params={
                        "chapter_id": data["chapter_id"],
                        "version": data["version"],
                        "offset": 2,
                    },
                )
                assert last.status_code == 200, last.text
                assert last.json()["data"]["next_offset"] is None
                pages = data["images"] + last.json()["data"]["images"]
                for page_url, number in zip(pages, (1, 2, 10), strict=True):
                    image = await client.get(page_url)
                    assert (
                        image.status_code == 200
                        and image.content == _PNG + str(number).encode()
                    )
                    assert image.headers["content-type"] == "image/png"
                    assert image.headers["x-content-type-options"] == "nosniff"
                    assert (
                        image.headers["cache-control"]
                        == "private, max-age=0, must-revalidate"
                    )
                etag = image.headers["etag"]
                cached = await client.get(
                    pages[-1], headers={"If-None-Match": f"W/{etag}"}
                )
                assert cached.status_code == 304 and cached.content == b""
                empty_page = (await client.get(url, params={"offset": 3})).json()[
                    "data"
                ]
                assert empty_page["images"] == [] and empty_page["next_offset"] is None
                (Path(item.dir) / "ComicInfo.xml").write_text(
                    "<ComicInfo><Title>Updated</Title></ComicInfo>"
                )
                updated = (await client.get(url)).json()["data"]
                assert (
                    updated["title"] == "Updated"
                    and updated["version"] == data["version"]
                )
                assert updated["images"] == pages
                (Path(item.dir) / "ComicInfo.xml").write_text("invalid metadata")
                fallback = (await client.get(url)).json()["data"]
                assert fallback["title"] == (
                    Path(item.path).name if format == MediaFormat.DIR else "Embedded"
                )
                if chapter:
                    await MediaItem.filter(id=item.parent_id).update(visible=False)
                    assert (await client.get(url)).status_code == 404
                    assert (
                        await client.get(pages[0], headers={"If-None-Match": "*"})
                    ).status_code == 404
                    await MediaItem.filter(id=item.parent_id).update(visible=True)
                for asset_id in ("f" * 32, "1.png"):
                    missing = await client.get(
                        f"/_api/media/{item.id}/assets/{asset_id}",
                        params={"v": data["version"]},
                        headers={"If-None-Match": "*"},
                    )
                    assert missing.status_code == 404 and "etag" not in missing.headers
                await UserPermission.all().delete()
                assert (await client.get(url)).status_code == 403
                assert (
                    await client.get(pages[-1], headers={"If-None-Match": etag})
                ).status_code == 403
                client.headers.clear()
                client.cookies.clear()
                assert (await client.get(pages[0])).status_code == 401
            assert await MediaItem.get(id=item.id).values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "params,status",
    [
        ({"offset": -1}, 400),
        ({"offset": 4}, 400),
        ({"offset": "invalid"}, 400),
        ({"limit": 0}, 400),
        ({"limit": 101}, 400),
        ({"limit": "1.5"}, 400),
        ({"chapter_id": "item:999"}, 400),
        ({"chapter_id": "f" * 32}, 400),
        ({"chapter_id": "item:0"}, 400),
        ({"version": "f" * 64}, 409),
    ],
)
def test_comic_query_http(tmp_path, params, status):
    """Reject invalid comic ranges, foreign chapters and stale versions.

    Args:
        tmp_path: The isolated source and cache root.
        params: The invalid content query.
        status: The expected HTTP status.
    """

    async def run():
        async with _database():
            item = await _indexed_comic(tmp_path, MediaFormat.DIR)
            async with _client(_user()) as client:
                response = await client.get(
                    f"/_api/media/{item.id}/content", params=params
                )
                assert response.status_code == status, response.text
                assert response.json()["message"] == (
                    "bad_request" if status == 400 else "content_changed"
                )

    asyncio.run(run())


def test_comic_page_limits(tmp_path):
    """Apply the default and maximum batch sizes to the indexed page list.

    Args:
        tmp_path: The isolated source and cache root.
    """

    async def run():
        async with _database():
            item = await _indexed_comic(tmp_path, MediaFormat.DIR)
            for number in range(3, 103):
                (Path(item.path) / f"{number}.png").write_bytes(_PNG)
            item = await MediaItemService.index_content(item.id)
            async with _client(_user()) as client:
                url = f"/_api/media/{item.id}/content"
                default = (await client.get(url)).json()["data"]
                assert len(default["images"]) == 20 and default["next_offset"] == 20
                following = (
                    await client.get(
                        url,
                        params={
                            "offset": default["next_offset"],
                            "version": default["version"],
                        },
                    )
                ).json()["data"]
                assert len(following["images"]) == 20 and following["next_offset"] == 40
                maximum = (await client.get(url, params={"limit": 100})).json()["data"]
                assert len(maximum["images"]) == 100 and maximum["next_offset"] == 100
                assert maximum["image_count"] == 102
                assert default["images"] + following["images"] == maximum["images"][:40]

    asyncio.run(run())


@pytest.mark.parametrize(
    "change,status,code",
    [
        ("added", 409, "content_changed"),
        ("removed", 409, "content_changed"),
        ("renamed", 409, "content_changed"),
        ("modified", 409, "content_changed"),
        ("page_link", 409, "content_changed"),
        ("source_missing", 503, "media_source_unavailable"),
        ("source_denied", 503, "media_source_unavailable"),
        ("cache_missing", 409, "content_not_ready"),
        ("cache_link", 409, "content_not_ready"),
        ("cache_version", 409, "content_not_ready"),
    ],
)
def test_comic_content_changes(tmp_path, monkeypatch, change, status, code):
    """Reject stale page lists and conditional images after source or cache changes.

    Args:
        tmp_path: The isolated source and cache root.
        monkeypatch: The fixture replacing unavailable file inspection.
        change: The source or cache mutation before reading.
        status: The expected HTTP status.
        code: The expected controlled error.
    """

    async def run():
        async with _database():
            item = await _indexed_comic(tmp_path, MediaFormat.DIR)
            directory = Path(item.path)
            page = directory / "1.png"
            cache = (
                tmp_path / "cache/media_index" / str(item.id) / str(item.index_version)
            )
            async with _client(_user()) as client:
                url = f"/_api/media/{item.id}/content"
                asset_url = (await client.get(url)).json()["data"]["images"][0]
                if change == "added":
                    (directory / "3.png").write_bytes(_PNG)
                elif change == "removed":
                    page.unlink()
                elif change == "renamed":
                    page.rename(directory / "0.png")
                elif change == "modified":
                    page.write_bytes(_PNG + b"replacement")
                elif change == "page_link":
                    target = tmp_path / "outside.png"
                    page.rename(target)
                    page.symlink_to(target)
                elif change == "source_missing":
                    directory.rename(directory.with_name("Moved"))
                elif change == "source_denied":
                    original = Path.stat

                    def denied(path, *args, **kwargs):
                        """Simulate a page whose attributes are unavailable.

                        Args:
                            path: The inspected path.
                            *args: Positional stat arguments.
                            **kwargs: Keyword stat arguments.

                        Returns:
                            Attributes of accessible paths.

                        Raises:
                            PermissionError: For the selected source page.
                        """
                        if path == page:
                            raise PermissionError("unavailable page")
                        return original(path, *args, **kwargs)

                    monkeypatch.setattr(Path, "stat", denied)
                elif change == "cache_missing":
                    (cache / "index.json").unlink()
                elif change == "cache_link":
                    target = tmp_path / "outside-index.json"
                    (cache / "index.json").rename(target)
                    (cache / "index.json").symlink_to(target)
                else:
                    path = cache / "index.json"
                    path.write_text(
                        path.read_text().replace(str(item.index_version), "f" * 64)
                    )
                for target in (url, asset_url):
                    response = await client.get(target, headers={"If-None-Match": "*"})
                    assert response.status_code == status, response.text
                    assert response.json()["message"] == code
                    assert response.headers["cache-control"] == "private, no-store"
                    assert "etag" not in response.headers

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.CBZ, MediaFormat.ZIP])
def test_comic_asset_crc(tmp_path, format):
    """Revalidate archive members before allowing a conditional image response.

    Args:
        tmp_path: The isolated source and cache root.
        format: The supported comic archive format.
    """

    async def run():
        async with _database():
            item = await _indexed_comic(tmp_path, format)
            async with _client(_user()) as client:
                content = await client.get(f"/_api/media/{item.id}/content")
                url = content.json()["data"]["images"][0]
                etag = (await client.get(url)).headers["etag"]
                path = Path(item.path)
                before = path.stat()
                with zipfile.ZipFile(path) as archive:
                    entries = [
                        (entry, archive.read(entry)) for entry in archive.infolist()
                    ]
                with zipfile.ZipFile(path, "w") as archive:
                    for entry, data in entries:
                        if entry.filename == "pages/1.png":
                            data = data.replace(b"image", b"other")
                        archive.writestr(entry, data)
                assert path.stat().st_size == before.st_size
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                response = await client.get(url, headers={"If-None-Match": etag})
                assert response.status_code == 409, response.text
                assert response.json()["message"] == "content_changed"
                assert "etag" not in response.headers

    asyncio.run(run())


@pytest.mark.parametrize("asset", [False, True])
@pytest.mark.parametrize("change", ["page", "cache", "sibling"])
def test_comic_read_race(tmp_path, monkeypatch, asset, change):
    """Guard unlocked comic reads against page and cache replacement.

    Args:
        tmp_path: The isolated source and cache root.
        monkeypatch: The fixture wrapping the actual worker read.
        asset: Whether to read image bytes instead of the page list.
        change: The file changed before the read's stability check.
    """
    from app.core.media import image as image_media
    from app.core.media import reader

    async def run():
        async with _database():
            item = await _indexed_comic(tmp_path, MediaFormat.DIR)
            cache = (
                tmp_path / "cache/media_index" / str(item.id) / str(item.index_version)
            )
            module = image_media if asset else reader
            name = "read_image_resource" if asset else "read_metadata"
            read = getattr(module, name)
            loop_thread = threading.get_ident()

            def changed(*args):
                """Read actual data and simulate an external writer before returning.

                Args:
                    *args: The arguments to the original reader.

                Returns:
                    The original result before the source or cache is mutated.
                """
                assert threading.get_ident() != loop_thread
                result = read(*args)
                if change == "sibling":
                    (cache.parent / "unrelated.tmp").write_text("unrelated")
                else:
                    # changing an unselected page must also invalidate this version
                    path = (
                        Path(item.path) / "2.png"
                        if change == "page"
                        else cache / "index.json"
                    )
                    before = path.stat()
                    path.write_bytes(
                        path.read_bytes().replace(b"image", b"other")
                        if change == "page"
                        else path.read_bytes() + b" "
                    )
                    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
                return result

            monkeypatch.setattr(module, name, changed)
            async with library_lock(item.lib.dir):
                request = (
                    MediaItemService.get_asset(
                        item.id,
                        _user(),
                        hashlib.sha256(b"image:1.png").hexdigest()[:32],
                        str(item.index_version),
                    )
                    if asset
                    else MediaItemService.get_content(
                        item.id, _user(), MediaContentQuery()
                    )
                )
                if change == "sibling":
                    assert await asyncio.wait_for(request, timeout=3)
                else:
                    with pytest.raises(ContentError, match="content_changed"):
                        await asyncio.wait_for(request, timeout=3)

    asyncio.run(run())


async def _indexed_collection(tmp_path: Path) -> tuple[MediaItem, dict[int, MediaItem]]:
    """Publish mixed comic chapters in an order different from their directory names.

    Args:
        tmp_path: The isolated source and cache root.

    Returns:
        The collection without an aggregate index and its ready chapters by number.
    """
    parent = await _item(tmp_path, None)
    chapters = {}
    for number, format in (
        (10, MediaFormat.ZIP),
        (2, MediaFormat.CBZ),
        (1, MediaFormat.DIR),
    ):
        directory = Path(parent.path) / f"Chapter {number}"
        directory.mkdir()
        path = directory if format == MediaFormat.DIR else directory / f"Book.{format}"
        if format == MediaFormat.DIR:
            for page in (1, 2):
                (directory / f"{page}.png").write_bytes(
                    _PNG + f"{number}:{page}".encode()
                )
        else:
            with zipfile.ZipFile(path, "w") as archive:
                for page in (1, 2):
                    archive.writestr(f"{page}.png", _PNG + f"{number}:{page}".encode())
        (directory / "ComicInfo.xml").write_text(
            f"<ComicInfo><Title>Live {number}</Title></ComicInfo>"
        )
        chapter = await MediaItem.create(
            lib_id=parent.lib_id,
            parent=parent,
            path=str(path),
            dir=str(directory),
            name="Book",
            title=f"Summary {number}" if number != 2 else None,
            format=format,
        )
        chapters[number] = await MediaItemService.index_content(chapter.id)
    return parent, chapters


def test_collection_content_http(tmp_path, monkeypatch):
    """Select naturally ordered chapters and retain their versions through pagination.

    Args:
        tmp_path: The isolated source and cache root.
        monkeypatch: The fixture observing live metadata reads.
    """

    async def run():
        async with _database():
            parent, chapters = await _indexed_collection(tmp_path)
            before = await MediaItem.all().order_by("id").values()
            assert (
                parent.index_version is None
                and parent.index_state == IndexState.PENDING
            )
            read_metadata = Mock(wraps=reader.read_metadata)
            monkeypatch.setattr(reader, "read_metadata", read_metadata)
            async with _client(_user()) as client:
                url = f"/_api/media/{parent.id}/content"
                response = await client.get(url, params={"limit": 1})
                assert response.status_code == 200, response.text
                content = ImageContent.model_validate(response.json()["data"])
                assert (
                    content.item_id == parent.id
                    and content.source_item_id == chapters[1].id
                )
                assert content.version == chapters[1].index_version
                assert content.title == "Live 1" and content.next_offset == 1
                assert [entry.id for entry in content.chapters] == [
                    f"item:{chapters[number].id}" for number in (1, 2, 10)
                ]
                assert [entry.title for entry in content.chapters] == [
                    "Live 1",
                    "Chapter 2",
                    "Summary 10",
                ]
                assert read_metadata.call_count == 1 and read_metadata.call_args.args[
                    0
                ].path == Path(chapters[1].path)
                assert response.headers["cache-control"] == "private, no-store"
                assert str(tmp_path) not in response.text
                assert (await client.get(content.images[0])).content == _PNG + b"1:1"
                last = await client.get(
                    url,
                    params={
                        "chapter_id": content.chapter_id,
                        "version": content.version,
                        "offset": 1,
                    },
                )
                assert (
                    last.status_code == 200
                    and last.json()["data"]["next_offset"] is None
                )
                assert (
                    await client.get(last.json()["data"]["images"][0])
                ).content == _PNG + b"1:2"
                for number in (2, 10):
                    chapter = chapters[number]
                    (Path(chapter.dir) / "ComicInfo.xml").write_text(
                        f"<ComicInfo><Title>Updated {number}</Title></ComicInfo>"
                    )
                    selected = await client.get(
                        url,
                        params={
                            "chapter_id": f"item:{chapter.id}",
                            "version": str(chapter.index_version),
                        },
                    )
                    assert selected.status_code == 200, selected.text
                    data = selected.json()["data"]
                    assert (
                        data["item_id"] == parent.id
                        and data["source_item_id"] == chapter.id
                    )
                    assert (
                        data["version"] == chapter.index_version
                        and data["format"] == chapter.format
                    )
                    assert data["title"] == f"Updated {number}"
                    assert (
                        await client.get(data["images"][0])
                    ).content == _PNG + f"{number}:1".encode()
                stale = await client.get(
                    url,
                    params={
                        "chapter_id": f"item:{chapters[2].id}",
                        "version": content.version,
                    },
                )
                assert (
                    stale.status_code == 409
                    and stale.json()["message"] == "content_changed"
                )
                parent_asset = content.images[0].replace(
                    f"/media/{chapters[1].id}/", f"/media/{parent.id}/"
                )
                assert (await client.get(parent_asset)).status_code == 404
            assert await MediaItem.all().order_by("id").values() == before

    asyncio.run(run())


@pytest.mark.parametrize(
    "states,selected,code",
    [
        (("pending", "ready", "ready"), 2, None),
        (("empty", "empty", "ready"), 10, None),
        (("error", "ready", "pending"), 2, None),
        (("empty", "empty", "empty"), None, "empty_content"),
        (("pending", "pending", "pending"), None, "content_not_ready"),
        (("empty", "pending", "empty"), None, "content_not_ready"),
        (("error", "error", "error"), None, "content_not_ready"),
        ((None, None, None), None, "content_not_ready"),
    ],
)
def test_collection_selection(tmp_path, states, selected, code):
    """Select only published ready chapters and report unavailable selections.

    Args:
        tmp_path: The isolated source and cache root.
        states: Chapter states in natural directory order.
        selected: The expected automatic chapter number, or None when unavailable.
        code: The expected error when no chapter is ready.
    """

    async def run():
        async with _database():
            parent, chapters = await _indexed_collection(tmp_path)
            for number, state in zip((1, 2, 10), states, strict=True):
                await MediaItem.filter(id=chapters[number].id).update(index_state=state)
            async with _client(_user()) as client:
                url = f"/_api/media/{parent.id}/content"
                response = await client.get(url)
                if selected is None:
                    assert response.status_code == (
                        422 if code == "empty_content" else 409
                    )
                    assert response.json()["message"] == code
                else:
                    assert response.status_code == 200, response.text
                    data = response.json()["data"]
                    assert data["source_item_id"] == chapters[selected].id
                    assert [chapter["id"] for chapter in data["chapters"]] == [
                        f"item:{chapters[number].id}"
                        for number, state in zip((1, 2, 10), states, strict=True)
                        if state == "ready"
                    ]
                explicit = await client.get(
                    url, params={"chapter_id": f"item:{chapters[1].id}"}
                )
                assert explicit.status_code == (422 if states[0] == "empty" else 409)
                assert explicit.json()["message"] == (
                    "empty_content" if states[0] == "empty" else "content_not_ready"
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "scope", ["hidden", "foreign_library", "other_parent", "nested", "invalid_format"]
)
def test_collection_scope(tmp_path, scope):
    """Exclude chapters outside the collection's visible direct comic sources.

    Args:
        tmp_path: The isolated source and cache root.
        scope: The ownership or visibility change excluding the first chapter.
    """

    async def run():
        async with _database():
            parent, chapters = await _indexed_collection(tmp_path)
            if scope == "foreign_library":
                foreign = await MediaLib.create(
                    name="Foreign",
                    dir=str(tmp_path / "Foreign"),
                    lib_type=LibType.COMIC,
                    priority=2,
                )
                fields = {"lib_id": foreign.id}
            elif scope == "other_parent":
                other = await MediaItem.create(
                    lib_id=parent.lib_id,
                    path=str(tmp_path / "Library/Other"),
                    dir=str(tmp_path / "Library/Other"),
                    name="Other",
                )
                fields = {"parent_id": other.id}
            else:
                fields = {
                    "hidden": {"visible": False},
                    "nested": {"parent_id": chapters[2].id},
                    "invalid_format": {"format": MediaFormat.TXT},
                }[scope]
            await MediaItem.filter(id=chapters[1].id).update(**fields)
            async with _client(_user()) as client:
                url = f"/_api/media/{parent.id}/content"
                response = await client.get(url)
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["source_item_id"] == chapters[2].id
                assert [entry["id"] for entry in data["chapters"]] == [
                    f"item:{chapters[number].id}" for number in (2, 10)
                ]
                response = await client.get(
                    url, params={"chapter_id": f"item:{chapters[1].id}"}
                )
                assert (
                    response.status_code == 400
                    and response.json()["message"] == "bad_request"
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("empty", 422, "empty_content"),
        ("unknown", 400, "bad_request"),
        ("novel_chapter", 400, "bad_request"),
        ("parent_chapter", 400, "bad_request"),
        ("huge_id", 400, "bad_request"),
        ("cache", 409, "content_not_ready"),
        ("hidden", 404, "not_found"),
        ("outside", 503, "media_source_unavailable"),
    ],
)
def test_collection_errors(tmp_path, case, status, code):
    """Reject invalid collection requests without selecting unrelated sources.

    Args:
        tmp_path: The isolated source and cache root.
        case: The invalid collection state or chapter query.
        status: The expected HTTP status.
        code: The expected controlled error.
    """

    async def run():
        async with _database():
            if case == "empty":
                parent = await _item(tmp_path, None)
            else:
                parent, chapters = await _indexed_collection(tmp_path)
                if case == "cache":
                    chapter = chapters[1]
                    (
                        tmp_path
                        / "cache/media_index"
                        / str(chapter.id)
                        / str(chapter.index_version)
                        / "index.json"
                    ).unlink()
                elif case == "hidden":
                    await MediaItem.filter(id=parent.id).update(visible=False)
                elif case == "outside":
                    await MediaItem.filter(id=parent.id).update(
                        path=str(tmp_path / "Outside")
                    )
            selections = {
                "unknown": "item:9999",
                "novel_chapter": "f" * 32,
                "parent_chapter": f"item:{parent.id}",
                "huge_id": "item:" + "9" * 200,
            }
            params = {"chapter_id": selections[case]} if case in selections else {}
            async with _client(_user()) as client:
                response = await client.get(
                    f"/_api/media/{parent.id}/content", params=params
                )
                assert response.status_code == status, response.text
                assert response.json()["message"] == code
                assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(run())


@pytest.mark.parametrize(
    "change,status",
    [
        ("parent_hidden", 404),
        ("parent_deleted", 404),
        ("parent_path", 409),
        ("parent_format", 409),
        ("parent_state", 200),
        ("selected_hidden", 404),
        ("selected_version", 409),
        ("selected_pending", 409),
        ("sibling_hidden", 409),
        ("sibling_title", 409),
        ("sibling_added", 409),
        ("permission", 403),
    ],
)
def test_collection_read_race(tmp_path, monkeypatch, change, status):
    """Recheck collection access and directory membership after an unlocked read.

    Args:
        tmp_path: The isolated source and cache root.
        monkeypatch: The fixture wrapping worker dispatch.
        change: The database mutation after the selected chapter is read.
        status: The expected HTTP status after revalidation.
    """

    async def run():
        async with _database():
            parent, chapters = await _indexed_collection(tmp_path)
            user = _user([parent.lib_id])
            await User.create(
                id=user.id, username="Reader", password="unused", role=UserRole.USER
            )
            await UserPermission.create(
                user_id=user.id, rel_type=PermType.MEDIA_LIB, rel_id=parent.lib_id
            )
            dispatch = asyncio.to_thread

            async def changed(func, *args):
                """Mutate the database between actual file reading and revalidation.

                Args:
                    func: The synchronous chapter reader.
                    *args: Its original arguments.

                Returns:
                    The original content before database revalidation.
                """
                result = await dispatch(func, *args)
                if change == "permission":
                    await UserPermission.all().delete()
                elif change == "parent_deleted":
                    await parent.delete()
                elif change == "sibling_added":
                    await MediaItem.create(
                        lib_id=parent.lib_id,
                        parent=parent,
                        path=str(Path(parent.path) / "New"),
                        dir=str(Path(parent.path) / "New"),
                        name="New",
                        format=MediaFormat.DIR,
                        index_state=IndexState.READY,
                        index_version="d" * 64,
                    )
                else:
                    changes = {
                        "parent_hidden": (parent.id, {"visible": False}),
                        "parent_path": (
                            parent.id,
                            {"path": str(Path(parent.path).with_name("Moved"))},
                        ),
                        "parent_format": (parent.id, {"format": MediaFormat.DIR}),
                        "parent_state": (parent.id, {"index_state": IndexState.EMPTY}),
                        "selected_hidden": (chapters[1].id, {"visible": False}),
                        "selected_version": (
                            chapters[1].id,
                            {"index_version": "f" * 64},
                        ),
                        "selected_pending": (
                            chapters[1].id,
                            {"index_state": IndexState.PENDING},
                        ),
                        "sibling_hidden": (chapters[2].id, {"visible": False}),
                        "sibling_title": (chapters[2].id, {"title": "Updated"}),
                    }
                    id, fields = changes[change]
                    await MediaItem.filter(id=id).update(**fields)
                return result

            monkeypatch.setattr(media_service, "to_thread", changed)
            async with _client(user) as client, library_lock(parent.lib.dir):
                response = await asyncio.wait_for(
                    client.get(f"/_api/media/{parent.id}/content"), timeout=3
                )
                assert response.status_code == status, response.text
                assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(run())


def test_collection_reparented(tmp_path, monkeypatch):
    """Reject a chapter moved after selection before attempting any content read.

    Args:
        tmp_path: The isolated source and cache root.
        monkeypatch: The fixture moving a chapter just before access is loaded.
    """

    async def run():
        async with _database():
            parent, chapters = await _indexed_collection(tmp_path)
            other = await MediaItem.create(
                lib_id=parent.lib_id,
                path=str(tmp_path / "Library/Other"),
                dir=str(tmp_path / "Library/Other"),
                name="Other",
            )
            accessible = MediaItemService.get_accessible

            async def moved(cls, id, user):
                """Move the selected chapter before loading its current ownership.

                Args:
                    id: The item being authorized.
                    user: The current user.

                Returns:
                    The currently accessible item after the move.
                """
                if id == chapters[1].id:
                    await MediaItem.filter(id=id).update(parent_id=other.id)
                return await accessible(id, user)

            reader = Mock(side_effect=AssertionError("unexpected content read"))
            monkeypatch.setattr(MediaItemService, "get_accessible", classmethod(moved))
            monkeypatch.setattr(media_service, "_read_image_content", reader)
            async with _client(_user()) as client:
                response = await client.get(f"/_api/media/{parent.id}/content")
                assert response.status_code == 409, response.text
                assert response.json()["message"] == "content_changed"
            reader.assert_not_called()

    asyncio.run(run())


def test_collection_versions(tmp_path):
    """Exclude malformed unpublished versions from automatic chapter selection.

    Args:
        tmp_path: The isolated source and cache root.
    """

    async def run():
        async with _database():
            parent, chapters = await _indexed_collection(tmp_path)
            await MediaItem.filter(id=chapters[1].id).update(index_version="invalid")
            async with _client(_user()) as client:
                url = f"/_api/media/{parent.id}/content"
                response = await client.get(url)
                assert response.status_code == 200, response.text
                assert response.json()["data"]["source_item_id"] == chapters[2].id
                response = await client.get(
                    url, params={"chapter_id": f"item:{chapters[1].id}"}
                )
                assert response.status_code == 409
                assert response.json()["message"] == "content_not_ready"

    asyncio.run(run())


def test_collection_content_limit(tmp_path):
    """Include the complete collection directory in the content response limit.

    Args:
        tmp_path: The isolated source and cache root.
    """

    async def run():
        async with _database():
            parent, chapters = await _indexed_collection(tmp_path)
            await MediaItem.bulk_create(
                [
                    MediaItem(
                        lib_id=parent.lib_id,
                        parent_id=parent.id,
                        path=str(Path(parent.path) / f"Extra {number}"),
                        dir=str(Path(parent.path) / f"Extra {number}"),
                        name=f"Extra {number}",
                        title="长" * 255,
                        format=MediaFormat.DIR,
                        index_state=IndexState.READY,
                        index_version="a" * 64,
                    )
                    for number in range(1500)
                ]
            )
            async with _client(_user()) as client:
                response = await client.get(
                    f"/_api/media/{parent.id}/content",
                    params={"chapter_id": f"item:{chapters[1].id}"},
                )
                assert response.status_code == 422, response.text
                assert response.json()["message"] == "media_limit_exceeded"

    asyncio.run(run())


@pytest.mark.parametrize("format", [None, *MediaFormat])
def test_save_metadata(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            directory = Path(item.dir)
            novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
            xml = directory / ("metadata.opf" if novel else "ComicInfo.xml")
            xml.write_bytes(
                _opf("Old", "<dc:publisher>Remove me</dc:publisher>")
                if novel
                else b"<ComicInfo><Publisher>Remove me</Publisher></ComicInfo>"
            )
            (directory / "cover.png").write_bytes(_PNG)
            (directory / "notes.md").write_text("Unrelated")
            before = {
                path: path.read_bytes()
                for path in directory.iterdir()
                if path.is_file() and path != xml
            }
            await MediaItem.filter(id=item.id).update(
                visible=False,
                index_state=IndexState.READY,
                index_version="a" * 64,
                extra={"content": {"chapter_count": 2}},
            )
            assert await MediaItemService.save_metadata(
                item.id,
                {
                    "title": "New & <title>",
                    "plot": "New plot",
                    "authors": ["Author"],
                    "rating": 0,
                },
                overwrite=True,
            )
            parsed = (parse_opf if novel else parse_comicinfo)(xml.read_bytes()).data
            assert parsed.title == "New & <title>" and parsed.authors == ("Author",)
            assert (
                parsed.publisher is None and parsed.year is None and parsed.rating == 0
            )
            assert all(path.read_bytes() == data for path, data in before.items())
            assert not list(directory.glob(".metadata-*.tmp"))
            current = await MediaItem.get(id=item.id)
            assert (
                current.title == parsed.title
                and current.year is None
                and current.rating == 0
            )
            assert current.visible is False and current.index_version == "a" * 64
            assert current.index_state == IndexState.READY and current.nfo_path is None
            assert current.extra is not None and set(current.extra) == {
                "content",
                "schema_version",
                "metadata_sync",
            }
            assert current.extra["metadata_sync"]["state"] == "ready"
            assert not await MediaEvent.all().exists()
            await MediaItem.filter(id=item.id).update(visible=True)
            details = await MediaItemService.get_details(item.id, _user())
            assert (
                details["title"] == "New & <title>"
                and details["metadata"]["plot"] == "New plot"
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "name", ["Book.opf", "content.opf", "metadata.opf", "Metadata.OPF"]
)
def test_save_opf_priority(tmp_path, name):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            directory = Path(item.dir)
            target = directory / name
            target.write_bytes(
                _opf("External edit", "<dc:publisher>Removed</dc:publisher>")
            )
            target.chmod(0o640)
            if name.casefold() != "metadata.opf":
                (directory / "metadata.opf").write_bytes(_opf("Lower priority"))
            (directory / "other.opf").write_bytes(_opf("Unrelated"))
            assert await MediaItemService.save_metadata(
                item.id, {"title": "Confirmed"}, overwrite=True
            )
            assert (directory / "other.opf").read_bytes() == _opf("Unrelated")
            if name.casefold() != "metadata.opf":
                assert not target.exists()
            else:
                assert target.stat().st_mode & 0o777 == 0o640
            details = await MediaItemService.get_details(item.id, _user())
            assert (
                details["title"] == "Confirmed"
                and details["metadata"]["publisher"] is None
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "format",
    [
        MediaFormat.TXT,
        MediaFormat.DIR,
        MediaFormat.EPUB,
        MediaFormat.CBZ,
        MediaFormat.ZIP,
    ],
)
def test_auto_metadata_existing(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            directory = Path(item.dir)
            if format == MediaFormat.TXT:
                (directory / "content.opf").write_bytes(_opf("Local"))
            elif format == MediaFormat.DIR:
                (directory / "ComicInfo.xml").write_text(
                    "<ComicInfo><Title>Local</Title></ComicInfo>"
                )
            before = {
                path: path.read_bytes()
                for path in directory.iterdir()
                if path.is_file()
            }
            assert not await MediaItemService.save_metadata(
                item.id, {"title": "Automatic"}
            )
            assert before == {
                path: path.read_bytes()
                for path in directory.iterdir()
                if path.is_file()
            }
            current = await MediaItem.get(id=item.id)
            assert current.title == (
                "Local" if format in (MediaFormat.TXT, MediaFormat.DIR) else "Embedded"
            )
            assert not await MediaEvent.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "format", [None, MediaFormat.TXT, MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP]
)
def test_auto_metadata_create(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            if format in (MediaFormat.CBZ, MediaFormat.ZIP):
                with zipfile.ZipFile(item.path, "w") as archive:
                    archive.writestr("1.png", _PNG)
            assert await MediaItemService.save_metadata(item.id, {"title": "Automatic"})
            assert not await MediaEvent.all().exists()
            assert (await MediaItem.get(id=item.id)).title == "Automatic"

    asyncio.run(run())


def test_save_chapter_metadata(tmp_path):
    async def run():
        async with _database():
            parent = await _item(tmp_path, None)
            parent_xml = Path(parent.dir) / "ComicInfo.xml"
            parent_xml.write_text(
                "<ComicInfo><Title>Series</Title>"
                "<Writer>Parent author</Writer></ComicInfo>"
            )
            directory = Path(parent.dir) / "Chapter"
            directory.mkdir()
            (directory / "1.png").write_bytes(_PNG)
            chapter = await MediaItem.create(
                lib_id=parent.lib_id,
                parent_id=parent.id,
                path=str(directory),
                dir=str(directory),
                name="Chapter",
                format=MediaFormat.DIR,
            )
            before = parent_xml.read_bytes()
            assert await MediaItemService.save_metadata(
                chapter.id, {"title": "Chapter title"}
            )
            xml = directory / "ComicInfo.xml"
            assert not parse_comicinfo(xml.read_bytes()).data.authors
            assert parent_xml.read_bytes() == before
            details = await MediaItemService.get_details(chapter.id, _user())
            assert details["item_role"] == "chapter"
            assert details["metadata"]["authors"] == ("Parent author",)
            assert (await MediaItem.get(id=parent.id)).title == "Old database title"

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["validation", "publish"])
def test_auto_metadata_arrives(tmp_path, monkeypatch, stage):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            xml = Path(item.dir) / "metadata.opf"
            read = writer._read_local
            rename = writer.rename_exclusive

            def read_local(path, parser):
                if stage == "validation" and path.suffix == ".tmp":
                    xml.write_bytes(_opf("External"))
                return read(path, parser)

            def publish(src, dest):
                xml.write_bytes(_opf("External"))
                return rename(src, dest)

            monkeypatch.setattr(writer, "_read_local", read_local)
            if stage == "publish":
                monkeypatch.setattr(writer, "rename_exclusive", publish)
            assert not await MediaItemService.save_metadata(
                item.id, {"title": "Automatic"}
            )
            assert xml.read_bytes() == _opf("External")
            assert (await MediaItem.get(id=item.id)).title == "External"
            assert not await MediaEvent.all().exists()
            assert not list(Path(item.dir).glob(".metadata-*.tmp"))

    asyncio.run(run())


@pytest.mark.parametrize(
    "candidate",
    [
        {},
        {"title": "   "},
        {"title": "New", "authors": "Bad"},
        {"title": "New", "output_path": "/tmp/metadata.opf"},
        {"title": "New", "overwrite": True},
        {"title": "New", "volume": "4"},
        {"title": "New", "rating": 11},
        {"title": "New", "year": "2026"},
    ],
)
def test_save_metadata_invalid(tmp_path, candidate):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            xml = Path(item.dir) / "metadata.opf"
            xml.write_bytes(_opf("Original"))
            with pytest.raises(ContentError, match="invalid_metadata"):
                await MediaItemService.save_metadata(item.id, candidate, overwrite=True)
            assert xml.read_bytes() == _opf("Original")
            assert not await MediaEvent.all().exists()
            assert (await MediaItem.get(id=item.id)).title == "Old database title"

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem", ["missing", "body_link", "xml_link", "xml_directory", "ambiguous"]
)
def test_save_metadata_source(tmp_path, problem):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            directory = Path(item.dir)
            xml = directory / "metadata.opf"
            outside = tmp_path / "Outside"
            outside.write_bytes(_opf("Protected"))
            if problem == "missing":
                Path(item.path).unlink()
            elif problem == "body_link":
                Path(item.path).unlink()
                Path(item.path).symlink_to(outside)
            elif problem == "xml_link":
                xml.symlink_to(outside)
            elif problem == "xml_directory":
                xml.mkdir()
            else:
                (directory / "Other.txt").write_text("Ambiguous body")
            with pytest.raises(ContentError):
                await MediaItemService.save_metadata(
                    item.id, {"title": "New"}, overwrite=True
                )
            assert outside.read_bytes() == _opf("Protected")
            assert (await MediaItem.get(id=item.id)).title == "Old database title"
            event = await MediaEvent.get()
            assert event.payload is not None and event.payload["error_code"]
            assert not await watcher.consume_event(event)
            assert not list(directory.glob(".metadata-*.tmp"))

    asyncio.run(run())


@pytest.mark.parametrize(
    "href",
    [
        "cover.png",
        "images/cover.png",
        "../cover.png",
        "https://example.com/cover.png",
        "missing.png",
        "linked.png",
    ],
)
def test_save_metadata_cover(tmp_path, href):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            directory = Path(item.dir)
            (directory / "cover.png").write_bytes(_PNG)
            (directory / "images").mkdir()
            (directory / "images/cover.png").write_bytes(_PNG)
            (directory / "linked.png").symlink_to(directory / "cover.png")
            values = {"title": "New", "cover": {"href": href}}
            if href in ("cover.png", "images/cover.png"):
                assert await MediaItemService.save_metadata(
                    item.id, values, overwrite=True
                )
                cover = await MediaItemService.get_cover(item.id, _user())
                assert cover is not None
                assert cover.data == _PNG and cover.mime_type == "image/png"
            else:
                with pytest.raises(ContentError):
                    await MediaItemService.save_metadata(
                        item.id, values, overwrite=True
                    )
                assert not (directory / "metadata.opf").exists()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["replace", "retire"])
def test_save_metadata_retry(tmp_path, monkeypatch, stage):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            directory = Path(item.dir)
            xml = directory / "metadata.opf"
            xml.write_bytes(_opf("Original"))
            old = directory / "Book.opf"
            old.write_bytes(_opf("Original"))
            unlink = Path.unlink

            def denied_replace(*args):
                raise PermissionError("read only")

            def denied_unlink(path, *args, **kwargs):
                if path == old:
                    raise PermissionError("read only")
                return unlink(path, *args, **kwargs)

            with monkeypatch.context() as patch:
                if stage == "replace":
                    patch.setattr(writer.os, "replace", denied_replace)
                else:
                    patch.setattr(Path, "unlink", denied_unlink)
                with pytest.raises(ContentError, match="metadata_write_failed"):
                    await MediaItemService.save_metadata(
                        item.id, {"title": "New"}, overwrite=True
                    )
            assert old.read_bytes() == _opf("Original")
            assert parse_opf(xml.read_bytes()).data.title == (
                "Original" if stage == "replace" else "New"
            )
            assert (await MediaItem.get(id=item.id)).title == "Old database title"
            event = await MediaEvent.get()
            assert (
                event.payload is not None
                and event.payload["error_code"] == "metadata_write_failed"
            )
            assert not await watcher.consume_event(event)
            assert not list(directory.glob(".metadata-*.tmp"))
            assert await MediaItemService.save_metadata(
                item.id, {"title": "Retry"}, overwrite=True
            )
            assert (await MediaItem.get(id=item.id)).title == "Retry"
            assert not old.exists() and not await MediaEvent.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize("published", [False, True])
def test_save_metadata_recovery(tmp_path, monkeypatch, published):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            xml = Path(item.dir) / "metadata.opf"
            if published:

                async def fail(*args, **kwargs):
                    raise RuntimeError("database interrupted")

                with monkeypatch.context() as patch:
                    patch.setattr(media_service, "_save_summary", fail)
                    with pytest.raises(RuntimeError, match="interrupted"):
                        await MediaItemService.save_metadata(
                            item.id, {"title": "Candidate"}, overwrite=True
                        )
                event = await MediaEvent.get()
                assert event.payload is not None and event.payload["published"] is True
                xml.write_bytes(_opf("External after publication"))
                expected = "External after publication"
            else:

                async def interrupted(*args, **kwargs):
                    raise asyncio.CancelledError

                with monkeypatch.context() as patch:
                    patch.setattr(media_service, "write_in_thread", interrupted)
                    with pytest.raises(asyncio.CancelledError):
                        await MediaItemService.save_metadata(
                            item.id, {"title": "Candidate"}, overwrite=True
                        )
                event = await MediaEvent.get()
                assert event.payload is not None and event.payload["published"] is False
                xml.write_bytes(_opf("External before publication"))
                expected = "Candidate"
            assert await watcher.consume_event(event)
            assert parse_opf(xml.read_bytes()).data.title == expected
            assert (await MediaItem.get(id=item.id)).title == expected
            assert not await MediaEvent.all().exists()

    asyncio.run(run())


def test_auto_metadata_recovery(tmp_path, monkeypatch):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)

            async def interrupted(*args, **kwargs):
                raise asyncio.CancelledError

            with monkeypatch.context() as patch:
                patch.setattr(media_service, "write_in_thread", interrupted)
                with pytest.raises(asyncio.CancelledError):
                    await MediaItemService.save_metadata(
                        item.id, {"title": "Automatic"}
                    )
            event = await MediaEvent.get()
            xml = Path(item.dir) / "content.opf"
            xml.write_bytes(_opf("Arrived during shutdown"))
            assert await watcher.consume_event(event)
            assert xml.read_bytes() == _opf("Arrived during shutdown")
            assert not (Path(item.dir) / "metadata.opf").exists()
            assert (await MediaItem.get(id=item.id)).title == "Arrived during shutdown"
            assert not await MediaEvent.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize("state", [None, *IndexState])
def test_metadata_without_index(tmp_path, state):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            version = None if state is None else "0" * 64
            await MediaItem.filter(id=item.id).update(
                index_state=state, index_version=version
            )
            assert not (tmp_path / "cache" / "media_index").exists()
            assert await MediaItemService.save_metadata(item.id, {"title": "Automatic"})
            current = await MediaItem.get(id=item.id)
            assert current.title == "Automatic"
            assert current.index_state == state and current.index_version == version
            metadata = parse_opf((Path(item.dir) / "metadata.opf").read_bytes())
            assert metadata.data.title == "Automatic"
            assert not (tmp_path / "cache" / "media_index").exists()
            assert not await MediaEvent.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize("format", [None, *MediaFormat])
def test_save_metadata_http(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            user = _user()
            await User.create(
                id=user.id, username=user.username, password="unused", role=user.role
            )
            async with _client(user) as client:
                response = await client.post(
                    f"/_api/media/{item.id}/metadata",
                    json={
                        "graph_id": 1,
                        "metadata": {"title": "Confirmed", "authors": ["Writer"]},
                    },
                )
                assert response.status_code == 204, response.text
                assert response.headers["Cache-Control"] == "private, no-store"
                details = await client.get(f"/_api/media/{item.id}")
                assert details.status_code == 200, details.text
                assert details.json()["data"]["title"] == "Confirmed"
                assert details.json()["data"]["metadata"]["authors"] == ["Writer"]
                assert not await MediaEvent.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem,status",
    [
        ("permission", 403),
        ("unauthenticated", 401),
        ("missing", 404),
        ("invalid", 422),
        ("video", 422),
        ("publish", 503),
    ],
)
def test_save_metadata_http_error(tmp_path, monkeypatch, problem, status):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            user = _user([item.lib_id] if problem == "permission" else None)
            await User.create(
                id=user.id, username=user.username, password="unused", role=user.role
            )
            if problem == "video":
                await MediaLib.filter(id=item.lib_id).update(lib_type=LibType.MOVIE)
            if problem == "publish":

                def denied(*args):
                    raise PermissionError("read only")

                monkeypatch.setattr(writer.os, "replace", denied)
            async with _client(user) as client:
                if problem == "unauthenticated":
                    client.headers.pop("Authorization")
                response = await client.post(
                    f"/_api/media/{item.id + (problem == 'missing')}/metadata",
                    json={
                        "graph_id": 1,
                        "metadata": {}
                        if problem == "invalid"
                        else {"title": "Confirmed"},
                    },
                )
                assert response.status_code == status, response.text
                assert not (Path(item.dir) / "metadata.opf").exists()
                assert (await MediaItem.get(id=item.id)).title == "Old database title"

    asyncio.run(run())


@pytest.mark.parametrize("format", [None, *MediaFormat])
def test_reading_rejects_nfo(tmp_path, format):
    async def run():
        async with _database():
            item = await _item(tmp_path, format)
            nfo = Path(item.dir) / "movie.nfo"
            assert not await shelver.gen_nfo(
                "movie",
                str(nfo),
                {"title": "Wrong format"},
                item_id=item.id,
                overwrite=True,
                refresh=True,
            )
            assert not nfo.exists() and not await MediaEvent.all().exists()

    asyncio.run(run())


def test_save_metadata_cancel(tmp_path, monkeypatch):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            directory = Path(item.dir)
            xml = directory / "metadata.opf"
            started, released = threading.Event(), threading.Event()
            replace = writer.os.replace

            def delayed(src, dest):
                started.set()
                assert released.wait(5)
                replace(src, dest)

            with monkeypatch.context() as patch:
                patch.setattr(writer.os, "replace", delayed)
                saving = asyncio.create_task(
                    MediaItemService.save_metadata(
                        item.id, {"title": "Saved"}, overwrite=True
                    )
                )
                try:
                    assert await asyncio.to_thread(started.wait, 5)
                    saving.cancel()
                    await asyncio.sleep(0)
                    assert not saving.done()
                finally:
                    released.set()
                with pytest.raises(asyncio.CancelledError):
                    await saving
            assert parse_opf(xml.read_bytes()).data.title == "Saved"
            assert (await MediaItem.get(id=item.id)).title == "Old database title"
            event = await MediaEvent.get()
            assert event.payload is not None and event.payload["published"] is False
            published_state = xml.stat()
            assert await watcher.consume_event(event)
            assert xml.stat().st_ino == published_state.st_ino
            assert xml.stat().st_mtime_ns == published_state.st_mtime_ns
            assert not list(directory.glob(".metadata-*.tmp"))
            assert (await MediaItem.get(id=item.id)).title == "Saved"
            assert not await MediaEvent.all().exists()

    asyncio.run(run())


def test_save_metadata_body_change(tmp_path, monkeypatch):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            xml = Path(item.dir) / "metadata.opf"
            xml.write_bytes(_opf("Original"))
            read = writer._read_local

            def changed(path, parser):
                if path.suffix == ".tmp":
                    Path(item.path).write_text("Changed during publication")
                return read(path, parser)

            monkeypatch.setattr(writer, "_read_local", changed)
            with pytest.raises(ContentError, match="content_changed"):
                await MediaItemService.save_metadata(
                    item.id, {"title": "Candidate"}, overwrite=True
                )
            assert xml.read_bytes() == _opf("Original")
            assert (await MediaItem.get(id=item.id)).title == "Old database title"
            assert not list(Path(item.dir).glob(".metadata-*.tmp"))

    asyncio.run(run())


@pytest.mark.parametrize(
    "change", ["move", "remove", "failed_remove", "replace_task", "automatic"]
)
def test_metadata_pending(tmp_path, monkeypatch, change):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)

            async def interrupted(*args, **kwargs):
                raise asyncio.CancelledError

            with monkeypatch.context() as patch:
                patch.setattr(media_service, "write_in_thread", interrupted)
                with pytest.raises(asyncio.CancelledError):
                    await MediaItemService.save_metadata(
                        item.id, {"title": "First"}, overwrite=True
                    )
            event = await MediaEvent.get()
            directory = Path(item.dir)
            if change == "move":
                target = directory.with_name("Moved")
                directory.rename(target)
                await MediaItem.filter(id=item.id).update(
                    dir=str(target), path=str(target / "Book.txt")
                )
                assert await watcher.consume_event(event)
                assert (
                    parse_opf((target / "metadata.opf").read_bytes()).data.title
                    == "First"
                )
                assert not directory.exists()
            elif change in ("remove", "failed_remove"):
                if change == "failed_remove":
                    assert event.payload is not None
                    event.payload["error_code"] = "metadata_write_failed"
                    await event.save(update_fields=["payload"])
                await item.delete()
                assert await watcher.consume_event(event)
                assert not (directory / "metadata.opf").exists()
            elif change == "replace_task":
                assert await MediaItemService.save_metadata(
                    item.id, {"title": "Second"}, overwrite=True
                )
                assert not await watcher.consume_event(event)
                assert (
                    parse_opf((directory / "metadata.opf").read_bytes()).data.title
                    == "Second"
                )
            else:
                with pytest.raises(ContentError, match="content_not_ready"):
                    await MediaItemService.save_metadata(
                        item.id, {"title": "Automatic"}
                    )
                assert (await MediaEvent.get(id=event.id)).payload == event.payload
                assert await watcher.consume_event(event)
                assert (
                    parse_opf((directory / "metadata.opf").read_bytes()).data.title
                    == "First"
                )
            assert not await MediaEvent.all().exists()

    asyncio.run(run())


def test_auto_metadata_invalid_local(tmp_path):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            xml = Path(item.dir) / "metadata.opf"
            xml.write_text("<invalid")
            with pytest.raises(ContentError, match="invalid_metadata"):
                await MediaItemService.save_metadata(item.id, {"title": "Automatic"})
            assert xml.read_text() == "<invalid"
            assert (await MediaItem.get(id=item.id)).title == "Old database title"
            assert await MediaItemService.save_metadata(
                item.id, {"title": "Confirmed"}, overwrite=True
            )
            assert parse_opf(xml.read_bytes()).data.title == "Confirmed"
            assert not await MediaEvent.all().exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload", [None, {}, {"item_id": "invalid"}, {"schema_version": 2, "item_id": 1}]
)
def test_metadata_task_invalid(tmp_path, payload):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            event = await MediaEvent.create(
                lib_id=item.lib_id,
                src_path=item.path,
                event_type="metadata",
                payload=payload,
            )
            assert not await watcher.consume_event(event)
            current = await MediaEvent.get(id=event.id)
            assert (
                current.payload is not None
                and current.payload["error_code"] == "invalid_metadata"
            )
            assert not await watcher.consume_event(current)
            assert (await MediaEvent.get(id=event.id)).updated_at == current.updated_at
            assert not (Path(item.dir) / "metadata.opf").exists()

    asyncio.run(run())


def test_sync_after_manual_save(tmp_path, monkeypatch):
    async def run():
        async with _database():
            item = await _item(tmp_path, MediaFormat.TXT)
            xml = Path(item.dir) / "metadata.opf"
            xml.write_bytes(_opf("Before save"))
            read = media_service._read_metadata
            reached = asyncio.Event()
            loop = asyncio.get_running_loop()

            def observe(*args, **kwargs):
                loop.call_soon_threadsafe(reached.set)
                return read(*args, **kwargs)

            monkeypatch.setattr(media_service, "_read_metadata", observe)
            async with library_lock(item.lib.dir):
                sync = asyncio.create_task(MediaItemService.sync_metadata(item.id))
                try:
                    # a waiting synchronization must not capture pre-save metadata
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(reached.wait(), timeout=0.1)
                    xml.write_bytes(_opf("After save"))
                    await MediaItem.filter(id=item.id).update(title="After save")
                except BaseException:
                    sync.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await sync
                    raise
            assert (await sync).title == "After save"
            assert (await MediaItem.get(id=item.id)).title == "After save"

    asyncio.run(run())
