"""Filesystem and database behavior of metadata-driven media organization."""

import asyncio
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from filelock import Timeout
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.media import organizer
from app.core.media.coordination import library_lock
from app.core.media.shelver import gen_nfo, update_metadata
from app.core.media.watcher import LibWatcher
from app.models.download import Downloader, DownloadState, DownloadTask
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import HistoryType, User, UserHistory, UserRole
from app.services.danmaku import Danmaku, DanmakuAnime, DanmakuMeta, DanmakuService


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


async def _episode(root: Path):
    """Create an indexed episode with existing NFO metadata.

    Args:
        root: The temporary media library root directory.

    Returns:
        The library, parent directory item, and episode item.
    """
    lib = await MediaLib.create(
        name="Shows",
        dir=str(root),
        lib_type=LibType.TV_SHOW,
        priority=1,
        rename_template="{{show_title}}/Season {{season}}/{{episode_code}} - {{title}}",
    )
    source = root / "Original"
    source.mkdir()
    parent_nfo = source / "Original.nfo"
    _nfo(parent_nfo, "Show", "tvshow", "<season>1</season>")
    parent = await MediaItem.create(
        lib=lib,
        path=str(source),
        dir=str(source),
        name=source.name,
        nfo_path=str(parent_nfo),
        season=1,
    )
    video = source / "old.mkv"
    video.write_bytes(b"video")
    nfo = video.with_suffix(".nfo")
    _nfo(
        nfo,
        "Old title",
        "episodedetails",
        "<season>1</season><episode>1</episode>"
        "<aired>2026-01-01</aired><rating>5.25</rating>"
        "<art><poster>https://example.com/old-poster.jpg</poster>"
        "<fanart>https://example.com/old-backdrop.jpg</fanart></art>",
    )
    item = await MediaItem.create(
        lib=lib,
        parent=parent,
        path=str(video),
        dir=str(source),
        name=video.stem,
        nfo_path=str(nfo),
    )
    await update_metadata(lib, nfo)
    await item.refresh_from_db()
    return lib, parent, item


def test_movie_rename(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            user = await User.create(
                username="viewer", password="test", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user,
                rel_type=HistoryType.VIDEO,
                rel_id=item.id,
                position=42,
            )
            subtitle = tmp_path / "original.zh-CN.srt"
            subtitle.write_text("subtitles")
            mapping = await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            assert item.path == str(tmp_path / "New Movie.mkv")
            assert item.hash == "a" * 32
            await history.refresh_from_db()
            assert history.rel_id == item.id and history.position == 42
            assert (tmp_path / "New Movie.zh-CN.srt").read_text() == "subtitles"
            assert Path(item.nfo_path).is_file()
            assert mapping[str(tmp_path / "original.mkv")] == item.path
            assert await MediaItem.all().count() == 1
            assert await organizer.organize_items(lib, [item.id]) == {}
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


def test_movie_hierarchy(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, "{{title}}/{{title}}")
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            assert item.parent_id is not None
            parent_id = item.parent_id
            parent = await MediaItem.get(id=parent_id)
            assert parent.path == str(tmp_path / "New Movie")
            assert Path(parent.nfo_path).is_file()
            assert item.nfo_path is None
            lib.rename_template = "{{title}}"
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            assert item.parent_id is None
            assert item.path == str(tmp_path / "New Movie.mkv")
            assert item.title == "New Movie"
            assert Path(item.nfo_path).is_file()
            assert not await MediaItem.filter(id=parent_id).exists()

    asyncio.run(run())


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("template", ["{{title}}", "{{title}}/{{title}}"])
def test_shared_movie_companions(tmp_path, indexed, template):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, template)
            other_video = tmp_path / "original.mp4"
            other_video.write_bytes(b"other version")
            subtitle = tmp_path / "original.en.srt"
            subtitle.write_text("shared subtitles")
            ids = [item.id]
            if indexed:
                other = await MediaItem.create(
                    lib=lib,
                    path=str(other_video),
                    dir=str(tmp_path),
                    name=other_video.stem,
                    nfo_path=item.nfo_path,
                )
                ids.append(other.id)
            contents = {file.name: file.read_bytes() for file in tmp_path.iterdir()}

            mapping = await organizer.organize_items(lib, ids)

            assert mapping == {}
            assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == (
                contents
            )
            for current in await MediaItem.filter(lib=lib):
                assert Path(current.path).name in contents
                assert current.nfo_path == str(tmp_path / "original.nfo")
                assert Path(current.nfo_path).is_file()
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("layout", ["flat", "directory"])
@pytest.mark.parametrize("filename", ["New Movie.mp4", "Another Edition.mkv"])
def test_movie_destination(tmp_path, indexed, layout, filename):
    async def run():
        async with _database():
            template = "{{title}}/{{title}}" if layout == "directory" else "{{title}}"
            lib, item = await _movie(tmp_path, template)
            destination = tmp_path / "New Movie" if layout == "directory" else tmp_path
            parent = None
            if layout == "directory":
                destination.mkdir()
                nfo = destination / "New Movie.nfo"
                _nfo(nfo, "New Movie")
                if indexed:
                    parent = await MediaItem.create(
                        lib=lib,
                        path=str(destination),
                        dir=str(destination),
                        name=destination.name,
                        nfo_path=str(nfo),
                    )
            video = destination / filename
            video.write_bytes(b"existing video")
            if indexed:
                await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(video),
                    dir=str(destination),
                    name=video.stem,
                )
            original_path = item.path
            original_items = await MediaItem.all().values()
            original_files = {
                path: path.read_bytes()
                for path in tmp_path.rglob("*")
                if path.is_file()
            }

            mapping = await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()

            if layout == "directory" or filename == "New Movie.mp4":
                assert mapping == {}
                assert item.path == original_path
                assert await MediaItem.all().values() == original_items
                assert {
                    path: path.read_bytes()
                    for path in tmp_path.rglob("*")
                    if path.is_file()
                } == original_files
            else:
                assert item.path == str(tmp_path / "New Movie.mkv")
                assert mapping[original_path] == item.path
                assert Path(item.nfo_path).is_file()
            assert video.read_bytes() == b"existing video"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("companion", ["nfo", "cache"])
@pytest.mark.parametrize("relocated", [False, True])
@pytest.mark.parametrize("collision", [False, True])
def test_companion_rename(tmp_path, monkeypatch, companion, relocated, collision):
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))

    async def run():
        async with _database():
            target_name = "Renamed" if relocated else "Show"
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template=f"{target_name}/{{{{episode_code}}}} - {{{{title}}}}",
            )
            source = tmp_path / "Show"
            source.mkdir()
            parent_nfo = source / "Show.nfo"
            _nfo(parent_nfo, "Show", "tvshow", "<season>1</season>")
            parent = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(source),
                name=source.name,
                nfo_path=str(parent_nfo),
                season=1,
            )
            video = source / "old.mkv"
            video.write_bytes(b"video")
            nfo = video.with_suffix(".NFO" if companion == "nfo" else ".nfo")
            _nfo(
                nfo,
                "Pilot",
                "episodedetails",
                "<season>1</season><episode>1</episode>",
            )
            item = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(source),
                name=video.stem,
                nfo_path=str(nfo),
            )
            original = nfo
            destination = tmp_path / target_name / "S01E01 - Pilot.mkv"
            target = destination.with_suffix(".NFO")
            if companion == "cache":
                original = source / ".old.json"
                original.write_text('[{"text":"Local comment","start":1000}]')
                target = destination.parent / f".{destination.stem}.json"
                before = await DanmakuService.match_danmakus(item.path)
                assert [comment.text for comment in before.comments] == [
                    "Local comment"
                ]
            content = original.read_bytes()
            if collision:
                if relocated:
                    destination.parent.mkdir()
                    _nfo(
                        destination.parent / f"{target_name}.nfo",
                        "Show",
                        "tvshow",
                        "<season>1</season>",
                    )
                target.write_bytes(b"Existing companion")

            mapping = await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()

            if collision:
                assert mapping == {}
                assert item.path == str(video)
                assert original.read_bytes() == content
                assert target.read_bytes() == b"Existing companion"
            else:
                assert item.path == str(destination)
                assert mapping[str(original)] == str(target)
                assert target.read_bytes() == content
                assert not original.exists()
                if companion == "nfo":
                    assert item.nfo_path == str(target)
                else:
                    after = await DanmakuService.match_danmakus(item.path)
                    assert after.comments == before.comments
                    assert item.danmaku_path is None
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "invalid", ["collision", "incomplete_nfo", "directory_symlink"]
)
def test_unsafe_plan(tmp_path, invalid):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            if invalid == "collision":
                (tmp_path / "New Movie.mkv").write_bytes(b"other")
            elif invalid == "incomplete_nfo":
                Path(item.nfo_path).write_text("<movie><title>Partial</title>")
            else:
                real = tmp_path / "real"
                real.mkdir()
                (tmp_path / "destination").symlink_to(real, target_is_directory=True)
                lib.rename_template = "destination/{{title}}"
            assert await organizer.organize_items(lib, [item.id]) == {}
            assert Path(item.path).read_bytes() == b"video"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("conflict", ["existing_file", "planned_file"])
def test_destination_ancestor(tmp_path, conflict):
    async def run():
        async with _database():
            lib, parent, item = await _episode(tmp_path)
            source = Path(parent.path)
            destination = tmp_path / "Show" / "Season 01"
            filename = "S01E01 - Old title.mkv"
            directory_name = "extras" if conflict == "existing_file" else filename
            artwork = source / directory_name / "poster.jpg"
            artwork.parent.mkdir()
            artwork.write_bytes(b"poster")
            if conflict == "existing_file":
                destination.mkdir(parents=True)
                _nfo(
                    destination / "Season 01.nfo",
                    "Show",
                    "tvshow",
                    "<season>1</season>",
                )
                blocker = destination / directory_name
                blocker.write_bytes(b"existing file")
                blocker.chmod(0o755)
            original_path = item.path
            original_nfo = Path(item.nfo_path).read_bytes()
            original_parent_nfo = Path(parent.nfo_path).read_bytes()

            mapping = await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()

            assert mapping == {}
            assert item.path == original_path
            assert Path(item.path).read_bytes() == b"video"
            assert Path(item.nfo_path).read_bytes() == original_nfo
            assert Path(parent.nfo_path).read_bytes() == original_parent_nfo
            assert artwork.read_bytes() == b"poster"
            if conflict == "existing_file":
                assert blocker.read_bytes() == b"existing file"
            else:
                assert not destination.exists()
            assert not await MediaEvent.filter(event_type="organize").exists()
            assert await organizer.recover_organizing(lib) == {}

    asyncio.run(run())


def test_recovery(tmp_path, monkeypatch):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            cache = tmp_path / ".original.json"
            cache.write_text('[{"text":"Local comment"}]')
            original_rename = organizer.rename_exclusive
            calls = 0

            def fail_second(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated interruption")
                original_rename(source, destination)

            monkeypatch.setattr(organizer, "rename_exclusive", fail_second)
            with pytest.raises(organizer.OrganizePendingError):
                await organizer.organize_items(lib, [item.id])
            assert await MediaEvent.filter(event_type="organize").count() == 1
            assert (tmp_path / "New Movie.mkv").is_file()
            assert cache.is_file()
            monkeypatch.setattr(organizer, "rename_exclusive", original_rename)
            await organizer.recover_organizing(lib)
            await item.refresh_from_db()
            assert item.path == str(tmp_path / "New Movie.mkv")
            assert Path(item.nfo_path).is_file()
            assert (tmp_path / ".New Movie.json").read_text() == (
                '[{"text":"Local comment"}]'
            )
            assert not cache.exists()
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("known_nfo", [False, True])
@pytest.mark.parametrize("episode", [None, 1])
def test_episode_recovery(tmp_path, monkeypatch, known_nfo, episode):
    async def run():
        async with _database():
            lib, parent, item = await _episode(tmp_path)
            nfo = Path(item.nfo_path)
            shared = tmp_path / "shared"
            shared.mkdir()
            poster = shared / "poster.jpg"
            poster.write_bytes(b"poster")
            backdrop = nfo.parent / "backdrop.jpg"
            backdrop.write_bytes(b"backdrop")
            episode_element = (
                f"<episode>{episode}</episode>" if episode is not None else ""
            )
            nfo.write_text(
                "<episodedetails><title>New title</title><year>2027</year>"
                f"<season>2</season>{episode_element}"
                '<uniqueid type="imdb" default="true">new-id</uniqueid>'
                "<aired>2027-01-02</aired><rating>8.25</rating>"
                "<art><poster>../shared/poster.jpg</poster>"
                "<fanart>backdrop.jpg</fanart></art></episodedetails>",
                encoding="utf-8",
            )
            if not known_nfo:
                await MediaItem.filter(id=item.id).update(nfo_path=None, nfo_mtime=None)
            original = organizer._move_files

            def interrupted(root, payload):
                original(root, payload)
                raise OSError("interrupted before metadata commit")

            monkeypatch.setattr(organizer, "_move_files", interrupted)

            with pytest.raises(organizer.OrganizePendingError):
                await organizer.organize_items(lib, [parent.id])
            monkeypatch.setattr(organizer, "_move_files", original)
            await organizer.recover_organizing(lib)
            await item.refresh_from_db()

            expected = tmp_path / "Show" / "Season 02"
            filename = "S02E01 - New title.mkv" if episode is not None else "old.mkv"
            assert item.path == str(expected / filename)
            assert Path(item.path).read_bytes() == b"video"
            assert item.title == "New title"
            assert item.year == 2027
            assert item.season == 2 and item.episode == 1
            assert item.nfo_source == "imdb" and item.unique_id == "new-id"
            assert item.aired == "2027-01-02" and str(item.rating) == "8.25"
            assert item.poster == "../../shared/poster.jpg"
            assert (expected / item.poster).resolve() == poster
            assert (expected / item.backdrop).read_bytes() == b"backdrop"
            assert not backdrop.exists()
            metadata = organizer._metadata(
                Path(item.nfo_path), lib.lib_type, "episodedetails"
            )
            assert metadata["poster"] == item.poster
            assert metadata["backdrop"] == item.backdrop
            assert item.nfo_mtime == datetime.fromtimestamp(
                Path(item.nfo_path).stat().st_mtime, tz=UTC
            )
            events = Queue()
            watcher = LibWatcher(None)
            watcher._observers = {lib.dir: (None, events)}
            await watcher._enqueue_events(lib, backfill_nfo_events=False)
            assert events.empty()
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("invalid", [False, True])
def test_episode_metadata(tmp_path, invalid):
    async def run():
        async with _database():
            lib, parent, item = await _episode(tmp_path)
            original_path = item.path
            title = "x" * 256 if invalid else "New title"
            Path(item.nfo_path).write_text(
                f"<episodedetails><title>{title}</title></episodedetails>",
                encoding="utf-8",
            )

            mapping = await organizer.organize_items(lib, [parent.id])
            await item.refresh_from_db()

            if invalid:
                assert mapping == {}
                assert item.path == original_path
                assert item.title == "Old title"
            else:
                assert item.title == title
                assert item.year == 2026
                assert item.season == 1 and item.episode == 1
                for name in (
                    "nfo_source",
                    "unique_id",
                    "aired",
                    "rating",
                    "poster",
                    "backdrop",
                ):
                    assert getattr(item, name) is None
            assert Path(item.path).read_bytes() == b"video"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


def test_legacy_episode_recovery(tmp_path):
    async def run():
        async with _database():
            lib, parent, item = await _episode(tmp_path)
            _nfo(
                Path(item.nfo_path),
                "New title",
                "episodedetails",
                "<season>1</season><episode>1</episode>",
            )
            payload = await organizer._plan(lib, [item], parent)
            for name in organizer._METADATA:
                if name not in ("season", "episode"):
                    payload["updates"][0].pop(name, None)
            await MediaEvent.create(
                lib=lib,
                event_type="organize",
                src_path=item.path,
                is_directory=True,
                payload=payload,
            )
            organizer._move_files(tmp_path, payload)

            await organizer.recover_organizing(lib)
            await item.refresh_from_db()

            assert item.nfo_mtime is None
            events = Queue()
            watcher = LibWatcher(None)
            watcher._observers = {lib.dir: (None, events)}
            await watcher._enqueue_events(lib, backfill_nfo_events=False)
            assert events.qsize() == 1
            event = events.get_nowait()
            assert event.src_path == item.nfo_path
            await update_metadata(lib, event.src_path)
            await item.refresh_from_db()
            assert item.title == "New title"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


def test_tvshow_missing_nfo(tmp_path):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template=(
                    "{{show_title}}/Season {{season}}/{{episode_code}} - {{title}}"
                ),
            )
            source = tmp_path / "Original"
            source.mkdir()
            parent_nfo = source / "Original.nfo"
            _nfo(parent_nfo, "Show", "tvshow")
            parent = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(source),
                name=source.name,
                nfo_path=str(parent_nfo),
                season=1,
            )
            items = []
            for number in (1, 2):
                file = source / f"old{number}.mkv"
                file.write_bytes(b"video")
                nfo = file.with_suffix(".nfo")
                if number == 1:
                    _nfo(
                        nfo,
                        "Pilot",
                        "episodedetails",
                        "<season>1</season><episode>1</episode>",
                    )
                items.append(
                    await MediaItem.create(
                        lib=lib,
                        parent=parent,
                        path=str(file),
                        dir=str(source),
                        name=file.stem,
                        season=1,
                        episode=number,
                        nfo_path=str(nfo) if number == 1 else None,
                    )
                )
            await organizer.organize_items(lib, [parent.id])
            await parent.refresh_from_db()
            await items[0].refresh_from_db()
            await items[1].refresh_from_db()
            target = tmp_path / "Show" / "Season 01"
            assert parent.path == str(target)
            assert items[0].path == str(target / "S01E01 - Pilot.mkv")
            assert items[1].path == str(target / "old2.mkv")
            assert items[1].parent_id == parent.id
            assert Path(parent.nfo_path).name == "Season 01.nfo"
            assert not source.exists()

    asyncio.run(run())


@pytest.mark.parametrize("companion", ["nfo", "cache"])
@pytest.mark.parametrize("recovery", [False, True])
def test_missing_companion(tmp_path, monkeypatch, companion, recovery):
    root = tmp_path / "library"
    root.mkdir()
    locks = tmp_path / "locks"
    locks.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _: str(locks))

    async def run():
        async with _database():
            lib, parent, item = await _episode(root)
            source = Path(item.dir)
            if companion == "nfo":
                missing = Path(item.nfo_path)
                missing.unlink()
                (source / "poster.jpg").write_bytes(b"poster")
                item.poster = "poster.jpg"
                await item.save(update_fields=["poster"])
            else:
                missing = source / ".old.json"
                item.danmaku_path = str(missing)
                item.danmaku_meta = DanmakuMeta(
                    anime_id="show", episode_id="episode", type="tvseries"
                ).model_dump()
                await item.save(update_fields=["danmaku_path", "danmaku_meta"])
                lib.danmaku_server = "https://danmaku.example"
                await lib.save(update_fields=["danmaku_server"])
                comments = [Danmaku(text="Restored", start=1)]
                monkeypatch.setattr(
                    DanmakuService, "load_from_server", AsyncMock(return_value=comments)
                )

            async with library_lock(lib.dir):
                if recovery:
                    with monkeypatch.context() as patcher:
                        patcher.setattr(
                            organizer,
                            "_finish",
                            AsyncMock(side_effect=RuntimeError("interrupted")),
                        )
                        with pytest.raises(RuntimeError, match="interrupted"):
                            await organizer.organize_items(lib, [item.id])
                    await organizer.recover_organizing(lib)
                else:
                    await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()

            assert not source.exists()
            assert Path(item.path).is_file()
            assert not await MediaEvent.filter(event_type="organize").exists()
            if companion == "nfo":
                assert item.nfo_path is None
                assert item.nfo_mtime is None
                assert (Path(item.dir) / item.poster).read_bytes() == b"poster"
                assert await gen_nfo(
                    "episode",
                    str(missing),
                    {"title": "Restored", "season": 1, "episode": 1},
                    item_id=item.id,
                )
                current_nfo = Path(item.path).with_suffix(".nfo")
                assert await update_metadata(lib, current_nfo) == [item.id]
                await item.refresh_from_db()
                assert item.nfo_path == str(current_nfo)
                assert item.title == "Restored"
            else:
                assert item.danmaku_path is None
                result = await DanmakuService.match_danmakus(item.path)
                await item.refresh_from_db()
                current_cache = Path(item.dir) / f".{item.name}.json"
                assert item.danmaku_path == str(current_cache)
                assert await DanmakuService.load_from_cache(current_cache) == comments
                assert result.comments == comments
                assert result.metadata.episode_id == "episode"
            assert not missing.exists()
            assert not source.exists()

    asyncio.run(run())


@pytest.mark.parametrize("season_directory", [False, True])
@pytest.mark.parametrize(
    ("episode_season", "item_season", "nfo_season", "parent_season", "expected"),
    [
        (2, 3, 4, 5, 2),
        (None, 2, 1, 1, 2),
        (None, 0, 1, 1, 0),
        (None, None, 2, 1, 2),
        (None, None, 0, 1, 0),
        (None, None, None, 2, 2),
    ],
)
def test_season_fallback(
    tmp_path,
    season_directory,
    episode_season,
    item_season,
    nfo_season,
    parent_season,
    expected,
):
    async def run():
        async with _database():
            template = "{{show_title}}/"
            if season_directory:
                template += "Season {{season}}/"
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template=template + "{{episode_code}}",
            )
            source = tmp_path / "Original"
            source.mkdir()
            parent_nfo = source / "Original.nfo"
            _nfo(
                parent_nfo,
                "Show",
                "tvshow",
                f"<season>{nfo_season}</season>" if nfo_season is not None else "",
            )
            parent = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(source),
                name=source.name,
                nfo_path=str(parent_nfo),
                season=parent_season,
            )
            video = source / "old.mkv"
            video.write_bytes(b"video")
            nfo = video.with_suffix(".nfo")
            season_element = (
                f"<season>{episode_season}</season>"
                if episode_season is not None
                else ""
            )
            _nfo(
                nfo,
                "Episode",
                "episodedetails",
                season_element + "<episode>1</episode>",
            )
            item = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(source),
                name=video.stem,
                nfo_path=str(nfo),
                season=item_season,
                episode=1,
            )

            await organizer.organize_items(lib, [parent.id])

            await item.refresh_from_db()
            target = tmp_path / "Show"
            if season_directory:
                target /= f"Season {expected:02d}"
            assert item.path == str(target / f"S{expected:02d}E01.mkv")
            assert item.season == expected
            assert Path(item.path).read_bytes() == b"video"
            assert Path(item.nfo_path).is_file()
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("original_source", [False, True])
def test_transfer_mapping(tmp_path, original_source):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            downloader = await Downloader.create(name="Test", config="{}", priority=1)
            source = tmp_path if original_source else tmp_path / "downloads"
            if not original_source:
                source.mkdir()
                (source / "original.mkv").hardlink_to(Path(item.path))
            task = await DownloadTask.create(
                downloader=downloader,
                name="original",
                dir=str(source),
                files=["original.mkv"],
                state=DownloadState.COMPLETED,
                transfer_lib=lib,
            )
            result = await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            await task.refresh_from_db()
            if original_source:
                assert result == {}
                assert item.path == str(tmp_path / "original.mkv")
            else:
                assert item.path == str(tmp_path / "New Movie.mkv")
                assert task.transfer_targets == {"original.mkv": item.path}
                assert (source / "original.mkv").read_bytes() == b"video"

    asyncio.run(run())


def test_relative_symlink(tmp_path):
    async def run():
        async with _database():
            root = tmp_path / "library"
            root.mkdir()
            original = tmp_path / "download.mkv"
            original.write_bytes(b"original download")
            lib, item = await _movie(root, "{{title}}/{{title}}")
            path = Path(item.path)
            path.unlink()
            path.symlink_to("../download.mkv")
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            assert Path(item.path).is_symlink()
            assert Path(item.path).resolve() == original
            assert original.read_bytes() == b"original download"

    asyncio.run(run())


def test_movie_artwork(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, "{{title}}/{{title}}")
            artwork = tmp_path / "poster.jpg"
            artwork.write_bytes(b"picture")
            _nfo(
                Path(item.nfo_path),
                "New Movie",
                extra=f"<art><poster>{artwork}</poster></art>",
            )
            item.poster = str(artwork)
            await item.save(update_fields=["poster"])
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            parent = await MediaItem.get(id=item.parent_id)
            target = tmp_path / "New Movie" / "poster.jpg"
            assert target.read_bytes() == b"picture"
            assert parent.poster == str(target)
            assert item.poster == str(target)
            assert str(target) in Path(parent.nfo_path).read_text()
            assert not list(target.parent.glob(".organizing-*"))

    asyncio.run(run())


@pytest.mark.parametrize("linked", [False, True])
@pytest.mark.parametrize("suffix", [".srt", ".zh-Hans.forced.ass"])
def test_subtitle_owner(tmp_path, linked, suffix):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            other_video = tmp_path / "original.Extended.mkv"
            if linked:
                source = tmp_path / "extended.bin"
                source.write_bytes(b"extended video")
                other_video.symlink_to(source.name)
            else:
                other_video.write_bytes(b"extended video")
            other_subtitle = other_video.with_suffix(suffix)
            other_subtitle.write_text("extended subtitles")
            subtitle = tmp_path / "original.en.forced.srt"
            subtitle.write_text("original subtitles")

            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()

            assert item.path == str(tmp_path / "New Movie.mkv")
            assert Path(item.path).with_suffix(".en.forced.srt").read_text() == (
                "original subtitles"
            )
            assert other_video.read_bytes() == b"extended video"
            assert other_video.is_symlink() == linked
            assert other_subtitle.read_text() == "extended subtitles"

    asyncio.run(run())


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("absolute", [False, True])
def test_shared_artwork(tmp_path, nested, indexed, absolute):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, "{{title}}/{{title}}")
            if nested:
                await organizer.organize_items(lib, [item.id])
                await item.refresh_from_db()
                parent = await MediaItem.get(id=item.parent_id)
                nfo = Path(parent.nfo_path)
            else:
                nfo = Path(item.nfo_path)
            artwork = nfo.parent / "cover.jpg"
            artwork.write_bytes(b"shared cover")
            _nfo(nfo, "New Movie", extra="<art><poster>cover.jpg</poster></art>")
            other_video = tmp_path / "other.mkv"
            other_video.write_bytes(b"other video")
            other_nfo = other_video.with_suffix(".nfo")
            reference = str(artwork if absolute else artwork.relative_to(tmp_path))
            _nfo(
                other_nfo,
                "Other Movie",
                extra=f"<art><poster>\n  {reference}\n</poster></art>",
            )
            other_content = other_nfo.read_bytes()
            if indexed:
                await MediaItem.create(
                    lib=lib,
                    path=str(other_video),
                    dir=str(tmp_path),
                    name=other_video.stem,
                    nfo_path=str(other_nfo),
                    poster=reference,
                )
            lib.rename_template = "Renamed/{{title}}"

            await organizer.organize_items(lib, [item.id])

            await item.refresh_from_db()
            parent = await MediaItem.get(id=item.parent_id)
            assert item.path == str(tmp_path / "Renamed" / "New Movie.mkv")
            assert artwork.read_bytes() == b"shared cover"
            assert (Path(parent.nfo_path).parent / parent.poster).resolve() == artwork
            metadata = organizer._metadata(Path(parent.nfo_path), lib.lib_type, "movie")
            assert (
                Path(parent.nfo_path).parent / metadata["poster"]
            ).resolve() == artwork
            assert other_nfo.read_bytes() == other_content
            assert not (tmp_path / "Renamed" / "cover.jpg").exists()
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("absolute", [False, True])
def test_whitespace_artwork(tmp_path, absolute):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, "{{title}}/{{title}}")
            artwork = tmp_path / "poster.jpg"
            artwork.write_bytes(b"shared poster")
            reference = str(artwork) if absolute else artwork.name
            _nfo(
                Path(item.nfo_path),
                "New Movie",
                extra=f"<art><poster>\n  {reference}\n</poster></art>",
            )
            if not absolute:
                _nfo(
                    tmp_path / "other.nfo",
                    "Other Movie",
                    extra="<art><poster>poster.jpg</poster></art>",
                )

            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            parent = await MediaItem.get(id=item.parent_id)
            await update_metadata(lib, parent.nfo_path)
            await parent.refresh_from_db()

            expected = tmp_path / "New Movie" / artwork.name if absolute else artwork
            assert (Path(parent.nfo_path).parent / parent.poster).resolve() == expected
            assert expected.read_bytes() == b"shared poster"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("template", "reference", "organized"),
    [
        ("{{title}}", "poster.jpg", True),
        ("{{title}}", "./poster.jpg", True),
        ("{{title}}/{{title}}", "poster.jpg", False),
        ("{{title}}/{{title}}", "absolute", True),
        ("{{title}}/{{title}}", "https://example.com/poster.jpg", True),
    ],
)
def test_nfo_symlink(tmp_path, template, reference, organized):
    async def run():
        async with _database():
            root = tmp_path / "library"
            root.mkdir()
            lib, item = await _movie(root, template)
            artwork = root / "poster.jpg"
            artwork.write_bytes(b"poster")
            value = str(artwork) if reference == "absolute" else reference
            external_nfo = tmp_path / "source.nfo"
            _nfo(
                external_nfo,
                "New Movie",
                extra=f"<art><poster>\n  {value}\n</poster></art>",
            )
            original_content = external_nfo.read_bytes()
            nfo = Path(item.nfo_path)
            nfo.unlink()
            nfo.symlink_to("../source.nfo")
            original_path = item.path

            mapping = await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()

            assert bool(mapping) == organized
            assert (item.path != original_path) == organized
            owner = await MediaItem.get(id=item.parent_id) if item.parent_id else item
            current_nfo = Path(owner.nfo_path)
            assert current_nfo.is_symlink()
            assert current_nfo.resolve() == external_nfo
            assert external_nfo.read_bytes() == original_content
            await update_metadata(lib, current_nfo)
            await owner.refresh_from_db()
            if not reference.startswith("https://"):
                assert (current_nfo.parent / owner.poster).resolve() == artwork
            assert artwork.read_bytes() == b"poster"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("season", "organized"),
    [(None, False), ("1", True), ("\n  01\n", True), ("2", False)],
)
def test_season_nfo_symlink(tmp_path, season, organized):
    async def run():
        async with _database():
            root = tmp_path / "library"
            source = root / "Original"
            source.mkdir(parents=True)
            lib = await MediaLib.create(
                name="Shows",
                dir=str(root),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/Season {{season}}/{{episode_code}}",
            )
            external_nfo = tmp_path / "show.nfo"
            _nfo(
                external_nfo,
                "Show",
                "tvshow",
                f"<season>{season}</season>" if season is not None else "",
            )
            original_content = external_nfo.read_bytes()
            parent_nfo = source / "Original.nfo"
            parent_nfo.symlink_to("../../show.nfo")
            parent = await MediaItem.create(
                lib=lib,
                path=str(source),
                dir=str(source),
                name=source.name,
                nfo_path=str(parent_nfo),
                season=1,
            )
            video = source / "old.mkv"
            video.write_bytes(b"video")
            episode_nfo = video.with_suffix(".nfo")
            _nfo(
                episode_nfo,
                "Pilot",
                "episodedetails",
                "<season>1</season><episode>1</episode>",
            )
            item = await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(source),
                name=video.stem,
                nfo_path=str(episode_nfo),
                season=1,
                episode=1,
            )

            mapping = await organizer.organize_items(lib, [parent.id])
            await parent.refresh_from_db()
            await item.refresh_from_db()

            assert bool(mapping) == organized
            assert (item.path != str(video)) == organized
            assert Path(parent.nfo_path).is_symlink()
            assert Path(parent.nfo_path).resolve() == external_nfo
            assert external_nfo.read_bytes() == original_content
            assert Path(item.path).read_bytes() == b"video"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("listed", [False, True])
def test_download_nfo(tmp_path, listed):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/{{episode_code}}",
            )
            folder = tmp_path / "Show"
            folder.mkdir()
            nfo = folder / "Show.nfo"
            _nfo(nfo, "Show", "tvshow")
            original = nfo.read_bytes()
            parent = await MediaItem.create(
                lib=lib,
                path=str(folder),
                dir=str(folder),
                name=folder.name,
                nfo_path=str(nfo),
                season=1,
            )
            video = folder / "S01E01.mkv"
            video.write_bytes(b"video")
            episode_nfo = video.with_suffix(".nfo")
            _nfo(
                episode_nfo,
                "Pilot",
                "episodedetails",
                "<season>1</season><episode>1</episode>",
            )
            await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(video),
                dir=str(folder),
                name=video.stem,
                nfo_path=str(episode_nfo),
                season=1,
                episode=1,
            )
            downloader = await Downloader.create(name="Test", config="{}", priority=1)
            await DownloadTask.create(
                downloader=downloader,
                name="Show",
                dir=str(tmp_path),
                files=["Show/Show.nfo", "Show/S01E01.mkv", "Show/S01E01.nfo"]
                if listed
                else None,
                state=DownloadState.COMPLETED,
                transfer_lib=lib,
            )

            result = await organizer.organize_items(lib, [parent.id])

            assert result == {}
            assert nfo.read_bytes() == original
            assert video.read_bytes() == b"video"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


def test_indexed_events(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            media_event = await MediaEvent.create(
                lib=lib,
                src_path=item.path,
                event_type="created",
            )
            nfo_event = await MediaEvent.create(
                lib=lib,
                src_path=item.nfo_path,
                event_type="modified",
            )
            unknown_event = await MediaEvent.create(
                lib=lib,
                src_path=str(tmp_path / "unknown.mkv"),
                event_type="created",
            )
            await organizer.organize_items(lib, [item.id])
            assert not await MediaEvent.filter(id=media_event.id).exists()
            assert await MediaEvent.filter(id=nfo_event.id).exists()
            assert await MediaEvent.filter(id=unknown_event.id).exists()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "before", "after"])
def test_cancelled_writer(tmp_path, monkeypatch, failure):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            entered, release = threading.Event(), threading.Event()
            original = organizer._move_files

            def paused_writer(root, payload):
                entered.set()
                assert release.wait(timeout=5)
                if failure == "before":
                    raise OSError("Disk unavailable")
                original(root, payload)
                if failure == "after":
                    raise OSError("Interrupted after movement")

            monkeypatch.setattr(organizer, "_move_files", paused_writer)
            task = asyncio.create_task(organizer.organize_items(lib, [item.id]))
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await MediaEvent.filter(lib=lib, event_type="organize").exists()
            monkeypatch.setattr(organizer, "_move_files", original)
            await organizer.recover_organizing(lib)
            await item.refresh_from_db()
            assert Path(item.path).is_file()
            assert not await MediaEvent.filter(lib=lib, event_type="organize").exists()

    asyncio.run(run())


def test_season_merge(tmp_path):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/{{episode_code}} - {{title}}",
            )
            items, parents = [], []
            for season in (1, 2):
                directory = tmp_path / "Old Show" / f"Season {season}"
                directory.mkdir(parents=True)
                nfo = directory / f"Season {season}.nfo"
                _nfo(nfo, "Show", "tvshow")
                parent = await MediaItem.create(
                    lib=lib,
                    path=str(directory),
                    dir=str(directory),
                    name=directory.name,
                    nfo_path=str(nfo),
                    season=season,
                )
                video = directory / "old.mkv"
                video.write_bytes(b"video")
                nfo = video.with_suffix(".nfo")
                _nfo(
                    nfo,
                    "Episode",
                    "episodedetails",
                    f"<season>{season}</season><episode>1</episode>",
                )
                item = await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(video),
                    dir=str(directory),
                    name=video.stem,
                    nfo_path=str(nfo),
                    season=season,
                    episode=1,
                )
                parents.append(parent)
                items.append(item)
            await organizer.organize_items(lib, [parent.id for parent in parents])
            for season, item in enumerate(items, 1):
                await item.refresh_from_db()
                assert item.path == str(
                    tmp_path / "Show" / f"S0{season}E01 - Episode.mkv"
                )
                assert item.season == season
            assert items[0].parent_id == items[1].parent_id
            parent = await MediaItem.get(id=items[0].parent_id)
            assert parent.season is None
            assert Path(parent.nfo_path).is_file()
            assert (tmp_path / "Old Show" / "Season 2" / "Season 2.nfo").is_file()

    asyncio.run(run())


@pytest.mark.parametrize("seasons", [(1, 2), (0, 2)])
@pytest.mark.parametrize("existing_directory", [False, True])
@pytest.mark.parametrize("source_first", [False, True])
def test_season_split(tmp_path, seasons, existing_directory, source_first):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/Season {{season}}/{{episode_code}}",
            )
            show_directory = tmp_path / "Show"
            directory = (
                show_directory / f"Season {seasons[0]:02d}"
                if existing_directory
                else show_directory
            )
            directory.mkdir(parents=True)
            nfo = directory / f"{directory.name}.nfo"
            _nfo(
                nfo,
                "Show",
                "tvshow",
                "<season>1</season><art><poster>poster.jpg</poster></art>",
            )
            poster = directory / "poster.jpg"
            poster.write_bytes(b"shared poster")
            parent = await MediaItem.create(
                lib=lib,
                path=str(directory),
                dir=str(directory),
                name=directory.name,
                nfo_path=str(nfo),
                season=1,
            )
            items = {}
            # create the source season last when it should be processed first
            creation_order = reversed(seasons) if source_first else seasons
            for season in creation_order:
                video = directory / f"old{season}.mkv"
                video.write_bytes(b"video")
                nfo = video.with_suffix(".nfo")
                _nfo(
                    nfo,
                    "Episode",
                    "episodedetails",
                    f"<season>{season}</season><episode>1</episode>",
                )
                items[season] = await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(video),
                    dir=str(directory),
                    name=video.stem,
                    nfo_path=str(nfo),
                    season=1,
                )

            await organizer.organize_items(lib, [parent.id])

            for season, item in items.items():
                await item.refresh_from_db()
                expected = show_directory / f"Season {season:02d}"
                assert item.path == str(expected / f"S{season:02d}E01.mkv")
                assert item.nfo_path == str(expected / f"S{season:02d}E01.nfo")
                assert Path(item.path).read_bytes() == b"video"
                assert Path(item.nfo_path).is_file()
                assert not (directory / f"old{season}.mkv").exists()
                assert not (directory / f"old{season}.nfo").exists()
                assert item.season == season
                target_parent = await MediaItem.get(id=item.parent_id)
                assert target_parent.path == str(expected)
                assert target_parent.nfo_path == str(expected / f"{expected.name}.nfo")
                assert target_parent.season == season
                assert (expected / target_parent.poster).resolve() == poster
                metadata = organizer._metadata(
                    Path(target_parent.nfo_path), lib.lib_type, "tvshow"
                )
                assert metadata["season"] == season
                assert metadata["poster"] == target_parent.poster
            assert items[seasons[0]].parent_id != items[seasons[1]].parent_id
            if existing_directory:
                assert items[seasons[0]].parent_id == parent.id
                await parent.refresh_from_db()
                assert parent.season == seasons[0]
            else:
                assert not await MediaItem.filter(id=parent.id).exists()
            assert poster.read_bytes() == b"shared poster"
            assert not await MediaEvent.filter(event_type="organize").exists()
            assert (
                await organizer.organize_items(
                    lib, [item.id for item in items.values()]
                )
                == {}
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("source_first", "interrupted", "fallback_season"),
    [(False, False, 1), (True, False, 1), (True, True, 1), (True, True, 0)],
)
def test_split_season_fallback(
    tmp_path, monkeypatch, source_first, interrupted, fallback_season
):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/Season {{season}}/{{episode_code}}",
            )
            directory = tmp_path / "Show" / "Season 02"
            directory.mkdir(parents=True)
            nfo = directory / "Season 02.nfo"
            _nfo(nfo, "Show", "tvshow", f"<season>{fallback_season}</season>")
            parent = await MediaItem.create(
                lib=lib,
                path=str(directory),
                dir=str(directory),
                name=directory.name,
                nfo_path=str(nfo),
                season=fallback_season,
            )
            items = {}
            creation_order = (
                (fallback_season, 2) if source_first else (2, fallback_season)
            )
            for season in creation_order:
                video = directory / f"old{season}.mkv"
                video.write_bytes(b"video")
                nfo = video.with_suffix(".nfo")
                season_element = "<season>2</season>" if season == 2 else ""
                _nfo(
                    nfo,
                    "Episode",
                    "episodedetails",
                    f"{season_element}<episode>1</episode>",
                )
                items[season] = await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(video),
                    dir=str(directory),
                    name=video.stem,
                    nfo_path=str(nfo),
                    season=2 if season == 2 else None,
                    episode=1,
                )

            if interrupted:
                original_plan = organizer._plan
                calls = 0

                async def interrupt_second(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise RuntimeError(
                            "simulated interruption before second season"
                        )
                    return await original_plan(*args, **kwargs)

                with monkeypatch.context() as patch:
                    patch.setattr(organizer, "_plan", interrupt_second)
                    with pytest.raises(RuntimeError, match="simulated interruption"):
                        await organizer.organize_items(lib, [parent.id])

            await organizer.organize_items(lib, [parent.id])

            for season, item in items.items():
                await item.refresh_from_db()
                expected = tmp_path / "Show" / f"Season {season:02d}"
                assert item.path == str(expected / f"S{season:02d}E01.mkv")
                assert item.nfo_path == str(expected / f"S{season:02d}E01.nfo")
                assert item.season == season
                target_parent = await MediaItem.get(id=item.parent_id)
                assert target_parent.season == season
                assert (
                    organizer._metadata(
                        Path(target_parent.nfo_path), lib.lib_type, "tvshow"
                    )["season"]
                    == season
                )
            assert items[2].parent_id == parent.id
            assert items[fallback_season].parent_id != parent.id
            assert (
                await organizer.organize_items(
                    lib, [item.id for item in items.values()]
                )
                == {}
            )
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


def test_shared_image(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, "{{title}}/{{title}}")
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            parent = await MediaItem.get(id=item.parent_id)
            shared = tmp_path / "shared"
            shared.mkdir()
            (shared / "cover.jpg").write_bytes(b"cover")
            _nfo(
                Path(parent.nfo_path),
                "New Movie",
                extra="<art><poster>../shared/cover.jpg</poster></art>",
            )
            parent.poster = "../shared/cover.jpg"
            await parent.save(update_fields=["poster"])
            lib.rename_template = "{{title}}"
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            assert item.poster == "shared/cover.jpg"
            assert (
                "<poster>shared/cover.jpg</poster>" in Path(item.nfo_path).read_text()
            )
            assert (shared / "cover.jpg").is_file()

    asyncio.run(run())


def test_download_source_alias(tmp_path):
    async def run():
        async with _database():
            root = tmp_path / "library"
            root.mkdir()
            lib, item = await _movie(root)
            alias = tmp_path / "downloads"
            alias.symlink_to(root, target_is_directory=True)
            downloader = await Downloader.create(name="Test", config="{}", priority=1)
            await DownloadTask.create(
                downloader=downloader,
                name="original",
                dir=str(alias),
                files=["original.mkv"],
                state=DownloadState.COMPLETED,
            )
            assert await organizer.organize_items(lib, [item.id]) == {}
            assert Path(item.path).read_bytes() == b"video"

    asyncio.run(run())


def test_artwork_symlink(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, "{{title}}/{{title}}")
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            directory = Path(item.path).parent
            (directory / "cover.jpg").write_bytes(b"cover")
            (directory / "poster.jpg").symlink_to("cover.jpg")
            lib.rename_template = "Renamed/{{title}}"
            await organizer.organize_items(lib, [item.id])
            link = tmp_path / "Renamed" / "poster.jpg"
            assert link.is_symlink()
            assert link.resolve() == tmp_path / "Renamed" / "cover.jpg"
            assert link.read_bytes() == b"cover"

    asyncio.run(run())


def test_episode_case_collision(tmp_path):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/{{title}}",
            )
            directory = tmp_path / "Original"
            directory.mkdir()
            nfo = directory / "Original.nfo"
            _nfo(nfo, "Show", "tvshow")
            parent = await MediaItem.create(
                lib=lib,
                path=str(directory),
                dir=str(directory),
                name=directory.name,
                nfo_path=str(nfo),
                season=1,
            )
            for index, title in enumerate(("Pilot", "pilot")):
                video = directory / f"old{index}.mkv"
                video.write_bytes(b"video")
                nfo = video.with_suffix(".nfo")
                _nfo(
                    nfo,
                    title,
                    "episodedetails",
                    "<season>1</season><episode>1</episode>",
                )
                await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(video),
                    dir=str(directory),
                    name=video.stem,
                    nfo_path=str(nfo),
                )
            assert await organizer.organize_items(lib, [parent.id]) == {}
            assert (directory / "old0.mkv").is_file()
            assert (directory / "old1.mkv").is_file()
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


def test_metadata_length(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, "{{title}}/{{title}}")
            artwork = tmp_path / "poster.jpg"
            artwork.write_bytes(b"picture")
            title = "a" * 230
            _nfo(
                Path(item.nfo_path),
                title,
                extra=f"<art><poster>{artwork}</poster></art>",
            )
            assert len(str(tmp_path / title / artwork.name)) > 255
            item.poster = str(artwork)
            await item.save(update_fields=["poster"])

            assert await organizer.organize_items(lib, [item.id]) == {}
            await item.refresh_from_db()
            assert Path(item.path).read_bytes() == b"video"
            assert artwork.read_bytes() == b"picture"
            assert not (tmp_path / title).exists()
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("template", ["{{title}}", "{{title}}/{{title}}"])
def test_subtitle_symlink(tmp_path, absolute, template):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, template)
            subtitle = tmp_path / "original.en.srt"
            subtitle.write_text("subtitles")
            link = tmp_path / "original.zh.srt"
            link.symlink_to(subtitle if absolute else subtitle.name)

            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            target = Path(item.path).with_suffix(".en.srt")
            moved_link = Path(item.path).with_suffix(".zh.srt")
            assert moved_link.is_symlink()
            assert moved_link.resolve() == target.resolve()
            assert moved_link.read_text() == "subtitles"
            assert moved_link.readlink().is_absolute() == absolute

    asyncio.run(run())


@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("different_visibility", [False, True])
def test_parent_merge(tmp_path, visible, different_visibility):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="Merged/{{episode_code}} - {{title}}",
            )
            items, parents = [], []
            for season, title in enumerate(("First Title", "Second Title"), 1):
                directory = tmp_path / f"Original {season}"
                directory.mkdir()
                nfo = directory / f"{directory.name}.nfo"
                image = directory / f"poster{season}.jpg"
                image.write_bytes(b"poster")
                _nfo(
                    nfo,
                    title,
                    "tvshow",
                    f"<season>{season}</season>"
                    f"<art><poster>{image.name}</poster></art>",
                )
                parent = await MediaItem.create(
                    lib=lib,
                    path=str(directory),
                    dir=str(directory),
                    name=directory.name,
                    nfo_path=str(nfo),
                    season=season,
                    visible=(
                        not visible if season == 2 and different_visibility else visible
                    ),
                )
                video = directory / "old.mkv"
                video.write_bytes(b"video")
                nfo = video.with_suffix(".nfo")
                _nfo(
                    nfo,
                    "Episode",
                    "episodedetails",
                    f"<season>{season}</season><episode>1</episode>",
                )
                items.append(
                    await MediaItem.create(
                        lib=lib,
                        parent=parent,
                        path=str(video),
                        dir=str(directory),
                        name=video.stem,
                        nfo_path=str(nfo),
                        season=season,
                    )
                )
                parents.append(parent)

            await organizer.organize_items(lib, [parent.id for parent in parents])
            await items[1].refresh_from_db()
            if different_visibility:
                assert items[1].parent_id == parents[1].id
                assert Path(items[1].path).parent == tmp_path / "Original 2"
                await parents[0].refresh_from_db()
                await parents[1].refresh_from_db()
                assert parents[0].visible == visible
                assert parents[1].visible != visible
                assert not await MediaEvent.filter(event_type="organize").exists()
                return
            parent = await MediaItem.get(id=items[1].parent_id)
            metadata = organizer._metadata(
                Path(parent.nfo_path), lib.lib_type, "tvshow"
            )
            assert parent.title == metadata["title"] == "First Title"
            assert parent.poster == metadata["poster"] == "poster1.jpg"
            assert parent.season == metadata["season"] is None
            assert parent.visible == visible
            assert (Path(parent.path) / parent.poster).read_bytes() == b"poster"

    asyncio.run(run())


def test_hidden_movie(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path, "{{title}}/{{title}}")
            item.visible = False
            await item.save(update_fields=["visible"])
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            parent = await MediaItem.get(id=item.parent_id)
            assert not parent.visible

            # a child under a hidden parent may still have its default visibility
            item.visible = True
            await item.save(update_fields=["visible"])
            lib.rename_template = "Renamed/{{title}}"
            await organizer.organize_items(lib, [item.id])
            await parent.refresh_from_db()
            assert not parent.visible
            lib.rename_template = "{{title}}"
            await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            assert item.parent_id is None
            assert not item.visible

    asyncio.run(run())


@pytest.mark.parametrize("parent_collision", [False, True])
def test_indexed_destination(tmp_path, parent_collision):
    async def run():
        async with _database():
            lib, item = await _movie(
                tmp_path, "{{title}}/{{title}}" if parent_collision else "{{title}}"
            )
            destination = tmp_path / (
                "New Movie" if parent_collision else "New Movie.mkv"
            )
            stale = await MediaItem.create(
                lib=lib,
                path=str(destination),
                dir=str(destination if parent_collision else destination.parent),
                name=destination.stem,
                title="Existing historical record",
            )
            assert not destination.exists()
            assert await organizer.organize_items(lib, [item.id]) == {}
            assert Path(item.path).read_bytes() == b"video"
            assert not destination.exists()
            await stale.refresh_from_db()
            assert stale.title == "Existing historical record"
            assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())


def test_planned_path_collision(tmp_path):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            destination = str(tmp_path / "same-target")
            with pytest.raises(ValueError, match="same database path"):
                await organizer._validate_updates(
                    lib,
                    [
                        {"id": item.id, "path": destination},
                        {"id": None, "path": destination},
                    ],
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    ("operation", "has_comments"),
    [("match", True), ("confirm", True), ("confirm", False)],
    ids=["match", "confirm", "empty"],
)
def test_danmaku_response(tmp_path, monkeypatch, operation, has_comments):
    locks = tmp_path / "locks"
    locks.mkdir()
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _: str(locks))

    async def run():
        async with _database():
            lib, item = await _movie(root, "{{title}}/{{title}}")
            async with library_lock(lib.dir):
                await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            lib.danmaku_server = "https://danmaku.example"
            lib.danmaku_ttl = 0
            await lib.save(update_fields=["danmaku_server", "danmaku_ttl"])
            metadata = DanmakuMeta(anime_id="1", episode_id="2", type="movie")
            old_cache = Path(item.dir) / f".{item.name}.json"
            old_cache.write_text("[]")
            item.danmaku_path = str(old_cache)
            item.danmaku_meta = metadata.model_dump()
            await item.save(update_fields=["danmaku_path", "danmaku_meta"])
            comments = [Danmaku(text="Updated", start=1)] if has_comments else []
            loading = asyncio.Event()
            release = asyncio.Event()
            loaded = asyncio.Event()

            async def load(*args):
                loading.set()
                await release.wait()
                loaded.set()
                return comments

            monkeypatch.setattr(DanmakuService, "load_from_server", load)
            request = asyncio.create_task(
                DanmakuService.match_danmakus(item.path)
                if operation == "match"
                else DanmakuService.confirm_episode(item.path, metadata)
            )
            original_plan = organizer._plan

            async def plan_with_response(*args, **kwargs):
                payload = await original_plan(*args, **kwargs)
                release.set()
                await asyncio.wait_for(loaded.wait(), timeout=1)
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(request), timeout=0.1)
                assert old_cache.read_text() == "[]"
                return payload

            try:
                await asyncio.wait_for(loading.wait(), timeout=1)
                monkeypatch.setattr(organizer, "_plan", plan_with_response)
                lib.rename_template = "Renamed/{{title}}"
                async with await library_lock(lib.dir).acquire(timeout=1):
                    await organizer.organize_items(lib, [item.id])
                result = await asyncio.wait_for(request, timeout=3)

                await item.refresh_from_db()
                new_cache = root / "Renamed" / old_cache.name
                assert item.path == str(root / "Renamed" / "New Movie.mkv")
                assert result.comments == comments
                if has_comments:
                    assert item.danmaku_path == str(new_cache)
                    assert await DanmakuService.load_from_cache(new_cache) == comments
                else:
                    assert item.danmaku_path is None
                    assert not new_cache.exists()
                assert not old_cache.exists()
                assert not await MediaEvent.filter(event_type="organize").exists()
            finally:
                release.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(run())


def test_danmaku_deletion(tmp_path, monkeypatch):
    locks = tmp_path / "locks"
    locks.mkdir()
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _: str(locks))

    async def run():
        async with _database():
            lib, item = await _movie(root, "{{title}}/{{title}}")
            async with library_lock(lib.dir):
                await organizer.organize_items(lib, [item.id])
            await item.refresh_from_db()
            old_cache = Path(item.dir) / f".{item.name}.json"
            old_cache.write_text("[]")
            item.danmaku_path = str(old_cache)
            await item.save(update_fields=["danmaku_path"])
            request = None

            try:
                async with library_lock(lib.dir):
                    request = asyncio.create_task(
                        DanmakuService.delete_danmakus(item.path)
                    )
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(request), timeout=0.1)
                    assert old_cache.exists()
                    lib.rename_template = "Renamed/{{title}}"
                    await organizer.organize_items(lib, [item.id])
                await asyncio.wait_for(request, timeout=3)

                await item.refresh_from_db()
                assert item.path == str(root / "Renamed" / "New Movie.mkv")
                assert item.danmaku_path is None
                assert not old_cache.exists()
                assert not (root / "Renamed" / old_cache.name).exists()
                assert not await MediaEvent.filter(event_type="organize").exists()
            finally:
                if request is not None:
                    if not request.done():
                        request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "operation", ["confirm_anime", "refresh_episodes", "confirm_episode"]
)
def test_danmaku_scope(tmp_path, monkeypatch, operation):
    locks = tmp_path / "locks"
    locks.mkdir()
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _: str(locks))

    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(root),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="Merged/{{episode_code}}",
                danmaku_server="https://danmaku.example",
            )
            parents, items, caches = [], [], []
            for episode, name in enumerate(("Original", "Merged"), 1):
                directory = root / name
                directory.mkdir()
                parent_nfo = directory / f"{name}.nfo"
                _nfo(parent_nfo, "Show", "tvshow", "<season>1</season>")
                parent = await MediaItem.create(
                    lib=lib,
                    path=str(directory),
                    dir=str(directory),
                    name=name,
                    nfo_path=str(parent_nfo),
                    season=1,
                )
                video = directory / f"old{episode}.mkv"
                video.write_bytes(b"video")
                nfo = video.with_suffix(".nfo")
                _nfo(
                    nfo,
                    "Episode",
                    "episodedetails",
                    f"<season>1</season><episode>{episode}</episode>",
                )
                cache = directory / f".{video.stem}.json"
                cache.write_text("[]")
                item = await MediaItem.create(
                    lib=lib,
                    parent=parent,
                    path=str(video),
                    dir=str(directory),
                    name=video.stem,
                    nfo_path=str(nfo),
                    season=1,
                    episode=episode,
                    danmaku_path=str(cache),
                    danmaku_meta={
                        "anime_id": "old",
                        "episode_id": str(episode),
                        "type": "tvseries",
                    },
                )
                parents.append(parent)
                items.append(item)
                caches.append(cache)
            loading, release, loaded = asyncio.Event(), asyncio.Event(), asyncio.Event()
            comments = [Danmaku(text="Updated", start=1)]

            async def load(*args):
                loading.set()
                await release.wait()
                loaded.set()
                return comments

            async def respond(request):
                assert request.url.path == "/api/v2/bangumi/new"
                if operation != "confirm_episode":
                    await load()
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "bangumi": {
                            "episodes": [
                                {"episodeNumber": "1", "episodeId": "new-1"},
                                {"episodeNumber": "2", "episodeId": "new-2"},
                            ]
                        },
                    },
                )

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as client:
                app = SimpleNamespace(ctx=SimpleNamespace(httpx=client))
                monkeypatch.setattr("app.services.danmaku.Sanic.get_app", lambda: app)
                monkeypatch.setattr(DanmakuService, "load_from_server", load)
                meta = DanmakuAnime(anime_id="new", type="tvseries")
                if operation == "confirm_anime":
                    pending = DanmakuService.confirm_anime(parents[0].path, meta)
                elif operation == "refresh_episodes":
                    pending = DanmakuService.refresh_episodes(parents[0], meta)
                else:
                    pending = DanmakuService.confirm_episode(
                        items[0].path,
                        DanmakuMeta(**meta.model_dump(), episode_id="new-1"),
                    )
                request = asyncio.create_task(pending)
                original_plan = organizer._plan

                async def plan_with_response(*args, **kwargs):
                    payload = await original_plan(*args, **kwargs)
                    release.set()
                    await asyncio.wait_for(loaded.wait(), timeout=1)
                    with pytest.raises(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(request), timeout=0.1)
                    assert caches[0].exists()
                    return payload

                try:
                    await asyncio.wait_for(loading.wait(), timeout=1)
                    monkeypatch.setattr(organizer, "_plan", plan_with_response)
                    async with await library_lock(lib.dir).acquire(timeout=1):
                        await organizer.organize_items(lib, [parents[0].id])
                    result = await asyncio.wait_for(request, timeout=3)

                    await items[0].refresh_from_db()
                    await items[1].refresh_from_db()
                    assert items[0].path == str(root / "Merged" / "S01E01.mkv")
                    assert items[0].parent_id == items[1].parent_id == parents[1].id
                    assert items[0].danmaku_meta["episode_id"] == "new-1"
                    new_cache = root / "Merged" / caches[0].name
                    if operation == "confirm_episode":
                        assert result.comments == comments
                        assert items[0].danmaku_path == str(new_cache)
                        assert (
                            await DanmakuService.load_from_cache(new_cache) == comments
                        )
                    else:
                        assert result is True
                        assert items[0].danmaku_path is None
                        assert not new_cache.exists()
                    assert not caches[0].exists()
                    assert items[1].danmaku_path == str(caches[1])
                    assert items[1].danmaku_meta["anime_id"] == "old"
                    assert caches[1].read_text() == "[]"
                    assert not await MediaItem.filter(id=parents[0].id).exists()
                    assert not await MediaEvent.filter(event_type="organize").exists()
                finally:
                    release.set()
                    if not request.done():
                        request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    asyncio.run(run())


def test_danmaku_cancellation(tmp_path, monkeypatch):
    locks = tmp_path / "locks"
    locks.mkdir()
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _: str(locks))
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    async def run():
        async with _database():
            lib, item = await _movie(root)
            lib.danmaku_server = "https://danmaku.example"
            await lib.save(update_fields=["danmaku_server"])
            cache = root / f".{item.name}.json"
            write_bytes = Path.write_bytes

            def delayed_write(path, content):
                if path != cache:
                    return write_bytes(path, content)
                started.set()
                assert release.wait(timeout=5)
                try:
                    return write_bytes(path, content)
                finally:
                    finished.set()

            comments = [Danmaku(text="Updated", start=1)]
            monkeypatch.setattr(Path, "write_bytes", delayed_write)
            monkeypatch.setattr(
                DanmakuService, "load_from_server", AsyncMock(return_value=comments)
            )
            request = asyncio.create_task(
                DanmakuService.confirm_episode(
                    item.path, DanmakuMeta(anime_id="1", episode_id="2", type="movie")
                )
            )
            try:
                assert await asyncio.to_thread(started.wait, 3)
                request.cancel()
                with pytest.raises(Timeout):
                    async with await library_lock(lib.dir).acquire(timeout=0):
                        pass
                assert not finished.is_set()
                assert not request.done()
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(request, timeout=3)

            assert finished.is_set()
            async with await library_lock(lib.dir).acquire(timeout=1):
                assert await DanmakuService.load_from_cache(cache) == comments
                assert not await MediaEvent.filter(event_type="organize").exists()

    asyncio.run(run())
