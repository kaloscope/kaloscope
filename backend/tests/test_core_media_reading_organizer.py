"""Tests for reading organization, source preservation and journal recovery."""

import asyncio
import base64
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from lxml import etree
from sanic import Sanic
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.media import events, organizer, watcher
from app.core.media.coordination import library_lock
from app.core.media.handlers.reading import ReadingMediaHandler
from app.core.media.metadata import ReadingMetadata, render_metadata
from app.models.download import Downloader, DownloadState, DownloadTask
from app.models.media import (
    IndexState,
    LibType,
    MediaEvent,
    MediaFormat,
    MediaItem,
    MediaLib,
)
from app.models.user import HistoryEntry, User, UserHistory, UserRole
from app.services import media as media_service
from app.services.flow import FlowTriggerService
from app.services.media import MediaItemService
from tests.test_services_media_reading import _user
from tests.test_services_user_history import _entry

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII="
)


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Keep locks, caches and generated covers inside the isolated test directory.

    Args:
        tmp_path: The isolated test root.
        monkeypatch: The fixture restoring application configuration.
    """
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(cache))


@asynccontextmanager
async def _database():
    """Provide an isolated media schema without starting application workers.

    Yields:
        Control while the in-memory database is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


def _source(directory: Path, format: MediaFormat, title: str):
    """Write a readable body and its external metadata.

    Args:
        directory: The owned work or chapter directory.
        format: The reading source format to create.
        title: The metadata title used for both external and embedded sources.
    """
    directory.mkdir(parents=True, exist_ok=True)
    novel = format in (MediaFormat.TXT, MediaFormat.EPUB)
    metadata = render_metadata(
        ReadingMetadata(title=title, authors=("Writer",)),
        "opf" if novel else "comicinfo",
        identifier=uuid4(),
    )
    (directory / ("metadata.opf" if novel else "ComicInfo.xml")).write_bytes(metadata)
    (directory / "cover.png").write_bytes(_PNG)
    if format == MediaFormat.DIR:
        for name in ("01.png", "02.png"):
            (directory / name).write_bytes(_PNG)
        return
    path = directory / f"old.{format.value.upper()}"
    if format == MediaFormat.TXT:
        path.write_text(
            "第一章 开始\n\nHello\n\n第二章 后续\n\nWorld", encoding="utf-8"
        )
        return
    with ZipFile(path, "w") as archive:
        if format == MediaFormat.EPUB:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr(
                "META-INF/container.xml",
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" '
                'version="1.0">'
                '<rootfiles><rootfile full-path="book.opf" '
                'media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
            tree = etree.fromstring(metadata)
            namespace = "{http://www.idpf.org/2007/opf}"
            manifest = tree.find(f"{namespace}manifest")
            assert manifest is not None
            etree.SubElement(
                manifest,
                f"{namespace}item",
                id="body",
                href="body.xhtml",
                attrib={"media-type": "application/xhtml+xml"},
            )
            spine = tree.find(f"{namespace}spine")
            assert spine is not None
            etree.SubElement(spine, f"{namespace}itemref", idref="body")
            archive.writestr("book.opf", etree.tostring(tree))
            archive.writestr(
                "body.xhtml",
                '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hello</p></body></html>',
            )
        else:
            archive.writestr("ComicInfo.xml", metadata)
            archive.writestr("01.png", _PNG)
            archive.writestr("02.png", _PNG)


async def _work(tmp_path: Path, format: MediaFormat) -> tuple[MediaLib, MediaItem]:
    """Index a standalone work through the normal reading service.

    Args:
        tmp_path: The isolated source and cache root.
        format: The reading format to create.

    Returns:
        The configured library and its ready work.
    """
    root = tmp_path / "library"
    directory = root / "original"
    _source(directory, format, "New Work")
    lib = await MediaLib.create(
        name="Reading",
        dir=str(root),
        lib_type=LibType.NOVEL
        if format in (MediaFormat.TXT, MediaFormat.EPUB)
        else LibType.COMIC,
        priority=1,
        rename_template="{{collection_title}}/{{title}} - {{authors}}",
    )
    assert await MediaItemService.ingest_reading_work(lib.id, directory) == {}
    return lib, await MediaItem.get(lib_id=lib.id)


@pytest.mark.parametrize("format", list(MediaFormat))
def test_standalone(tmp_path, format):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, format)
            source = Path(item.path)
            original = {
                path.name: path.read_bytes() for path in Path(item.dir).iterdir()
            }
            user = await User.create(
                username="reader", password="unused", role=UserRole.ADMIN
            )
            entry = await _entry(item)
            history = await UserHistory.create(user=user, **entry)
            locator = history.locator
            async with library_lock(lib.dir):
                mapping = await organizer.organize_items(lib, [item.id])
            target_dir = Path(lib.dir) / "New Work"
            target = (
                target_dir
                if format == MediaFormat.DIR
                else target_dir / f"New Work - Writer.{format.value.upper()}"
            )
            await item.refresh_from_db()
            await history.refresh_from_db()
            assert item.path == str(target) and item.dir == str(target_dir)
            assert item.index_state == IndexState.PENDING
            assert mapping[str(source)] == str(target)
            assert history.rel_id == item.id and history.locator == locator
            assert not source.exists()
            for name, content in original.items():
                name = target.name if name.startswith("old.") else name
                assert (target_dir / name).read_bytes() == content
            event = await MediaEvent.get(event_type="modified")
            assert event.src_path == str(target_dir) and event.is_directory
            assert await MediaItemService.ingest_reading_work(lib.id, target_dir) == {}
            await item.refresh_from_db()
            assert item.index_state == IndexState.READY
            assert await MediaItem.filter(lib_id=lib.id).count() == 1
            saved = HistoryEntry.model_validate(entry)
            assert saved.locator is not None
            resumed = await MediaItemService.get_locator(
                item.id, _user(), saved.locator, restore=True
            )
            assert resumed is not None and resumed.version == item.index_version
            async with library_lock(lib.dir):
                assert await organizer.organize_items(lib, [item.id]) == {}
            assert await MediaEvent.filter(event_type="modified").count() == 1

    asyncio.run(run())


def test_collection(tmp_path):
    async def run():
        async with _database():
            root = tmp_path / "library"
            source_dir = root / "original"
            source_dir.mkdir(parents=True)
            (source_dir / "ComicInfo.xml").write_bytes(
                render_metadata(
                    ReadingMetadata(title="Series", authors=("Work author",)),
                    "comicinfo",
                    identifier=uuid4(),
                )
            )
            for format in (MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP):
                _source(source_dir / format.value, format, format.value.upper())
            (source_dir / "notes.md").write_text("Keep this file", encoding="utf-8")
            lib = await MediaLib.create(
                name="Comics",
                dir=str(root),
                lib_type=LibType.COMIC,
                priority=1,
                rename_template="{{collection_title}}/{{title}}",
            )
            assert await MediaItemService.ingest_reading_work(lib.id, source_dir) == {}
            work = await MediaItem.get(lib_id=lib.id, parent_id=None)
            chapters = await MediaItem.filter(parent_id=work.id)
            original = {
                path.relative_to(source_dir): path.read_bytes()
                for path in source_dir.rglob("*")
                if path.is_file()
            }
            async with library_lock(lib.dir):
                mapping = await organizer.organize_items(
                    lib, [chapter.id for chapter in chapters] + [work.id]
                )
            target = root / "Series"
            await work.refresh_from_db()
            assert work.path == str(target) and work.format is None
            for chapter in chapters:
                await chapter.refresh_from_db()
                assert chapter.parent_id == work.id and chapter.format is not None
                directory = target / chapter.format.value.upper()
                expected = (
                    directory
                    if chapter.format == MediaFormat.DIR
                    else directory
                    / f"{chapter.format.value.upper()}.{chapter.format.value.upper()}"
                )
                assert chapter.path == str(expected)
            for path, content in original.items():
                assert (
                    Path(
                        mapping.get(str(source_dir / path), str(source_dir / path))
                    ).read_bytes()
                    == content
                )
            assert await MediaEvent.filter(event_type="modified").count() == 1
            assert await MediaItemService.ingest_reading_work(lib.id, target) == {}
            assert await MediaItem.filter(lib_id=lib.id).count() == 4
            assert not source_dir.exists()

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["first_move", "after_files"])
def test_opf_recovery(tmp_path, monkeypatch, interruption):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, MediaFormat.TXT)
            directory = Path(item.dir)
            opf = directory / "old.opf"
            original_opf = directory / "metadata.opf"
            original_opf.rename(opf)
            tree = etree.parse(opf)
            manifest = etree.SubElement(
                tree.getroot(), "{http://www.idpf.org/2007/opf}manifest"
            )
            etree.SubElement(
                manifest, "{http://www.idpf.org/2007/opf}item", href="old.TXT#start"
            )
            etree.SubElement(tree.getroot(), "custom").text = "Preserved"
            opf.write_bytes(etree.tostring(tree))
            move = organizer._move_files

            def interrupt(root, payload):
                if interruption == "first_move":
                    move(root, {**payload, "moves": payload["moves"][:1], "edits": []})
                else:
                    move(root, payload)
                raise OSError("interrupted")

            monkeypatch.setattr(organizer, "_move_files", interrupt)
            with pytest.raises(organizer.OrganizePendingError):
                await organizer.organize_items(lib, [item.id])
            assert await MediaEvent.filter(event_type="organize").count() == 1
            monkeypatch.setattr(organizer, "_move_files", move)
            lib.rename_template = None
            await lib.save(update_fields=["rename_template"])
            async with library_lock(lib.dir):
                await organizer.recover_organizing(lib)
            await item.refresh_from_db()
            destination = Path(item.dir) / "New Work - Writer.opf"
            assert not opf.exists() and destination.exists()
            tree = etree.parse(destination)
            assert tree.findtext("custom") == "Preserved"
            assert tree.xpath("//*[local-name()='item']/@href") == [
                "New%20Work%20-%20Writer.TXT#start"
            ]
            assert not await MediaEvent.filter(event_type="organize").exists()
            assert await MediaEvent.filter(event_type="modified").count() == 1
            assert (
                await MediaItemService.ingest_reading_work(lib.id, Path(item.dir)) == {}
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "problem", ["target", "missing_title", "unindexed", "symlink", "hidden"]
)
def test_invalid_work(tmp_path, problem):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, MediaFormat.TXT)
            original_path = item.path
            directory = Path(item.dir)
            if problem == "target":
                target = Path(lib.dir) / "New Work"
                target.mkdir()
                (target / "keep.txt").write_text("Keep", encoding="utf-8")
            elif problem == "missing_title":
                (directory / "metadata.opf").write_text(
                    "<package><metadata/></package>", encoding="utf-8"
                )
            elif problem == "unindexed":
                (directory / "extra.txt").write_text("Another novel", encoding="utf-8")
            elif problem == "symlink":
                (directory / "link.md").symlink_to(Path(item.path))
            else:
                lib.rename_template = ".hidden/{{title}}"
            before = {
                path: path.read_bytes()
                for path in directory.iterdir()
                if path.is_file()
            }
            async with library_lock(lib.dir):
                assert await organizer.organize_items(lib, [item.id]) == {}
            await item.refresh_from_db()
            assert item.path == original_path
            assert not await MediaEvent.filter(event_type="organize").exists()
            for path, content in before.items():
                assert path.read_bytes() == content

    asyncio.run(run())


def test_unused_unit_name(tmp_path):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, MediaFormat.DIR)
            lib.rename_template = "{{collection_title}}/{{number}}"
            async with library_lock(lib.dir):
                assert await organizer.organize_items(lib, [item.id])
            scan = ReadingMediaHandler(lib.lib_type).scan_sources(lib.dir)
            assert len(scan.sources) == 1 and not scan.issues
            assert scan.sources[0].format == MediaFormat.DIR

    asyncio.run(run())


@pytest.mark.parametrize("state", ["original", "pending", "complete"])
def test_download_source(tmp_path, state):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, MediaFormat.TXT)
            source = Path(item.path)
            downloads = source.parent if state == "original" else tmp_path / "downloads"
            if state != "original":
                downloads.mkdir()
                (downloads / source.name).hardlink_to(source)
            downloader = await Downloader.create(name="Test", config="{}", priority=1)
            task = await DownloadTask.create(
                downloader=downloader,
                name="Reading",
                dir=str(downloads),
                files=[source.name, "next.txt"]
                if state == "pending"
                else [source.name],
                state=DownloadState.COMPLETED,
                transfer_lib=lib,
                transfer_targets={source.name: str(source)},
                transfer_pending=state == "pending",
            )
            original = source.read_bytes()
            async with library_lock(lib.dir):
                if state == "pending":
                    with pytest.raises(organizer.OrganizeDeferredError):
                        await organizer.organize_items(lib, [item.id])
                else:
                    mapping = await organizer.organize_items(lib, [item.id])
                    assert bool(mapping) == (state == "complete")
            await item.refresh_from_db()
            await task.refresh_from_db()
            assert (downloads / source.name).read_bytes() == original
            assert Path(item.path).read_bytes() == original
            assert task.transfer_targets == {source.name: item.path}
            assert not await MediaEvent.filter(event_type="organize").exists()
            if state == "pending":
                event = await MediaEvent.create(
                    lib_id=lib.id,
                    event_type="ingest",
                    src_path=item.path,
                    payload={"bootparams": [], "organize_ids": [item.id]},
                )
                with pytest.raises(organizer.OrganizeDeferredError):
                    await watcher.consume_event(event)
                await event.refresh_from_db()
                assert event.payload is not None and "error_code" not in event.payload
                await DownloadTask.filter(id=task.id).update(
                    files=[source.name], transfer_pending=False
                )
                assert await watcher.consume_event(event)
                await item.refresh_from_db()
                await task.refresh_from_db()
                assert item.path != str(source)
                assert task.transfer_targets == {source.name: item.path}
                assert not await MediaEvent.filter(event_type="ingest").exists()

    asyncio.run(run())


def test_source_change(tmp_path, monkeypatch):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, MediaFormat.TXT)
            read = organizer._reading_metadata
            source = Path(item.path)

            def changed(source):
                result = read(source)
                source.path.write_text(
                    "Changed after reading metadata", encoding="utf-8"
                )
                return result

            monkeypatch.setattr(organizer, "_reading_metadata", changed)
            async with library_lock(lib.dir):
                assert await organizer.organize_items(lib, [item.id]) == {}
            assert (
                source.read_text(encoding="utf-8") == "Changed after reading metadata"
            )
            assert not await MediaEvent.filter(event_type="organize").exists()
            await item.refresh_from_db()
            assert item.path == str(source)

    asyncio.run(run())


@pytest.mark.parametrize("format", list(MediaFormat))
def test_saved_work(tmp_path, monkeypatch, format):
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        async with _database():
            lib, item = await _work(tmp_path, format)
            original_path = item.path
            assert await MediaItemService.save_metadata(
                item.id, {"title": "Saved", "authors": ["Writer"]}, overwrite=True
            )
            event = await MediaEvent.get(event_type="ingest")
            assert event.payload == {"bootparams": [], "organize_ids": [item.id]}
            assert (await MediaItem.get(id=item.id)).path == original_path
            assert await watcher.consume_event(event)
            await item.refresh_from_db()
            assert item.path != original_path and item.title == "Saved"
            assert not await watcher.consume_event(event)
            tasks = await events.coalesce_reading_events(lib.id)
            assert len(tasks) == 1
            task = tasks[0]
            assert not await events.prepare_reading_event(task.id)
            await task.refresh_from_db()
            assert task.payload is not None
            now = task.payload["not_before"] + 1
            monkeypatch.setattr(events, "time", lambda: now)
            monkeypatch.setattr(watcher, "time", lambda: now)
            assert await events.prepare_reading_event(task.id)
            task = await MediaEvent.get(id=task.id).select_related("lib")
            assert await watcher.consume_event(task)
            await item.refresh_from_db()
            assert item.index_state == IndexState.READY
            assert not await MediaEvent.all().exists()
            details = await MediaItemService.get_details(item.id, _user())
            assert details["metadata"]["title"] == "Saved"
            fire.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["existing", "external", "disabled"])
def test_save_without_organization(tmp_path, change):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, MediaFormat.TXT)
            original_path = item.path
            assert not await MediaEvent.filter(event_type="ingest").exists()
            if change == "external":
                _source(Path(item.dir), MediaFormat.TXT, "External")
                await MediaItemService.sync_metadata(item.id)
            else:
                if change == "disabled":
                    await MediaLib.filter(id=lib.id).update(rename_template=None)
                assert await MediaItemService.save_metadata(
                    item.id, {"title": "Candidate"}, overwrite=change == "disabled"
                ) == (change == "disabled")
            assert (
                await MediaItemService.ingest_reading_work(lib.id, Path(item.dir)) == {}
            )
            assert not await MediaEvent.all().exists()
            assert (await MediaItem.get(id=item.id)).path == original_path

    asyncio.run(run())


@pytest.mark.parametrize("format", [MediaFormat.TXT, MediaFormat.DIR])
@pytest.mark.parametrize("stage", ["none", "published", "summary", "handoff"])
def test_metadata_handoff(tmp_path, monkeypatch, format, stage):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, format)
            name = "metadata.opf" if format == MediaFormat.TXT else "ComicInfo.xml"
            (Path(item.dir) / name).unlink()
            save = MediaEvent.save
            create = MediaEvent.create

            async def fail_summary(*args, **kwargs):
                raise RuntimeError("interrupted summary")

            async def fail_flag(event, *args, **kwargs):
                if event.event_type == "metadata" and (event.payload or {}).get(
                    "published"
                ):
                    raise RuntimeError("interrupted publication flag")
                return await save(event, *args, **kwargs)

            async def fail_handoff(cls, *args, **kwargs):
                if kwargs.get("event_type") == "ingest":
                    raise RuntimeError("interrupted handoff")
                return await create(*args, **kwargs)

            with monkeypatch.context() as patch:
                if stage == "summary":
                    patch.setattr(media_service, "_save_summary", fail_summary)
                elif stage == "published":
                    patch.setattr(MediaEvent, "save", fail_flag)
                elif stage == "handoff":
                    patch.setattr(MediaEvent, "create", classmethod(fail_handoff))
                if stage == "none":
                    assert await MediaItemService.save_metadata(
                        item.id, {"title": "Automatic", "authors": ["Writer"]}
                    )
                else:
                    with pytest.raises(RuntimeError, match="interrupted"):
                        await MediaItemService.save_metadata(
                            item.id, {"title": "Automatic", "authors": ["Writer"]}
                        )
            if stage != "none":
                pending = await MediaEvent.get(event_type="metadata")
                assert not await MediaEvent.filter(event_type="ingest").exists()
                assert await watcher.consume_event(pending)
                assert not await watcher.consume_event(pending)
            event = await MediaEvent.get(event_type="ingest")
            assert await watcher.consume_event(event)
            await item.refresh_from_db()
            assert item.dir == str(Path(lib.dir) / "Automatic")
            assert not await MediaEvent.filter(
                event_type__in=["ingest", "organize", "metadata"]
            ).exists()

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["files", "database"])
def test_organization_restart(tmp_path, monkeypatch, stage):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, MediaFormat.TXT)
            assert await MediaItemService.save_metadata(
                item.id, {"title": "Saved", "authors": ["Writer"]}, overwrite=True
            )
            event = await MediaEvent.get(event_type="ingest")
            move = organizer._move_files
            delete = MediaEvent.delete

            def interrupted(root, payload):
                move(root, {**payload, "moves": payload["moves"][:1], "edits": []})
                raise OSError("interrupted move")

            async def fail_commit(current, *args, **kwargs):
                if current.event_type == "organize":
                    raise RuntimeError("interrupted commit")
                return await delete(current, *args, **kwargs)

            with monkeypatch.context() as patch:
                if stage == "files":
                    patch.setattr(organizer, "_move_files", interrupted)
                else:
                    patch.setattr(MediaEvent, "delete", fail_commit)
                with pytest.raises(organizer.OrganizePendingError):
                    await watcher.consume_event(event)
            await event.refresh_from_db()
            assert event.payload is not None and "error_code" not in event.payload
            await MediaLib.filter(id=lib.id).update(rename_template=None)
            await lib.refresh_from_db()
            monitor = watcher.LibWatcher(Mock(spec=Sanic))
            queue = await monitor._create_events(lib)
            assert not queue.empty()
            assert not await MediaEvent.filter(event_type="organize").exists()
            assert await watcher.consume_event(event)
            await item.refresh_from_db()
            assert item.dir == str(Path(lib.dir) / "Saved")
            assert not await MediaEvent.filter(event_type="ingest").exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "payload",
    [
        {"organize_ids": [True], "bootparams": []},
        {"organize_ids": [], "bootparams": []},
        {"organize_ids": [1], "bootparams": [{"item_id": 1}]},
    ],
)
def test_organization_payload(tmp_path, payload):
    async def run():
        async with _database():
            lib, item = await _work(tmp_path, MediaFormat.TXT)
            event = await MediaEvent.create(
                lib_id=lib.id, event_type="ingest", src_path=item.path, payload=payload
            )
            with pytest.raises(ValueError, match="organization parameters"):
                await watcher.consume_event(event)
            await event.refresh_from_db()
            assert event.payload is not None
            assert event.payload["error_code"] == "invalid_metadata"
            assert not await watcher.consume_event(event)
            assert Path(item.path).is_file()

    asyncio.run(run())
