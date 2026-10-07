"""Tests for video deletion ownership and shared media cleanup."""

import asyncio
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from tortoise import Tortoise
from tortoise.queryset import QuerySet

from app.core.config import KaloscopeConfig
from app.core.media.common import ContentError
from app.models.media import LibType, MediaItem, MediaLib
from app.models.user import HistoryType, User, UserHistory, UserRole
from app.services import media as media_service
from app.services.media import MediaItemService


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Keep locks and filesystem deletion inside the test directory.

    Args:
        tmp_path: The isolated filesystem root.
        monkeypatch: The fixture restoring application configuration.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(cache))
    monkeypatch.setattr(
        KaloscopeConfig, "get", lambda: SimpleNamespace(filesystem_trash_mode=False)
    )


@asynccontextmanager
async def _database() -> AsyncGenerator[None]:
    """Provide an isolated media schema.

    Yields:
        Control while the in-memory database is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


async def _source(tmp_path: Path, layout: str) -> tuple[MediaLib, MediaItem]:
    """Create a flat movie or a directory with two video parts and local artwork.

    Args:
        tmp_path: The isolated filesystem root.
        layout: The flat, movie, show or season layout to create.

    Returns:
        The library and its first video item.
    """
    root = tmp_path / "Library"
    directory = root if layout == "flat" else root / "Work"
    if layout == "season":
        directory /= "Season 01"
    directory.mkdir(parents=True)
    lib = await MediaLib.create(
        name="Video",
        dir=str(root),
        lib_type=LibType.TV_SHOW if layout in {"show", "season"} else LibType.MOVIE,
        priority=1,
    )
    parent = None
    if layout != "flat":
        nfo = directory / f"{directory.name}.nfo"
        nfo.write_text("<tvshow><art><poster>poster.jpg</poster></art></tvshow>")
        (directory / "poster.jpg").write_bytes(b"parent cover")
        parent = await MediaItem.create(
            lib=lib,
            path=str(directory),
            dir=str(directory),
            name=directory.name,
            nfo_path=str(nfo),
        )
    (directory / "art").mkdir()
    names = ["Episode"] if parent is None else ["Episode", "Episode.Extended"]
    items = []
    for name in names:
        path = directory / f"{name}.mkv"
        path.write_bytes(b"video")
        nfo = path.with_suffix(".NFO")
        nfo.write_text(f"<movie><art><poster>art/{name}.jpg</poster></art></movie>")
        (directory / f"art/{name}.jpg").write_bytes(b"cover")
        path.with_suffix(".zh-Hans.forced.SRT").write_bytes(b"subtitles")
        items.append(
            await MediaItem.create(
                lib=lib,
                parent=parent,
                path=str(path),
                dir=str(directory),
                name=name,
                nfo_path=str(nfo),
            )
        )
    (directory / "notes.md").write_text("Keep me")
    (directory / "unrelated.srt").write_text("Keep me")
    return lib, items[0]


@pytest.mark.parametrize(
    ("layout", "group"),
    [("flat", False)]
    + [
        (layout, group)
        for layout in ("movie", "show", "season")
        for group in (False, True)
    ],
)
@pytest.mark.parametrize("local", [False, True])
def test_video_delete(tmp_path, monkeypatch, layout, group, local):
    async def run():
        async with _database():
            lib, item = await _source(tmp_path, layout)
            target = item.parent_id if group else item.id
            assert target is not None
            selected = await MediaItem.all() if group else [item]
            selected_ids = {row.id for row in selected}
            if group:
                await MediaItem.filter(id=item.id).update(visible=False)
            user = await User.create(
                username="user", password="unused", role=UserRole.USER
            )
            for row in await MediaItem.all():
                for kind in (HistoryType.VIDEO, HistoryType.IMAGE):
                    await UserHistory.create(user=user, rel_id=row.id, rel_type=kind)
            expected_history = [
                row.id
                for row in await UserHistory.all()
                if row.rel_type != HistoryType.VIDEO or row.rel_id not in selected_ids
            ]
            files = {
                path: path.read_bytes()
                for path in Path(lib.dir).rglob("*")
                if path.is_file()
            }
            owned = {
                Path(item.path),
                Path(item.nfo_path or ""),
                Path(item.path).with_suffix(".zh-Hans.forced.SRT"),
                Path(item.dir) / "art/Episode.jpg",
            }
            if group:
                owned = {
                    path
                    for path in files
                    if path.name not in {"notes.md", "unrelated.srt"}
                }
            removed = []
            main_thread = threading.get_ident()
            delete = media_service.delete_path

            def remove(path):
                """Verify file deletion runs off the event loop.

                Args:
                    path: The selected body or companion.
                """
                assert threading.get_ident() != main_thread and not path.is_dir()
                removed.append(path)
                delete(path)

            monkeypatch.setattr(media_service, "delete_path", remove)
            await MediaItemService.delete(target, local=local)
            assert Path(lib.dir).is_dir()
            if local:
                assert set(removed) == owned
                assert not await MediaItem.filter(id__in=selected_ids).exists()
                assert (
                    await UserHistory.all().values_list("id", flat=True)
                    == expected_history
                )
            else:
                assert not removed
                assert not await MediaItem.filter(
                    id__in=selected_ids, visible=True
                ).exists()
                assert (
                    await UserHistory.all().count() == 2 * await MediaItem.all().count()
                )
            for path, content in files.items():
                assert (
                    not path.exists()
                    if local and path in owned
                    else path.read_bytes() == content
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "shared",
    [
        "version",
        "missing_version",
        "nfo",
        "broken_nfo",
        "link",
        "nfo_link",
        "directory_link",
    ],
)
def test_video_shared_files(tmp_path, shared):
    async def run():
        async with _database():
            lib, item = await _source(tmp_path, "flat")
            video = Path(item.path)
            cover = Path(item.dir) / "art/Episode.jpg"
            subtitle = video.with_suffix(".zh-Hans.forced.SRT")
            nfo = Path(item.nfo_path or "")
            preserve = set()
            if shared in {"version", "missing_version"}:
                other = video.with_suffix(".mp4")
                if shared == "version":
                    other.write_bytes(b"other video")
                else:
                    await MediaItem.create(
                        lib=lib, path=str(other), dir=item.dir, name=other.stem
                    )
                preserve.update({nfo, subtitle, cover})
            elif shared in {"nfo", "broken_nfo"}:
                (Path(lib.dir) / "other.nfo").write_text(
                    "<movie><art><poster>art/Episode.jpg</poster></art></movie>"
                    if shared == "nfo"
                    else "<broken>"
                )
                preserve.add(cover)
            else:
                alias = Path(lib.dir) / "alias"
                alias.symlink_to(
                    nfo
                    if shared == "nfo_link"
                    else subtitle
                    if shared == "link"
                    else cover.parent,
                    target_is_directory=shared == "directory_link",
                )
                preserve.update(
                    {nfo, cover}
                    if shared == "nfo_link"
                    else {subtitle if shared == "link" else cover}
                )
            before = {path: path.read_bytes() for path in preserve}
            await MediaItemService.delete(item.id, local=True)
            assert (
                not video.exists() and not await MediaItem.filter(id=item.id).exists()
            )
            assert all(path.read_bytes() == content for path, content in before.items())

    asyncio.run(run())


@pytest.mark.parametrize("layout", ["flat", "movie", "season"])
def test_video_empty_directory(tmp_path, layout):
    async def run():
        async with _database():
            lib, item = await _source(tmp_path, layout)
            directory = Path(item.dir)
            (directory / "notes.md").unlink()
            (directory / "unrelated.srt").unlink()
            await MediaItemService.delete(item.parent_id or item.id, local=True)
            assert not await MediaItem.exists()
            assert Path(lib.dir).is_dir()
            assert not (directory / "art").exists()
            if layout != "flat":
                assert not directory.exists()
            else:
                assert not list(directory.iterdir())

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["subtitle", "database", "new_video", "replace_video", "cancel"]
)
def test_video_delete_retry(tmp_path, monkeypatch, failure):
    async def run():
        async with _database():
            _, item = await _source(tmp_path, "flat")
            video = Path(item.path)
            subtitle = video.with_suffix(".zh-Hans.forced.SRT")
            remove = media_service.delete_path
            delete = QuerySet.delete
            write = media_service.write_in_thread

            def interrupted(path):
                """Simulate partial deletion or a source appearing during cleanup.

                Args:
                    path: The next selected file.
                """
                if failure == "subtitle" and path == subtitle:
                    raise PermissionError("unavailable")
                remove(path)
                if path == video and failure in {"new_video", "replace_video"}:
                    target = (
                        video.with_suffix(".mp4") if failure == "new_video" else video
                    )
                    target.write_bytes(b"new video")

            async def failed_delete(query):
                """Fail the media transaction after history cleanup.

                Args:
                    query: The pending deletion query.

                Returns:
                    The original deletion count for other models.
                """
                if query.model is MediaItem:
                    raise RuntimeError("database failure")
                return await delete(query)

            async def cancelled(function, *args, **kwargs):
                """Cancel only after the active filesystem writer returns.

                Args:
                    function: The selected filesystem operation.
                    *args: Its positional arguments.
                    **kwargs: Its keyword arguments.

                Returns:
                    The original result for other operations.
                """
                result = await write(function, *args, **kwargs)
                if function is media_service._delete_media_files:
                    raise asyncio.CancelledError
                return result

            user = await User.create(
                username="user", password="unused", role=UserRole.USER
            )
            history = await UserHistory.create(
                user=user, rel_id=item.id, rel_type=HistoryType.VIDEO
            )
            with monkeypatch.context() as patch:
                patch.setattr(media_service, "delete_path", interrupted)
                if failure == "database":
                    patch.setattr(QuerySet, "delete", failed_delete)
                elif failure == "cancel":
                    patch.setattr(media_service, "write_in_thread", cancelled)
                expected = (
                    asyncio.CancelledError
                    if failure == "cancel"
                    else RuntimeError
                    if failure == "database"
                    else ContentError
                )
                with pytest.raises(expected):
                    await MediaItemService.delete(item.id, local=True)
            assert await MediaItem.filter(id=item.id).exists()
            assert await UserHistory.filter(id=history.id).exists()
            if failure in {"new_video", "replace_video"}:
                target = video.with_suffix(".mp4") if failure == "new_video" else video
                assert target.read_bytes() == b"new video"
                assert Path(item.nfo_path or "").exists() and subtitle.exists()
                target.unlink()
            await MediaItemService.delete(item.id, local=True)
            assert not await MediaItem.exists() and not await UserHistory.exists()
            assert not video.exists() and not subtitle.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "guard", ["outside", "directory_link", "shared_body", "companion_link", "body_link"]
)
def test_video_delete_boundary(tmp_path, guard):
    async def run():
        async with _database():
            lib, item = await _source(tmp_path, "flat")
            video = Path(item.path)
            outside = tmp_path / "outside.mkv"
            outside.write_bytes(b"keep")
            if guard == "outside":
                item.path, item.dir = str(outside), str(outside.parent)
                await item.save(update_fields=["path", "dir"])
            elif guard == "directory_link":
                root = Path(lib.dir)
                root.rename(tmp_path / "actual")
                root.symlink_to(tmp_path / "actual", target_is_directory=True)
            elif guard == "shared_body":
                (Path(lib.dir) / "other.mkv").symlink_to(video.name)
            elif guard == "companion_link":
                subtitle = video.with_suffix(".linked.srt")
                subtitle.symlink_to(outside)
            else:
                video.unlink()
                video.symlink_to(outside)
            if guard in {"outside", "directory_link", "shared_body"}:
                with pytest.raises(ContentError):
                    await MediaItemService.delete(item.id, local=True)
                assert await MediaItem.filter(id=item.id).exists()
            else:
                await MediaItemService.delete(item.id, local=True)
                assert not video.exists() and not video.is_symlink()
                if guard == "companion_link":
                    assert video.with_suffix(".linked.srt").is_symlink()
            assert outside.read_bytes() == b"keep"

    asyncio.run(run())
