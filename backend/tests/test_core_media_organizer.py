"""Filesystem and database behavior of metadata-driven media organization."""

import asyncio
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from tortoise import Tortoise

from app.core.media import organizer
from app.models.download import Downloader, DownloadState, DownloadTask
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import HistoryType, User, UserHistory, UserRole


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


def test_recovery(tmp_path, monkeypatch):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
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
            monkeypatch.setattr(organizer, "rename_exclusive", original_rename)
            await organizer.recover_organizing(lib)
            await item.refresh_from_db()
            assert item.path == str(tmp_path / "New Movie.mkv")
            assert Path(item.nfo_path).is_file()
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


def test_cancelled_writer(tmp_path, monkeypatch):
    async def run():
        async with _database():
            lib, item = await _movie(tmp_path)
            entered, release = threading.Event(), threading.Event()
            original = organizer._move_files

            def paused_writer(root, payload):
                entered.set()
                release.wait(timeout=5)
                original(root, payload)

            monkeypatch.setattr(organizer, "_move_files", paused_writer)
            task = asyncio.create_task(organizer.organize_items(lib, [item.id]))
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0.02)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            monkeypatch.setattr(organizer, "_move_files", original)
            await organizer.recover_organizing(lib)
            await item.refresh_from_db()
            assert Path(item.path).is_file()

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
def test_season_split(tmp_path, seasons):
    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Shows",
                dir=str(tmp_path),
                lib_type=LibType.TV_SHOW,
                priority=1,
                rename_template="{{show_title}}/Season {{season}}/{{episode_code}}",
            )
            directory = tmp_path / "Show"
            directory.mkdir()
            nfo = directory / "Show.nfo"
            _nfo(
                nfo,
                "Show",
                "tvshow",
                "<season>1</season><art><poster>poster.jpg</poster></art>",
            )
            (directory / "poster.jpg").write_bytes(b"shared poster")
            parent = await MediaItem.create(
                lib=lib,
                path=str(directory),
                dir=str(directory),
                name=directory.name,
                nfo_path=str(nfo),
                season=1,
            )
            items = []
            for season in seasons:
                video = directory / f"old{season}.mkv"
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
                        season=1,
                    )
                )
            await organizer.organize_items(lib, [parent.id])
            for season, item in zip(seasons, items, strict=True):
                await item.refresh_from_db()
                expected = directory / f"Season 0{season}"
                assert item.path == str(expected / f"S0{season}E01.mkv")
                assert item.season == season
                target_parent = await MediaItem.get(id=item.parent_id)
                assert target_parent.season == season
                assert target_parent.poster == "../poster.jpg"
                assert "../poster.jpg" in Path(target_parent.nfo_path).read_text()
                assert (
                    organizer._metadata(
                        Path(target_parent.nfo_path), lib.lib_type, "tvshow"
                    )["season"]
                    == season
                )
            assert items[0].parent_id != items[1].parent_id
            assert not await MediaItem.filter(id=parent.id).exists()
            assert (directory / "poster.jpg").read_bytes() == b"shared poster"
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
