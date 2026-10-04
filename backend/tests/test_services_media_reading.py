"""Tests for reading details, summary synchronization, covers and source boundaries."""

import asyncio
import hashlib
import os
import threading
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from app.core.middleware import on_request, on_response
from app.models.media import IndexState, LibType, MediaFormat, MediaItem, MediaLib
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


@pytest.mark.parametrize("format", [MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP])
def test_renamed_chapter_metadata(tmp_path, format):
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
            directory.rename(destination)
            await MediaItemService.rename_reading_directory(
                parent.lib_id, directory, destination
            )
            data = await MediaItemService.get_details(child.id, _user([parent.lib_id]))
            assert data["title"] == "Chapter" and data["metadata_state"] == "ready"
            assert data["metadata"]["authors"] == ("Author",)
            assert data["parent"]["id"] == parent.id
            cover = await MediaItemService.get_cover(child.id, _user([parent.lib_id]))
            assert cover is not None and cover.path == destination / "cover.png"
            assert cover.data == _PNG
            assert (destination / "ComicInfo.xml").read_text() == xml
            (destination / "ComicInfo.xml").write_text(xml.replace("Chapter", "Fresh"))
            (work / "ComicInfo.xml").write_text(
                "<ComicInfo><Writer>Current author</Writer></ComicInfo>"
            )
            data = await MediaItemService.get_details(child.id, _user([parent.lib_id]))
            assert data["title"] == "Fresh"
            assert data["metadata"]["authors"] == ("Current author",)
            assert await MediaItem.all().count() == 2

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
