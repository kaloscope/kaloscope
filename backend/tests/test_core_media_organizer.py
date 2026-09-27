"""Filesystem and database behavior of metadata-driven media organization."""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from filelock import Timeout
from lxml import etree
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.media import organizer
from app.core.media.coordination import library_lock
from app.models.download import Downloader, DownloadState, DownloadTask
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib


@asynccontextmanager
async def _database():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _nfo(path: Path, title: str, tag="movie", extra=""):
    path.write_text(
        f"<{tag}><title>{title}</title><year>2026</year>"
        '<uniqueid type="tmdb" default="true">10</uniqueid>'
        f"{extra}</{tag}>",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("lib_type", "tag"),
    [
        (LibType.MOVIE, "movie"),
        (LibType.TV_SHOW, "tvshow"),
        (LibType.TV_SHOW, "episodedetails"),
    ],
)
def test_metadata(tmp_path, lib_type, tag):
    path = tmp_path / "media.nfo"
    _nfo(
        path,
        "示例 &amp; New",
        tag=tag,
        extra=(
            "<originaltitle>Original</originaltitle>"
            "<season>0</season><episode>2</episode>"
            "<actor><name>Alice</name><role>Lead</role></actor>"
        ),
    )

    metadata = organizer._metadata(path, lib_type, tag)

    assert metadata["title"] == "示例 & New"
    assert metadata["originaltitle"] == "Original"
    assert metadata["year"] == 2026
    assert metadata["unique_id"] == "10"
    assert metadata["nfo_source"] == "tmdb"
    assert metadata["nfo_path"] == str(path)
    assert metadata["actors"] == [{"name": "Alice", "role": "Lead", "thumb": None}]
    assert (metadata["season"], metadata["episode"]) == (
        (0, 2) if lib_type is LibType.TV_SHOW else (None, None)
    )


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("", etree.XMLSyntaxError),
        ("<movie><title>Partial</title>", etree.XMLSyntaxError),
        ("<movie><title>First</title></movie><movie/>", etree.XMLSyntaxError),
        ("<tvshow><title>Wrong type</title></tvshow>", ValueError),
        ("<movie><year>2026</year></movie>", ValueError),
        ("<movie><title/></movie>", ValueError),
        ("<movie><title>  </title></movie>", ValueError),
    ],
)
def test_metadata_invalid(tmp_path, content, error):
    path = tmp_path / "media.nfo"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(error):
        organizer._metadata(path, LibType.MOVIE, "movie")


def test_metadata_entity(tmp_path):
    title = tmp_path / "title.txt"
    title.write_text("External title", encoding="utf-8")
    path = tmp_path / "media.nfo"
    path.write_text(
        f'<!DOCTYPE movie [<!ENTITY title SYSTEM "{title.as_uri()}">]>'
        "<movie><title>&title;</title></movie>",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="NFO has no title"):
        organizer._metadata(path, LibType.MOVIE, "movie")


async def _movie(root: Path, template="{{title}}"):
    lib = await MediaLib.create(
        name="Movies",
        dir=str(root),
        lib_type=LibType.MOVIE,
        priority=1,
        rename_template=template,
    )
    video = root / "original.mkv"
    video.write_bytes(b"video")
    nfo = video.with_suffix(".nfo")
    _nfo(nfo, "New Movie")
    item = await MediaItem.create(
        lib=lib,
        path=str(video),
        dir=str(root),
        name=video.stem,
        nfo_path=str(nfo),
        hash="a" * 32,
    )
    return lib, item


async def _journal(lib: MediaLib, item: MediaItem):
    """Persist a movie rename plan without invoking the planner.

    Args:
        lib: The library containing the movie.
        item: The indexed movie and its associated NFO.

    Returns:
        The persisted organization event.
    """
    root = Path(lib.dir)
    video, nfo = root / "New Movie.mkv", root / "New Movie.nfo"
    return await MediaEvent.create(
        lib=lib,
        event_type="organize",
        src_path=item.path,
        payload={
            "moves": [
                {
                    "src": source,
                    "dst": str(target),
                    "identity": organizer._fingerprint(Path(source)),
                }
                for source, target in ((item.path, video), (item.nfo_path, nfo))
            ],
            "updates": [
                {
                    "id": item.id,
                    "path": str(video),
                    "dir": str(root),
                    "name": video.stem,
                    "nfo_path": str(nfo),
                    "title": "New Movie",
                    "year": 2026,
                }
            ],
            "parent": None,
            "delete_parent": None,
            "mapping": {item.path: str(video)},
            "nfo_edits": [],
            "symlinks": [],
            "creates": [],
            "cleanup": None,
        },
    )


@pytest.mark.parametrize("interruption", ["before", "move", "database"])
def test_journal_recovery(tmp_path, monkeypatch, interruption):
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))
    db_url = f"sqlite://{tmp_path / 'recovery.sqlite3'}"

    async def run():
        await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
        await Tortoise.generate_schemas()
        try:
            lib, item = await _movie(root)
            journal = await _journal(lib, item)
            original = {Path(path).name: path for path in (item.path, item.nfo_path)}
            downloader = await Downloader.create(
                config="config", name="RPC", priority=1
            )
            download = await DownloadTask.create(
                downloader=downloader,
                dir=str(tmp_path / "downloads"),
                name="Movie",
                state=DownloadState.COMPLETED,
                files=list(original),
                transfer_lib=lib,
                transfer_targets=original,
            )
            notification = await MediaEvent.create(
                lib=lib, event_type="created", src_path=item.path
            )
            rename, delete = organizer.rename_exclusive, MediaEvent.delete

            def interrupted_move(source, destination):
                if interruption == "before" or source.suffix == ".nfo":
                    raise OSError("Interrupted file movement")
                rename(source, destination)

            async def interrupted_delete(event, *args, **kwargs):
                await delete(event, *args, **kwargs)
                if event.id == journal.id:
                    raise OSError("Interrupted database commit")

            with monkeypatch.context() as patcher:
                if interruption == "database":
                    patcher.setattr(MediaEvent, "delete", interrupted_delete)
                else:
                    patcher.setattr(organizer, "rename_exclusive", interrupted_move)
                async with library_lock(lib.dir):
                    with pytest.raises(organizer.OrganizePendingError):
                        await organizer.recover_organizing(lib)

            await item.refresh_from_db()
            await download.refresh_from_db()
            assert item.path == original["original.mkv"]
            assert item.title is None
            assert download.transfer_targets == original
            assert await MediaEvent.filter(id=journal.id).exists()

            await Tortoise.close_connections()
            await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
            lib = await MediaLib.get(id=lib.id)
            async with library_lock(lib.dir):
                mapping = await organizer.recover_organizing(lib)
                assert await organizer.recover_organizing(lib) == {}

            await item.refresh_from_db()
            await download.refresh_from_db()
            assert mapping == {original["original.mkv"]: str(root / "New Movie.mkv")}
            assert item.path == str(root / "New Movie.mkv")
            assert item.nfo_path == str(root / "New Movie.nfo")
            assert item.title == "New Movie" and item.year == 2026
            assert item.hash == "a" * 32
            assert item.nfo_mtime == datetime.fromtimestamp(
                Path(item.nfo_path).stat().st_mtime, tz=UTC
            )
            assert Path(item.path).read_bytes() == b"video"
            assert download.transfer_targets == {
                "original.mkv": item.path,
                "original.nfo": item.nfo_path,
            }
            assert all(not Path(path).exists() for path in original.values())
            assert await MediaItem.filter(lib=lib).count() == 1
            assert not await MediaEvent.filter(id=journal.id).exists()
            assert await MediaEvent.filter(id=notification.id).exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    "conflict", ["source", "destination", "moved", "outside", "directory"]
)
def test_journal_conflict(tmp_path, monkeypatch, conflict):
    root, outside = tmp_path / "library", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))

    async def run():
        async with _database():
            lib, item = await _movie(root)
            journal = await _journal(lib, item)
            source, destination = Path(item.path), root / "New Movie.mkv"
            if conflict == "source":
                source.write_bytes(b"replacement")
            elif conflict == "destination":
                destination.write_bytes(b"existing")
            elif conflict == "moved":
                source.rename(destination)
                destination.write_bytes(b"replacement")
            else:
                if conflict == "directory":
                    (root / "link").symlink_to(outside, target_is_directory=True)
                    destination = root / "link" / destination.name
                else:
                    destination = outside / destination.name
                journal.payload["moves"][0]["dst"] = str(destination)
                await journal.save(update_fields=["payload"])
            contents = {
                path: path.read_bytes() if path.exists() else None
                for path in (source, destination, Path(item.nfo_path))
            }

            async with library_lock(lib.dir):
                with pytest.raises(organizer.OrganizePendingError):
                    await organizer.recover_organizing(lib)

            await item.refresh_from_db()
            assert item.path == str(source)
            assert item.title is None
            assert await MediaEvent.filter(id=journal.id).exists()
            for path, content in contents.items():
                assert (path.read_bytes() if path.exists() else None) == content
            assert list(outside.iterdir()) == []

    asyncio.run(run())


@pytest.mark.parametrize("cancel_count", [1, 2])
@pytest.mark.parametrize("failure", [None, "before", "after"])
def test_journal_cancellation(tmp_path, monkeypatch, cancel_count, failure):
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))

    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            journal = await _journal(lib, item)
            entered, release, finished = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )
            original = organizer._move_files

            def paused_writer(root, payload):
                entered.set()
                try:
                    assert release.wait(timeout=5)
                    if failure == "before":
                        raise OSError("Disk unavailable")
                    original(root, payload)
                    if failure == "after":
                        raise OSError("Interrupted after movement")
                finally:
                    finished.set()

            async def recover():
                async with library_lock(lib.dir):
                    await organizer.recover_organizing(lib)

            monkeypatch.setattr(organizer, "_move_files", paused_writer)
            task = asyncio.create_task(recover())
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                for _ in range(cancel_count):
                    task.cancel()
                    await asyncio.sleep(0)
                    with pytest.raises(Timeout):
                        async with await library_lock(lib.dir).acquire(timeout=0):
                            pass
                    assert not finished.is_set()
                    assert not task.done()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

            with pytest.raises(asyncio.CancelledError):
                task.result()
            assert finished.is_set()
            assert await MediaEvent.filter(id=journal.id).exists()
            monkeypatch.setattr(organizer, "_move_files", original)
            async with library_lock(lib.dir):
                await organizer.recover_organizing(lib)
            await item.refresh_from_db()
            assert item.path == str(tmp_path / "New Movie.mkv")
            assert Path(item.path).read_bytes() == b"video"
            assert not await MediaEvent.filter(id=journal.id).exists()

    asyncio.run(run())


@pytest.mark.parametrize("mode", [None, 0o640])
def test_journal_files(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))

    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            journal = await _journal(lib, item)
            nfo, copied, alias = (
                tmp_path / name for name in ("New Movie.nfo", "copy.nfo", "alias.mkv")
            )
            content = "<movie><title>New Movie</title></movie>"
            Path(item.nfo_path).chmod(0o640)
            alias.symlink_to(Path(item.path).name)
            journal.payload["nfo_edits"] = [
                {
                    "path": str(nfo),
                    "before": hashlib.sha256(
                        Path(item.nfo_path).read_bytes()
                    ).hexdigest(),
                    "content": content,
                }
            ]
            journal.payload["creates"] = [{"path": str(copied), "content": content}]
            if mode is not None:
                journal.payload["creates"][0]["mode"] = mode
            journal.payload["symlinks"] = [
                {
                    "path": str(alias),
                    "before": Path(item.path).name,
                    "target": "New Movie.mkv",
                }
            ]
            await journal.save(update_fields=["payload"])
            original = organizer._move_files

            def interrupted_writer(root, payload):
                original(root, payload)
                raise OSError("Interrupted before database commit")

            with monkeypatch.context() as patcher:
                patcher.setattr(organizer, "_move_files", interrupted_writer)
                async with library_lock(lib.dir):
                    with pytest.raises(organizer.OrganizePendingError):
                        await organizer.recover_organizing(lib)
            assert await MediaEvent.filter(id=journal.id).exists()
            inodes = {path: path.lstat().st_ino for path in (nfo, copied, alias)}

            async with library_lock(lib.dir):
                await organizer.recover_organizing(lib)

            assert nfo.read_text() == copied.read_text() == content
            assert nfo.stat().st_mode & 0o777 == 0o640
            assert copied.stat().st_mode & 0o777 == (
                mode if mode is not None else 0o600
            )
            assert alias.readlink() == Path("New Movie.mkv")
            assert alias.read_bytes() == b"video"
            assert {path: path.lstat().st_ino for path in inodes} == inodes
            assert not list(tmp_path.glob(".organizing-*"))
            assert not await MediaEvent.filter(id=journal.id).exists()

    asyncio.run(run())
