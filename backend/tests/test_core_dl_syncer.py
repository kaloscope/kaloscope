"""Unit tests for download synchronization."""

import asyncio
import errno
import hashlib
import json
import mimetypes
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from sanic import Sanic
from torrentool.bencode import Bencode
from tortoise import Tortoise
from tortoise.exceptions import OperationalError
from tortoise.queryset import QuerySet

from app.core.config import KaloscopeConfig
from app.core.constants import NFO_MIME_TYPE
from app.core.dl import syncer
from app.core.dl.config import load_driver
from app.core.dl.driver import (
    DownloadAction,
    DownloaderDriver,
    DownloadIdentity,
    DownloadRequest,
    DownloadSnapshot,
)
from app.core.dl.openlist import puller
from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.driver import OpenListDriver
from app.core.dl.openlist.manifest import (
    RemoteManifestEntry,
    manifest_fingerprint,
    serialize_manifest,
)
from app.core.dl.openlist.models import (
    OpenListAuth,
    OpenListConfig,
    OpenListErrorKind,
    RemoteCleanupPolicy,
    RemoteEntry,
    RemoteEntryPage,
)
from app.core.dl.rpc import RpcClient, RpcConfig, RpcDriver
from app.core.dl.rpc.models import API, Method
from app.models.download import (
    Downloader,
    DownloadPlan,
    DownloadPlanHistory,
    DownloadState,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
    TransferMethod,
)
from app.models.flow import FlowGraph, GraphCategory, GraphState
from app.models.general import Notification
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.services import download as download_service


def _openlist_runner(downloader, driver):
    runner = cast(Any, object.__new__(syncer.DLSyncer))
    runner._app = SimpleNamespace(
        shared_ctx=SimpleNamespace(
            dl_task_actions={}, dl_sync_fast=SimpleNamespace(is_set=lambda: True)
        )
    )
    runner._last_sync_tasks = datetime.now()
    runner._last_check_plans = datetime.now()
    runner._drivers = {downloader.id: (downloader.config, driver)}
    return runner


@pytest.mark.parametrize("restart", [False, True])
def test_completion_recovery(tmp_path, monkeypatch, restart):
    async def stop_after_cycle(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(syncer.asyncio, "sleep", stop_after_cycle)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        pages = {}

        class Client:
            calls = 0

            async def list(self, path, *_args, **_kwargs):
                self.calls += 1
                if not restart and self.calls == 2:
                    raise OpenListClientError(OpenListErrorKind.TRANSIENT)
                return pages[path]

        config = OpenListConfig(
            host="localhost",
            port=80,
            auth=OpenListAuth(token=SecretStr("test")),
            tool="115 Open",
        )
        driver = OpenListDriver(config)
        client = Client()
        driver.client = cast(OpenListClient, client)
        try:
            downloader = await Downloader.create(
                config="config", name="OpenList", priority=1
            )
            library_dir = tmp_path / "library"
            library = await MediaLib.create(
                dir=str(library_dir), name="library", priority=1, lib_type=LibType.MOVIE
            )
            tasks = []
            for index in range(1, 2 if restart else 3):
                name = f"{index}.mkv"
                (tmp_path / name).write_bytes(b"abc")
                task = await DownloadTask.create(
                    downloader=downloader,
                    dir=str(tmp_path),
                    name=name,
                    state=DownloadState.VERIFYING,
                    total_size=3,
                    transfer_lib=library,
                    transfer_method=TransferMethod.COPY,
                )
                entry = RemoteManifestEntry(path=name, is_dir=False, size=3)
                job = await OfflineDownloadJob.create(
                    download=task,
                    job_uuid=f"{index:032x}",
                    source_fingerprint="1" * 64,
                    remote_dir=f"/Kaloscope/{index:032x}",
                    manifest=serialize_manifest((entry,)),
                    manifest_fingerprint=manifest_fingerprint((entry,)),
                )
                pages[job.remote_dir] = RemoteEntryPage(
                    content=[RemoteEntry(name=name, is_dir=False, size=3)], total=1
                )
                tasks.append(task)
            if restart:
                await driver.sync(
                    tuple(DownloadIdentity.from_task(task) for task in tasks)
                )
            else:
                await syncer.sync_tasks(tasks, driver)
            completed = await DownloadTask.get(state=DownloadState.COMPLETED)
            await driver.close()
            driver = OpenListDriver(config)
            driver.client = cast(OpenListClient, client)
            runner = _openlist_runner(downloader, driver)
            await runner.interval()
            assert (library_dir / completed.name).read_bytes() == b"abc"
            await runner.interval()
            assert await Notification.filter(title="DOWNLOAD_COMPLETED").count() == 1
        finally:
            await driver.close()
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    "scenario", ["copy", "notification", "delete", "missing", "conflict"]
)
def test_completion_pending(tmp_path, monkeypatch, scenario):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        driver = OpenListDriver(
            OpenListConfig(
                host="localhost",
                port=80,
                auth=OpenListAuth(token=SecretStr("test")),
                tool="115 Open",
            )
        )
        driver.client = cast(OpenListClient, SimpleNamespace())
        try:
            downloader = await Downloader.create(
                config="config", name="OpenList", priority=1
            )
            library_dir = tmp_path / "library"
            library = await MediaLib.create(
                dir=str(library_dir), name="library", priority=1, lib_type=LibType.MOVIE
            )
            (tmp_path / "movie.mkv").write_bytes(b"abc")
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(tmp_path),
                name="movie.mkv",
                files=["movie.mkv"],
                state=DownloadState.COMPLETED,
                transfer_lib=library,
                transfer_method=TransferMethod.COPY,
            )
            job = await OfflineDownloadJob.create(
                download=task,
                job_uuid="1" * 32,
                source_fingerprint="1" * 64,
                remote_dir=f"/Kaloscope/{'1' * 32}",
                completion_due_at=datetime.now(UTC),
            )
            if scenario == "delete":
                await OfflineDownloadJob.filter(id=job.id).update(
                    delete_due_at=datetime.now(UTC), delete_local=True
                )
                await syncer.sync_tasks([task], driver)
                await job.refresh_from_db()
                assert job.completion_due_at is not None
                assert not library_dir.exists()
                assert await Notification.all().count() == 0
                return

            if scenario == "missing":
                (tmp_path / task.name).unlink()
            elif scenario == "conflict":
                library_dir.mkdir()
                (library_dir / task.name).write_bytes(b"existing video")

            def interrupted_copy(_source, destination):
                Path(destination).write_bytes(b"a")
                raise OSError(errno.ENOSPC, "Disk is full")

            with monkeypatch.context() as patcher:
                if scenario == "copy":
                    patcher.setattr(puller.shutil, "copy2", interrupted_copy)
                elif scenario == "notification":
                    patcher.setattr(
                        syncer.Notifications,
                        "send",
                        AsyncMock(side_effect=RuntimeError("Notification failed")),
                    )
                await syncer.sync_tasks([task], driver)
            await job.refresh_from_db()
            assert job.completion_due_at is not None
            assert await Notification.all().count() == 0
            if scenario == "copy":
                assert not (library_dir / task.name).exists()
            elif scenario == "missing":
                (tmp_path / task.name).write_bytes(b"abc")
            elif scenario == "conflict":
                assert (library_dir / task.name).read_bytes() == b"existing video"
                (library_dir / task.name).unlink()

            await driver.close()
            driver = OpenListDriver(driver.config)
            driver.client = cast(OpenListClient, SimpleNamespace())
            await syncer.sync_tasks([task], driver)
            assert await Notification.all().count() == 0
            await OfflineDownloadJob.filter(id=job.id).update(
                completion_due_at=datetime.now(UTC)
            )
            await syncer.sync_tasks([task], driver)
            await syncer.sync_tasks([task], driver)
            await job.refresh_from_db()
            await task.refresh_from_db()
            assert (library_dir / task.name).read_bytes() == b"abc"
            assert list(library_dir.iterdir()) == [library_dir / task.name]
            assert job.completion_due_at is None
            assert task.error_msg is None
            assert await Notification.filter(title="DOWNLOAD_COMPLETED").count() == 1
        finally:
            await driver.close()
            await Tortoise.close_connections()

    asyncio.run(run())


async def _create_transfer_task(tmp_path, method, files):
    downloader = await Downloader.create(config="config", name="RPC", priority=1)
    library = await MediaLib.create(
        dir=str(tmp_path / "library"),
        name="library",
        priority=1,
        lib_type=LibType.MOVIE,
    )
    source = tmp_path / "downloads"
    source.mkdir()
    for name in files:
        (source / name).write_bytes(name.encode())
    task = await DownloadTask.create(
        downloader=downloader,
        dir=str(source),
        name="movie",
        files=files,
        state=DownloadState.COMPLETED,
        transfer_lib=library,
        transfer_method=method,
    )
    return task, library


@pytest.mark.parametrize("method", list(TransferMethod))
def test_transfer_retry(tmp_path, monkeypatch, method):
    from app.core.media import organizer

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task, library = await _create_transfer_task(tmp_path, method, ["movie.mkv"])
            await syncer.transfer_files(task, task.files)
            original = Path(library.dir) / "movie.mkv"
            organized = Path(library.dir) / "Organized.mkv"
            assert task.transfer_targets == {"movie.mkv": str(original)}
            original.rename(organized)

            async def finish_organizing(current_library):
                assert current_library.id == library.id
                await DownloadTask.filter(id=task.id).update(
                    transfer_targets={"movie.mkv": str(organized)}
                )
                return {str(original): str(organized)}

            recovery = AsyncMock(side_effect=finish_organizing)
            monkeypatch.setattr(organizer, "recover_organizing", recovery)

            # keep the stale instance to exercise the locked database refresh
            await syncer.transfer_files(task, task.files)
            recovery.assert_awaited_once()
            await task.refresh_from_db()
            assert task.transfer_targets == {"movie.mkv": str(organized)}
            assert task.files == ["movie.mkv"]
            assert organized.read_bytes() == b"movie.mkv"
            assert not original.exists()
            source = Path(task.dir) / "movie.mkv"
            assert source.exists() is (method is not TransferMethod.MOVE)
            if method is TransferMethod.HARDLINK:
                assert os.path.samefile(source, organized)
            elif method is TransferMethod.SYMLINK:
                assert organized.is_symlink()
                assert organized.readlink() == source
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("delete_library", [False, True])
def test_transfer_detach(tmp_path, monkeypatch, delete_library):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        running = None
        try:
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.MOVE, ["movie.mkv"]
            )
            requested = asyncio.Event()
            lock = syncer.library_lock

            def waiting_lock(directory):
                requested.set()
                return lock(directory)

            monkeypatch.setattr(syncer, "library_lock", waiting_lock)
            async with lock(library.dir):
                running = asyncio.create_task(syncer.transfer_files(task, task.files))
                await asyncio.wait_for(requested.wait(), 2)
                assert not running.done()
                if delete_library:
                    await MediaLib.filter(id=library.id).delete()
                else:
                    await DownloadTask.filter(id=task.id).update(transfer_lib_id=None)

            await asyncio.wait_for(running, 2)
            await task.refresh_from_db()
            assert task.transfer_lib_id is None
            assert task.transfer_targets is None
            assert not (Path(library.dir) / "movie.mkv").exists()
            assert (Path(task.dir) / "movie.mkv").read_bytes() == b"movie.mkv"
        finally:
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("method", list(TransferMethod))
@pytest.mark.parametrize(
    ("state", "attempted"),
    [
        (DownloadState.DOWNLOADING, False),
        (DownloadState.COMPLETED, False),
        (DownloadState.COMPLETED, True),
    ],
)
def test_transfer_conflict_organization(tmp_path, method, state, attempted):
    from app.core.media import organizer

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task, library = await _create_transfer_task(tmp_path, method, ["movie.mkv"])
            task.state = state
            await task.save()
            library.rename_template = "{{title}}"
            await library.save()
            source = Path(task.dir) / "movie.mkv"
            source.write_bytes(b"new video")
            if state is DownloadState.DOWNLOADING:
                source.unlink()
            destination = Path(library.dir) / "movie.mkv"
            destination.parent.mkdir()
            destination.write_bytes(b"old video")
            nfo = destination.with_suffix(".nfo")
            nfo.write_text("<movie><title>Existing Film</title></movie>")
            item = await MediaItem.create(
                lib=library,
                path=str(destination),
                dir=library.dir,
                name=destination.stem,
                nfo_path=str(nfo),
            )

            if attempted:
                await syncer.transfer_files(task, task.files)
                await task.refresh_from_db()
                assert task.transfer_targets is None
                assert source.read_bytes() == b"new video"
                assert destination.read_bytes() == b"old video"
            async with syncer.library_lock(library.dir):
                await organizer.organize_items(library, [item.id])
            await task.refresh_from_db()
            assert task.transfer_targets is None

            source.write_bytes(b"new video")
            task.state = DownloadState.COMPLETED
            await task.save()
            await syncer.transfer_files(task, task.files)

            await task.refresh_from_db()
            await item.refresh_from_db()
            organized = Path(library.dir) / "Existing Film.mkv"
            assert item.path == str(organized)
            assert organized.read_bytes() == b"old video"
            assert destination.read_bytes() == b"new video"
            assert task.transfer_targets == {"movie.mkv": str(destination)}
            assert source.exists() is (method is not TransferMethod.MOVE)
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_transfer_backfill(tmp_path):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            files = ["movie.mkv", "missing.mkv", "organized.mkv"]
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.COPY, files
            )
            task.sub_pattern = r"^"
            task.sub_repl = "legacy/"
            organized = Path(library.dir) / "Final.mkv"
            task.transfer_targets = {"organized.mkv": str(organized)}
            await task.save()
            destination = Path(library.dir) / "legacy/movie.mkv"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"existing")
            organized.write_bytes(b"organized")

            async with syncer.library_lock(library.dir):
                await syncer.backfill_transfer_targets(library)
            await task.refresh_from_db()
            assert task.transfer_targets == {
                "organized.mkv": str(organized),
            }
            assert not (Path(library.dir) / "legacy/missing.mkv").exists()
            assert (Path(task.dir) / "movie.mkv").read_bytes() == b"movie.mkv"

            # leave unowned legacy destinations untouched during ordinary transfer
            await DownloadTask.filter(id=task.id).update(transfer_targets=None)
            await syncer.transfer_files(task, ["movie.mkv"])
            await task.refresh_from_db()
            assert task.transfer_targets is None
            assert destination.read_bytes() == b"existing"
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("method", [TransferMethod.HARDLINK, TransferMethod.SYMLINK])
def test_transfer_backfill_links(tmp_path, method):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task, library = await _create_transfer_task(tmp_path, method, ["movie.mkv"])
            source = Path(task.dir) / "movie.mkv"
            destination = Path(library.dir) / "movie.mkv"
            destination.parent.mkdir()
            if method is TransferMethod.HARDLINK:
                destination.hardlink_to(source)
            else:
                destination.symlink_to(source)

            async with syncer.library_lock(library.dir):
                await syncer.backfill_transfer_targets(library)
            await task.refresh_from_db()

            assert task.transfer_targets == {"movie.mkv": str(destination)}
            assert destination.samefile(source)
            assert destination.read_bytes() == b"movie.mkv"

            await DownloadTask.filter(id=task.id).update(transfer_targets=None)
            await syncer.transfer_files(task, task.files)
            await task.refresh_from_db()
            assert task.transfer_targets == {"movie.mkv": str(destination)}
            assert destination.samefile(source)
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("pattern", "replacement"),
    [("[", None), ("^", r"\2"), ("^", "{{ 1 / 0 }}")],
)
def test_transfer_backfill_invalid(tmp_path, pattern, replacement):
    from app.core.media import organizer

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.COPY, ["movie.mkv"]
            )
            library.rename_template = "{{title}}"
            await library.save()
            root = Path(library.dir)
            destination = root / "movie.mkv"
            (Path(task.dir) / "movie.mkv").write_bytes(b"movie")
            await syncer.transfer_files(task, task.files)
            nfo = destination.with_suffix(".nfo")
            nfo.write_text("<movie><title>Renamed</title></movie>")
            item = await MediaItem.create(
                lib=library,
                path=str(destination),
                dir=library.dir,
                name="movie",
                nfo_path=str(nfo),
            )
            source = root / "protected.mkv"
            source.write_bytes(b"original download")
            source_nfo = source.with_suffix(".nfo")
            source_nfo.write_text("<movie><title>Do Not Rename</title></movie>")
            protected = await MediaItem.create(
                lib=library,
                path=str(source),
                dir=library.dir,
                name="protected",
                nfo_path=str(source_nfo),
            )
            invalid = await DownloadTask.create(
                downloader_id=task.downloader_id,
                name="invalid",
                dir=library.dir,
                files=[source.name],
                state=DownloadState.COMPLETED,
                transfer_lib=library,
                transfer_method=TransferMethod.MOVE,
                sub_pattern=pattern,
                sub_repl=replacement,
            )

            async with syncer.library_lock(library.dir):
                await organizer.organize_items(library, [protected.id, item.id])
            await task.refresh_from_db()
            await invalid.refresh_from_db()
            await item.refresh_from_db()
            await protected.refresh_from_db()

            organized = root / "Renamed.mkv"
            assert item.path == str(organized)
            assert organized.read_bytes() == b"movie"
            assert task.transfer_targets == {"movie.mkv": str(organized)}
            assert invalid.transfer_targets is None
            assert protected.path == str(source)
            assert source.read_bytes() == b"original download"
            assert source_nfo.read_text() == (
                "<movie><title>Do Not Rename</title></movie>"
            )
            assert not (root / "Do Not Rename.mkv").exists()
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("partial", [False, True])
def test_transfer_backfill_recorded(tmp_path, monkeypatch, caplog, partial):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            files = ["movie.mkv", "missing.mkv"] if partial else ["movie.mkv"]
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.MOVE, files
            )
            source = Path(task.dir) / "movie.mkv"
            destination = Path(library.dir) / "Recorded.mkv"
            transfer_id = hashlib.sha256(f"download:{task.id}".encode()).hexdigest()[
                :32
            ]
            install = puller._install_local_file_sync

            def interrupted_install(target, **kwargs):
                install(target, **kwargs)
                raise OSError("Interrupted after publication")

            with monkeypatch.context() as patcher:
                patcher.setattr(puller, "_install_local_file_sync", interrupted_install)
                with pytest.raises(OSError, match="Interrupted after publication"):
                    puller.transfer_local_file(source, destination, transfer_id)
            task.sub_pattern = "["
            task.transfer_targets = {"movie.mkv": str(destination)}
            await task.save()

            async with syncer.library_lock(library.dir):
                await syncer.backfill_transfer_targets(library)
            await task.refresh_from_db()

            assert task.transfer_targets == {"movie.mkv": str(destination)}
            assert destination.read_bytes() == b"movie.mkv"
            assert not source.exists()
            assert list(Path(library.dir).iterdir()) == [destination]
            assert ("Skipping transfer target backfill" in caplog.text) is partial
            if partial:
                assert (Path(task.dir) / "missing.mkv").read_bytes() == b"missing.mkv"
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("method", [TransferMethod.COPY, TransferMethod.MOVE])
@pytest.mark.parametrize("offline", [False, True])
def test_organization_transfer_recovery(tmp_path, monkeypatch, method, offline):
    from app.core.media import organizer

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task, library = await _create_transfer_task(tmp_path, method, ["movie.mkv"])
            library.rename_template = "{{title}}"
            await library.save()
            job_id = "1" * 32 if offline else None
            if offline:
                await OfflineDownloadJob.create(
                    download=task,
                    job_uuid=job_id,
                    source_fingerprint="a" * 64,
                    remote_dir="/Kaloscope/test",
                )
            source = Path(task.dir) / "movie.mkv"
            destination = Path(library.dir) / "movie.mkv"
            rename = puller.rename_exclusive
            install = puller._install_local_file_sync

            def cross_device(old, new):
                if old == source:
                    raise OSError(errno.EXDEV, "Different filesystem")
                return rename(old, new)

            def interrupted_install(target, **kwargs):
                install(target, **kwargs)
                raise OSError(errno.EINTR, "Interrupted after publication")

            with monkeypatch.context() as patcher:
                patcher.setattr(puller, "rename_exclusive", cross_device)
                patcher.setattr(puller, "_install_local_file_sync", interrupted_install)
                with pytest.raises(OSError, match="Interrupted after publication"):
                    await syncer.transfer_files(task, task.files, job_id=job_id)
            assert source.is_file()
            assert destination.is_file()
            nfo = destination.with_suffix(".nfo")
            nfo.write_text("<movie><title>Renamed</title></movie>")
            item = await MediaItem.create(
                lib=library,
                path=str(destination),
                dir=library.dir,
                name="movie",
                nfo_path=str(nfo),
            )

            unlink = Path.unlink

            def blocked_cleanup(path, *args, **kwargs):
                if (
                    method is TransferMethod.MOVE
                    and path == source
                    or method is TransferMethod.COPY
                    and path.suffix == ".done"
                ):
                    raise PermissionError("Transfer cleanup is blocked")
                return unlink(path, *args, **kwargs)

            with monkeypatch.context() as patcher:
                patcher.setattr(Path, "unlink", blocked_cleanup)
                async with syncer.library_lock(library.dir):
                    with pytest.raises(PermissionError, match="cleanup is blocked"):
                        await organizer.organize_items(library, [item.id])
            assert destination.is_file()
            assert not (Path(library.dir) / "Renamed.mkv").exists()

            async with syncer.library_lock(library.dir):
                await organizer.organize_items(library, [item.id])
            await syncer.transfer_files(task, task.files, job_id=job_id)
            await task.refresh_from_db()
            await item.refresh_from_db()

            organized = Path(library.dir) / "Renamed.mkv"
            assert item.path == str(organized)
            assert task.transfer_targets == {"movie.mkv": str(organized)}
            assert organized.read_bytes() == b"movie.mkv"
            assert source.exists() is (method is TransferMethod.COPY)
            assert sorted(path.name for path in Path(library.dir).iterdir()) == [
                "Renamed.mkv",
                "Renamed.nfo",
            ]
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_partial_transfer(tmp_path, monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            files = ["first.mkv", "second.mkv"]
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.COPY, files
            )
            copy_file = puller.shutil.copy2

            def interrupted_copy(source, destination):
                if Path(source).name == "second.mkv":
                    Path(destination).write_bytes(b"partial")
                    raise OSError(errno.ENOSPC, "Disk is full")
                return copy_file(source, destination)

            with monkeypatch.context() as patcher:
                patcher.setattr(puller.shutil, "copy2", interrupted_copy)
                with pytest.raises(OSError, match="Disk is full"):
                    await syncer.transfer_files(task, files)
            await task.refresh_from_db()
            first = Path(library.dir) / files[0]
            second = Path(library.dir) / files[1]
            assert task.transfer_targets == {files[0]: str(first)}
            assert not second.exists()
            async with syncer.library_lock(library.dir):
                await syncer.backfill_transfer_targets(library)
            await task.refresh_from_db()
            assert task.transfer_targets == {files[0]: str(first)}

            await syncer.transfer_files(task, files)
            await task.refresh_from_db()
            assert task.transfer_targets == {
                files[0]: str(first),
                files[1]: str(second),
            }
            assert second.read_bytes() == b"second.mkv"
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("offline", [False, True])
@pytest.mark.parametrize("interruption", ["copy", "cancel"])
@pytest.mark.parametrize("restart", [False, True])
def test_partial_transfer_companions(
    tmp_path, monkeypatch, offline, interruption, restart
):
    from app.core.media import organizer, watcher
    from app.services.flow import FlowTriggerService

    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        db_url = f"sqlite://{tmp_path / 'media.sqlite'}"
        await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
        await Tortoise.generate_schemas()
        started = threading.Event()
        finish = threading.Event()
        running = None
        try:
            files = ["movie.mkv", "movie.nfo", "movie.zh-CN.srt"]
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.COPY, files
            )
            library.rename_template = "{{title}}"
            await library.save()
            task.transfer_pending = not offline
            await task.save()
            job_id = "1" * 32 if offline else None
            if offline:
                await OfflineDownloadJob.create(
                    download=task,
                    job_uuid=job_id,
                    source_fingerprint="a" * 64,
                    remote_dir="/Kaloscope/test",
                    completion_due_at=datetime.now(UTC),
                )
            source = Path(task.dir)
            (source / "movie.nfo").write_text(
                "<movie><title>Renamed Movie</title></movie>"
            )
            copy_file = puller.shutil.copy2
            transfer_file = syncer.transfer_local_file

            def interrupted_copy(old, new):
                if Path(old).name == "movie.zh-CN.srt":
                    raise OSError("Subtitle copy interrupted")
                return copy_file(old, new)

            def delayed_transfer(old, *args, **kwargs):
                if Path(old).name == "movie.nfo":
                    started.set()
                    assert finish.wait(5)
                return transfer_file(old, *args, **kwargs)

            with monkeypatch.context() as patcher:
                if interruption == "copy":
                    patcher.setattr(puller.shutil, "copy2", interrupted_copy)
                    with pytest.raises(OSError, match="Subtitle copy interrupted"):
                        await syncer.transfer_files(task, files, job_id=job_id)
                else:
                    patcher.setattr(syncer, "transfer_local_file", delayed_transfer)
                    running = asyncio.create_task(
                        syncer.transfer_files(task, files, job_id=job_id)
                    )
                    assert await asyncio.to_thread(started.wait, 2)
                    running.cancel()
                    finish.set()
                    with pytest.raises(asyncio.CancelledError):
                        await running

            root = Path(library.dir)
            video = root / "movie.mkv"
            nfo = root / "movie.nfo"
            item = await MediaItem.create(
                lib=library,
                path=str(video),
                dir=library.dir,
                name="movie",
                nfo_path=str(nfo),
            )
            event = await MediaEvent.create(
                lib=library, src_path=str(nfo), event_type="modified"
            )

            with pytest.raises(organizer.OrganizeDeferredError):
                await watcher.consume_event(event)

            await task.refresh_from_db()
            await item.refresh_from_db()
            await event.refresh_from_db()
            assert task.transfer_targets == {
                "movie.mkv": str(video),
                "movie.nfo": str(nfo),
            }
            assert item.path == str(video)
            assert item.nfo_mtime is not None
            assert event.event_type == "ingest"
            assert video.exists() and nfo.exists()
            assert not (root / "Renamed Movie.mkv").exists()
            fire.assert_not_awaited()

            if restart:
                await Tortoise.close_connections()
                await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
                task = await DownloadTask.get(id=task.id)
                event = await MediaEvent.get(id=event.id)

            assert await syncer.transfer_files(task, files, job_id=job_id)
            await watcher.consume_event(event)
            assert await syncer.transfer_files(task, files, job_id=job_id)

            await task.refresh_from_db()
            await item.refresh_from_db()
            assert item.path == str(root / "Renamed Movie.mkv")
            assert task.transfer_targets == {
                name: str(root / name.replace("movie", "Renamed Movie", 1))
                for name in files
            }
            assert sorted(path.name for path in root.iterdir()) == [
                "Renamed Movie.mkv",
                "Renamed Movie.nfo",
                "Renamed Movie.zh-CN.srt",
            ]
            assert (root / "Renamed Movie.zh-CN.srt").read_bytes() == files[2].encode()
            assert not await MediaEvent.filter(id=event.id).exists()
            fire.assert_not_awaited()
        finally:
            finish.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("pending", [False, True])
def test_partial_transfer_unrelated(tmp_path, pending):
    from app.core.media import organizer

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            files = ["movie.mkv", "movie.nfo", "movie.zh-CN.srt"]
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.COPY, files
            )
            task.transfer_pending = pending
            await task.save()
            library.rename_template = "{{title}}"
            await library.save()
            source = Path(task.dir)
            (source / "movie.nfo").write_text(
                "<movie><title>Pending Movie</title></movie>"
            )
            (source / "movie.zh-CN.srt").unlink()
            assert not await syncer.transfer_files(task, files)
            root = Path(library.dir)
            (root / "other.mkv").write_bytes(b"unrelated video")
            (root / "other.nfo").write_text(
                "<movie><title>Independent Movie</title></movie>"
            )
            items = [
                await MediaItem.create(
                    lib=library,
                    path=str(root / f"{name}.mkv"),
                    dir=library.dir,
                    name=name,
                    nfo_path=str(root / f"{name}.nfo"),
                )
                for name in ("movie", "other")
            ]

            async with syncer.library_lock(library.dir):
                if pending:
                    with pytest.raises(organizer.OrganizeDeferredError):
                        await organizer.organize_items(
                            library, [item.id for item in items]
                        )
                else:
                    await organizer.organize_items(library, [item.id for item in items])

            await items[0].refresh_from_db()
            await items[1].refresh_from_db()
            await task.refresh_from_db()
            name = "movie" if pending else "Pending Movie"
            assert items[0].path == str(root / f"{name}.mkv")
            assert items[1].path == str(root / "Independent Movie.mkv")
            assert (root / "Independent Movie.mkv").read_bytes() == b"unrelated video"
            assert (root / "Independent Movie.nfo").exists()
            assert (root / "Pending Movie.mkv").exists() is (not pending)
            assert task.transfer_targets == {
                "movie.mkv": str(root / f"{name}.mkv"),
                "movie.nfo": str(root / f"{name}.nfo"),
            }
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("method", "cross_device"),
    [
        (TransferMethod.COPY, False),
        (TransferMethod.MOVE, False),
        (TransferMethod.MOVE, True),
    ],
)
@pytest.mark.parametrize("recovery", ["transfer", "organization"])
def test_transfer_target_persistence(
    tmp_path, monkeypatch, method, cross_device, recovery
):
    from app.core.media import organizer

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task, library = await _create_transfer_task(tmp_path, method, ["movie.mkv"])
            source = Path(task.dir) / "movie.mkv"
            destination = Path(library.dir) / "movie.mkv"
            transfer_id = hashlib.sha256(f"download:{task.id}".encode()).hexdigest()[
                :32
            ]
            if cross_device:
                rename = puller.rename_exclusive

                def cross_device_rename(old, new):
                    if old == source:
                        raise OSError(errno.EXDEV, "Different filesystem")
                    return rename(old, new)

                monkeypatch.setattr(puller, "rename_exclusive", cross_device_rename)
            update = QuerySet.update

            def failed_update(query, **kwargs):
                if "transfer_targets" in kwargs:
                    raise OperationalError("Target persistence interrupted")
                return update(query, **kwargs)

            with monkeypatch.context() as patcher:
                patcher.setattr(QuerySet, "update", failed_update)
                with pytest.raises(OperationalError, match="persistence interrupted"):
                    await syncer.transfer_files(task, task.files)
                await task.refresh_from_db()
                assert task.transfer_targets is None
                assert destination.read_bytes() == b"movie.mkv"
                assert puller.owns_local_transfer(destination, transfer_id)
                assert source.exists() is (
                    method is TransferMethod.COPY or cross_device
                )

                async with syncer.library_lock(library.dir):
                    with pytest.raises(
                        OperationalError, match="persistence interrupted"
                    ):
                        await syncer.backfill_transfer_targets(library)
                await task.refresh_from_db()
                assert task.transfer_targets is None
                assert puller.owns_local_transfer(destination, transfer_id)
                assert source.exists() is (
                    method is TransferMethod.COPY or cross_device
                )

            library.rename_template = "{{title}}"
            await library.save()
            nfo = destination.with_suffix(".nfo")
            nfo.write_text("<movie><title>Organized</title></movie>")
            item = await MediaItem.create(
                lib=library,
                path=str(destination),
                dir=library.dir,
                name="movie",
                nfo_path=str(nfo),
            )
            if recovery == "transfer":
                await syncer.transfer_files(task, task.files)
                assert not puller.owns_local_transfer(destination, transfer_id)
            async with syncer.library_lock(library.dir):
                await organizer.organize_items(library, [item.id])
            await syncer.transfer_files(task, task.files)

            await task.refresh_from_db()
            await item.refresh_from_db()
            organized = Path(library.dir) / "Organized.mkv"
            assert task.transfer_targets == {"movie.mkv": str(organized)}
            assert item.path == str(organized)
            assert organized.read_bytes() == b"movie.mkv"
            assert source.exists() is (method is TransferMethod.COPY)
            assert sorted(path.name for path in Path(library.dir).iterdir()) == [
                "Organized.mkv",
                "Organized.nfo",
            ]
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("method", [TransferMethod.COPY, TransferMethod.MOVE])
def test_transfer_cancellation(tmp_path, monkeypatch, method):
    from app.core.media import organizer

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        started = threading.Event()
        finish = threading.Event()
        acquired = asyncio.Event()
        running = contender = None
        try:
            task, library = await _create_transfer_task(tmp_path, method, ["movie.mkv"])
            transfer_file = syncer.transfer_local_file

            def delayed_transfer(*args, **kwargs):
                started.set()
                assert finish.wait(5)
                return transfer_file(*args, **kwargs)

            async def acquire_library():
                async with syncer.library_lock(library.dir):
                    acquired.set()

            monkeypatch.setattr(syncer, "transfer_local_file", delayed_transfer)
            running = asyncio.create_task(syncer.transfer_files(task, task.files))
            assert await asyncio.to_thread(started.wait, 2)
            running.cancel()
            contender = asyncio.create_task(acquire_library())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(acquired.wait(), 0.2)
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await running
            await asyncio.wait_for(contender, 2)
            destination = Path(library.dir) / "movie.mkv"
            await task.refresh_from_db()
            assert task.transfer_targets == {"movie.mkv": str(destination)}
            library.rename_template = "{{title}}"
            await library.save()
            nfo = destination.with_suffix(".nfo")
            nfo.write_text("<movie><title>Renamed</title></movie>")
            item = await MediaItem.create(
                lib=library,
                path=str(destination),
                dir=library.dir,
                name=destination.stem,
                nfo_path=str(nfo),
            )

            async with syncer.library_lock(library.dir):
                await organizer.organize_items(library, [item.id])
            await syncer.transfer_files(task, task.files)

            await task.refresh_from_db()
            organized = Path(library.dir) / "Renamed.mkv"
            assert task.transfer_targets == {"movie.mkv": str(organized)}
            assert organized.read_bytes() == b"movie.mkv"
            assert not destination.exists()
        finally:
            finish.set()
            pending = [task for task in (running, contender) if task is not None]
            await asyncio.gather(*pending, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("method", [TransferMethod.COPY, TransferMethod.MOVE])
@pytest.mark.parametrize("failure", ["worker", "publication", "mapping", "cleanup"])
def test_transfer_cancel_failure(tmp_path, monkeypatch, method, failure):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        started = threading.Event()
        finish = threading.Event()
        acquired = asyncio.Event()
        running = contender = None
        try:
            task, library = await _create_transfer_task(tmp_path, method, ["movie.mkv"])
            task.transfer_pending = True
            await task.save()
            transfer_file = syncer.transfer_local_file
            update = QuerySet.update

            def delayed_transfer(*args, **kwargs):
                started.set()
                assert finish.wait(5)
                if failure == "worker":
                    raise OSError("Transfer interrupted")
                result = transfer_file(*args, **kwargs)
                if failure == "publication":
                    raise OSError("Publication interrupted")
                return result

            def failed_update(query, **kwargs):
                if "transfer_targets" in kwargs:
                    raise OperationalError("Transfer mapping interrupted")
                return update(query, **kwargs)

            def failed_cleanup(*args, **kwargs):
                raise OSError("Cleanup interrupted")

            async def acquire_library():
                async with syncer.library_lock(library.dir):
                    acquired.set()

            with monkeypatch.context() as patcher:
                patcher.setattr(syncer, "transfer_local_file", delayed_transfer)
                if failure == "mapping":
                    patcher.setattr(QuerySet, "update", failed_update)
                elif failure == "cleanup":
                    patcher.setattr(syncer, "recover_local_transfer", failed_cleanup)
                running = asyncio.create_task(syncer._resume_transfers())
                assert await asyncio.to_thread(started.wait, 2)
                running.cancel()
                contender = asyncio.create_task(acquire_library())
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(acquired.wait(), 0.2)
                finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await running
                await asyncio.wait_for(contender, 2)

            destination = Path(library.dir) / "movie.mkv"
            targets = {"movie.mkv": str(destination)}
            transfer_id = hashlib.sha256(f"download:{task.id}".encode()).hexdigest()[
                :32
            ]
            await task.refresh_from_db()
            assert task.transfer_pending is True
            assert task.transfer_targets == (targets if failure == "cleanup" else None)
            assert destination.exists() is (failure != "worker")
            assert puller.owns_local_transfer(destination, transfer_id) is (
                failure != "worker"
            )

            await syncer._resume_transfers()

            await task.refresh_from_db()
            assert task.transfer_pending is False
            assert task.transfer_targets == targets
            assert destination.read_bytes() == b"movie.mkv"
            assert not puller.owns_local_transfer(destination, transfer_id)
            assert (Path(task.dir) / "movie.mkv").exists() is (
                method is TransferMethod.COPY
            )
        finally:
            finish.set()
            pending = [task for task in (running, contender) if task is not None]
            await asyncio.gather(*pending, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("interruption", ["lock", "copy", "mapping"])
def test_rpc_transfer_recovery(tmp_path, monkeypatch, interruption):
    from app.core.media import organizer, watcher
    from app.services.flow import FlowTriggerService

    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    fire = AsyncMock()
    monkeypatch.setattr(FlowTriggerService, "fire", fire)

    async def run():
        db_url = f"sqlite://{tmp_path / 'recovery.sqlite3'}"
        await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
        await Tortoise.generate_schemas()
        running = nfo_event = None
        finish = threading.Event()
        try:
            files = (
                ["first.mkv"]
                if interruption == "lock"
                else [
                    "first.mkv",
                    "second.mkv",
                ]
            )
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.COPY, files
            )
            task.state = DownloadState.DOWNLOADING
            task.unique_id = "remote-task"
            await task.save()
            rpc_call = AsyncMock(
                return_value=[
                    {
                        "unique_id": task.unique_id,
                        "state": DownloadState.COMPLETED,
                        "files": files,
                    }
                ]
            )
            driver = cast(
                RpcDriver,
                SimpleNamespace(
                    client=SimpleNamespace(call=rpc_call),
                    config=SimpleNamespace(methods={}),
                ),
            )

            with monkeypatch.context() as patcher:
                if interruption == "lock":
                    requested = asyncio.Event()
                    lock = syncer.library_lock

                    def waiting_lock(directory):
                        requested.set()
                        return lock(directory)

                    patcher.setattr(syncer, "library_lock", waiting_lock)
                    async with lock(library.dir):
                        running = asyncio.create_task(
                            syncer._sync_rpc_tasks(driver, [task])
                        )
                        await asyncio.wait_for(requested.wait(), 2)
                        running.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await running
                elif interruption == "copy":
                    started = threading.Event()
                    transfer_file = syncer.transfer_local_file

                    def delayed_transfer(*args, **kwargs):
                        started.set()
                        assert finish.wait(5)
                        return transfer_file(*args, **kwargs)

                    patcher.setattr(syncer, "transfer_local_file", delayed_transfer)
                    running = asyncio.create_task(
                        syncer._sync_rpc_tasks(driver, [task])
                    )
                    assert await asyncio.to_thread(started.wait, 2)
                    running.cancel()
                    finish.set()
                    with pytest.raises(asyncio.CancelledError):
                        await running
                else:
                    update = QuerySet.update

                    def failed_update(query, **kwargs):
                        if "transfer_targets" in kwargs:
                            raise OperationalError("Transfer mapping interrupted")
                        return update(query, **kwargs)

                    patcher.setattr(QuerySet, "update", failed_update)
                    await syncer._sync_rpc_tasks(driver, [task])

            await task.refresh_from_db()
            assert task.state is DownloadState.COMPLETED
            assert task.transfer_pending is True
            assert task.files == files
            assert await Notification.filter(title="DOWNLOAD_COMPLETED").count() == 1
            root = Path(library.dir)
            expected = {name: str(root / name) for name in files}
            if interruption == "lock":
                assert not (root / files[0]).exists()
            else:
                assert (root / files[0]).read_bytes() == files[0].encode()
                assert not (root / files[1]).exists()
                library.rename_template = "{{title}}"
                await library.save()
                nfo = root / "first.nfo"
                nfo.write_text("<movie><title>Organized First</title></movie>")
                item = await MediaItem.create(
                    lib=library,
                    path=str(root / files[0]),
                    dir=library.dir,
                    name="first",
                    nfo_path=str(nfo),
                )
                nfo_event = await MediaEvent.create(
                    lib=library, src_path=str(nfo), event_type="modified"
                )

                with pytest.raises(organizer.OrganizeDeferredError):
                    await watcher.consume_event(nfo_event)

                await nfo_event.refresh_from_db()
                await item.refresh_from_db()
                assert nfo_event.event_type == "ingest"
                assert item.nfo_mtime is not None
                assert item.path == str(root / files[0])
                assert nfo.exists()
                assert not (root / "Organized First.mkv").exists()

            rpc_call.reset_mock()
            rpc_call.side_effect = AssertionError("The remote task no longer exists")
            await Tortoise.close_connections()
            await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
            downloader = await Downloader.get(id=task.downloader_id)
            runner = _openlist_runner(downloader, driver)
            runner._drivers = {}
            runner._driver_for = AsyncMock(
                side_effect=AssertionError("Recovery must not load a remote driver")
            )

            async def stop_after_cycle(_seconds):
                raise asyncio.CancelledError

            monkeypatch.setattr(syncer.asyncio, "sleep", stop_after_cycle)
            await runner.interval()
            await runner.interval()

            await task.refresh_from_db()
            assert task.transfer_pending is False
            assert task.transfer_targets == expected
            if nfo_event is not None:
                nfo_event = await MediaEvent.get(id=nfo_event.id)
                await watcher.consume_event(nfo_event)
                expected[files[0]] = str(root / "Organized First.mkv")
                await task.refresh_from_db()
                assert task.transfer_targets == expected
                assert not await MediaEvent.filter(id=nfo_event.id).exists()
            for name, destination in expected.items():
                assert Path(destination).read_bytes() == name.encode()
            if interruption != "lock":
                assert not (root / files[0]).exists()
            assert not list(root.glob("*.done"))
            assert await Notification.filter(title="DOWNLOAD_COMPLETED").count() == 1
            rpc_call.assert_not_awaited()
            runner._driver_for.assert_not_awaited()
            fire.assert_not_awaited()
        finally:
            finish.set()
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await Tortoise.close_connections()

    asyncio.run(run())


def test_rpc_transfer_known_files(tmp_path):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            files = ["movie.mkv"]
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.COPY, files
            )
            task.state = DownloadState.DOWNLOADING
            task.unique_id = "remote-task"
            await task.save()
            driver = cast(
                RpcDriver,
                SimpleNamespace(
                    client=SimpleNamespace(
                        call=AsyncMock(
                            return_value=[
                                {
                                    "unique_id": task.unique_id,
                                    "state": DownloadState.COMPLETED,
                                }
                            ]
                        )
                    ),
                    config=SimpleNamespace(methods={}),
                ),
            )

            await syncer._sync_rpc_tasks(driver, [task])

            await task.refresh_from_db()
            assert task.files == files
            assert task.transfer_pending is False
            destination = Path(library.dir) / files[0]
            assert destination.read_bytes() == files[0].encode()
            assert task.transfer_targets == {files[0]: str(destination)}
            assert await Notification.filter(title="DOWNLOAD_COMPLETED").count() == 1
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("method", list(TransferMethod))
@pytest.mark.parametrize("obstacle", ["missing", "conflict"])
@pytest.mark.parametrize("entry", ["rpc", "resume"])
@pytest.mark.parametrize("blocked", ["first.mkv", "second.mkv"])
def test_rpc_transfer_pending(tmp_path, method, obstacle, entry, blocked):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            files = ["first.mkv", "second.mkv"]
            task, library = await _create_transfer_task(tmp_path, method, files)
            task.state = (
                DownloadState.DOWNLOADING if entry == "rpc" else DownloadState.COMPLETED
            )
            task.unique_id = "remote-task"
            task.transfer_pending = entry == "resume"
            await task.save()
            source = Path(task.dir) / blocked
            destination = Path(library.dir) / blocked
            if obstacle == "missing":
                source.unlink()
            else:
                destination.parent.mkdir()
                destination.write_bytes(b"existing video")
            notifications = 1 if entry == "rpc" else 0

            if entry == "rpc":
                driver = cast(
                    RpcDriver,
                    SimpleNamespace(
                        client=SimpleNamespace(
                            call=AsyncMock(
                                return_value=[
                                    {
                                        "unique_id": task.unique_id,
                                        "state": DownloadState.COMPLETED,
                                        "files": files,
                                    }
                                ]
                            )
                        ),
                        config=SimpleNamespace(methods={}),
                    ),
                )
                await syncer._sync_rpc_tasks(driver, [task])
            else:
                await syncer._resume_transfers()

            completed = next(name for name in files if name != blocked)
            transferred = Path(library.dir) / completed
            transferred_inode = transferred.lstat().st_ino
            await syncer._resume_transfers()
            await task.refresh_from_db()
            assert task.state is DownloadState.COMPLETED
            assert task.transfer_pending is True
            assert task.transfer_targets == {completed: str(transferred)}
            assert transferred.lstat().st_ino == transferred_inode
            if obstacle == "missing":
                assert not destination.exists()
                source.write_bytes(blocked.encode())
            else:
                assert destination.read_bytes() == b"existing video"
                destination.unlink()

            await syncer._resume_transfers()
            await syncer._resume_transfers()

            await task.refresh_from_db()
            assert task.transfer_pending is False
            assert task.transfer_targets == {
                name: str(Path(library.dir) / name) for name in files
            }
            assert transferred.lstat().st_ino == transferred_inode
            for name in files:
                assert (Path(library.dir) / name).read_bytes() == name.encode()
            assert source.exists() is (method is not TransferMethod.MOVE)
            assert (
                await Notification.filter(title="DOWNLOAD_COMPLETED").count()
                == notifications
            )
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("field", ["transfer_lib_id", "transfer_method", "files"])
def test_rpc_transfer_disabled(tmp_path, field):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task, library = await _create_transfer_task(
                tmp_path, TransferMethod.COPY, ["movie.mkv"]
            )
            setattr(task, field, None)
            task.transfer_pending = True
            await task.save()

            await syncer._resume_transfers()

            await task.refresh_from_db()
            assert task.transfer_pending is False
            assert task.transfer_targets is None
            assert not Path(library.dir).exists()
            assert (Path(task.dir) / "movie.mkv").read_bytes() == b"movie.mkv"
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("interrupted", "cancel_fails"), [(False, False), (True, False), (False, True)]
)
def test_delete_during_submission(tmp_path, monkeypatch, interrupted, cancel_fails):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        entered = asyncio.Event()
        release = asyncio.Event()

        class Client:
            submitted = []
            canceled = []

            async def mkdir(self, path):
                entered.set()
                await release.wait()

            async def submit(self, source, path):
                self.submitted.append(path)
                return ("remote-1",)

            async def cancel(self, task_id):
                self.canceled.append(task_id)
                if cancel_fails and len(self.canceled) == 1:
                    raise OpenListClientError(OpenListErrorKind.TRANSIENT)

            async def undone(self):
                return ()

            async def done(self):
                return ()

            async def list(self, *_args, **_kwargs):
                return RemoteEntryPage(content=[], total=0)

            async def transfer_undone(self):
                return ()

        driver = OpenListDriver(
            OpenListConfig(
                host="localhost",
                port=80,
                auth=OpenListAuth(token=SecretStr("test")),
                tool="115 Open",
            )
        )
        client = Client()
        driver.client = cast(OpenListClient, client)
        monkeypatch.setattr(download_service, "load_driver", lambda _config: driver)
        adding = None
        try:
            downloader = await Downloader.create(
                config="config", name="OpenList", priority=1
            )
            adding = asyncio.create_task(
                download_service.DownloadTaskService.add_request(
                    downloader.id,
                    driver,
                    DownloadRequest(
                        directory=str(tmp_path),
                        identity=DownloadIdentity(),
                        link="https://source.test/movie.mkv",
                    ),
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=5)
            task = await DownloadTask.get()
            result = await download_service.DownloadTaskService.delete(
                task.id, local=True
            )
            assert result is not None
            action, state = result
            runner = _openlist_runner(downloader, driver)
            runner.publish(task.id, action, state, local=True)
            await runner._consume_actions()
            assert await DownloadTask.filter(id=task.id).exists()
            job = await OfflineDownloadJob.get(download_id=task.id)
            assert job.delete_due_at is not None
            assert job.delete_local is True

            # a slow live request must not be mistaken for a crashed process
            await DownloadTask.filter(id=task.id).update(
                created_at=datetime.now(UTC) - timedelta(minutes=1)
            )
            await driver.sync((DownloadIdentity.from_task(task),))
            await task.refresh_from_db()
            assert task.state is DownloadState.SUBMITTING

            if interrupted:
                adding.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await adding
                await driver.sync((DownloadIdentity.from_task(task),))
            else:
                release.set()
                await adding

            # restart the action consumer without its shared in-memory command
            runner = _openlist_runner(downloader, driver)
            await runner._consume_actions()
            if cancel_fails:
                await job.refresh_from_db()
                assert job.delete_due_at > datetime.now(UTC)
                await runner._consume_actions()
                assert client.canceled == ["remote-1"]
                await OfflineDownloadJob.filter(id=job.id).update(
                    delete_due_at=datetime.now(UTC)
                )
                await runner._consume_actions()
            assert not await DownloadTask.filter(id=task.id).exists()
            assert not await OfflineDownloadJob.filter(download_id=task.id).exists()
            assert len(client.submitted) == (0 if interrupted else 1)
            expected_cancels = 0 if interrupted else 2 if cancel_fails else 1
            assert client.canceled == ["remote-1"] * expected_cancels
        finally:
            if adding is not None and not adding.done():
                adding.cancel()
                await asyncio.gather(adding, return_exceptions=True)
            await driver.close()
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.fixture(autouse=True)
def disabled_secret_key(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(
        KaloscopeConfig,
        "_config",
        SimpleNamespace(
            secret_key_enabled=False,
            workspace={"TEMP": str(tmp_path_factory.mktemp("workspace-temp"))},
        ),
    )


class RecordingClient:
    def __init__(self, item):
        self.item = item
        self.calls = []

    async def call(self, method, variables):
        self.calls.append(method)
        assert method == "list"
        return [] if self.item is None else [self.item]


def _rpc_config() -> RpcConfig:
    return RpcConfig(
        name="test",
        host="example.com",
        port=80,
        methods={"list": API()},
    )


class RecordingQuery:
    def __init__(self):
        self.values = None

    async def update(self, **values):
        self.values = values


class PlanOpenListClient:
    def __init__(self):
        self.submissions = []

    async def mkdir(self, _path):
        return None

    async def submit(self, source, path):
        self.submissions.append((source, path))
        return ("remote-1",)


class FailingPlanOpenListClient(PlanOpenListClient):
    async def submit(self, source, path):
        self.submissions.append((source, path))
        raise RuntimeError("submission failed")


async def _run_download_plan(monkeypatch, driver, *, source=None, http=None):
    magnet = source or f"magnet:?xt=urn:btih:{'a' * 40}"
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        downloader = await Downloader.create(
            config="config", name="downloader", priority=1
        )
        graph = await FlowGraph.create(
            name="search",
            category=GraphCategory.INDEXER,
            state=GraphState.PUBLISHED,
        )
        plan = await DownloadPlan.create(
            graph=graph,
            downloader=downloader,
            dir="/downloads",
            keyword="example",
            batch_limit=1,
        )
        engine = SimpleNamespace(
            execute=AsyncMock(return_value={"items": [{"link": magnet}]})
        )
        monkeypatch.setattr(
            syncer.Sanic,
            "get_app",
            lambda: SimpleNamespace(
                ctx=SimpleNamespace(flow_engine=engine, httpx=http)
            ),
        )

        await syncer.execute_download_plan(plan, driver)
        await plan.refresh_from_db()
        task = await DownloadTask.get_or_none()
        job = await OfflineDownloadJob.get_or_none()
        history_count = await DownloadPlanHistory.all().count()
        return magnet, plan, task, job, history_count
    finally:
        await Tortoise.close_connections()


def test_download_plan(monkeypatch):
    client = PlanOpenListClient()
    driver = OpenListDriver(
        OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr("secret")),
            tool="Future Tool",
        )
    )
    driver.client = cast(OpenListClient, client)

    magnet, plan, task, job, history_count = asyncio.run(
        _run_download_plan(monkeypatch, driver)
    )

    assert task is not None
    assert job is not None
    assert task.state is DownloadState.REMOTE
    assert task.unique_id == "remote-1"
    assert job.download_id == task.id
    assert client.submissions == [(magnet, job.remote_dir)]
    assert plan.total_count == 1
    assert history_count == 1


@pytest.mark.parametrize("upload", [False, True])
def test_plan_torrent(monkeypatch, upload):
    info = {
        "name": "sample.bin",
        "length": 4,
        "piece length": 16384,
        "pieces": hashlib.sha1(b"data").digest(),
    }
    torrent = Bencode.encode(
        {"info": info, "announce": "https://tracker.example/announce"}
    )
    hash = hashlib.sha1(Bencode.encode(info)).hexdigest()
    requests = []
    client = SimpleNamespace(call=AsyncMock(return_value={"unique_id": "remote"}))
    methods: dict[Method, API] = {"add_link": API()}
    if upload:
        methods["add_torrent"] = API()
    driver = RpcDriver(
        RpcConfig(name="RPC", host="localhost", port=80, methods=methods)
    )
    monkeypatch.setattr(driver, "client", client)

    def handler(request):
        assert str(request.url) == "https://example.com/sample.torrent"
        requests.append(request.method)
        return httpx.Response(
            200,
            headers={"content-type": "application/x-bittorrent"},
            content=torrent if request.method == "GET" else b"",
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await _run_download_plan(
                monkeypatch,
                driver,
                source="https://example.com/sample.torrent",
                http=http,
            )

    _, plan, task, job, history_count = asyncio.run(run())
    assert task is not None and task.info_hash == hash and task.unique_id == "remote"
    assert task.magnet_link == f"magnet:?xt=urn:btih:{hash}"
    assert job is None
    assert plan.total_count == history_count == 1
    method, variables = client.call.await_args.args
    assert method == ("add_torrent" if upload else "add_link")
    assert variables["torrent"] == (
        (f"{hash}.torrent", torrent, "application/x-bittorrent") if upload else None
    )
    assert requests == ["HEAD", "GET"]


def test_plan_log(monkeypatch, caplog):
    client = FailingPlanOpenListClient()
    driver = OpenListDriver(
        OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr("secret")),
            tool="Future Tool",
        )
    )
    driver.client = cast(OpenListClient, client)
    caplog.set_level("ERROR")

    asyncio.run(_run_download_plan(monkeypatch, driver))

    assert "magnet:?" not in caplog.text
    assert "a" * 40 in caplog.text


def test_driver_error():
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir="/downloads",
                name="movie.mkv",
                state=DownloadState.SETTLING,
            )
            driver = SimpleNamespace(
                sync=AsyncMock(
                    return_value=(
                        DownloadSnapshot(
                            identity=DownloadIdentity(),
                            state=DownloadState.ERROR,
                            error="unmatched task",
                        ),
                        DownloadSnapshot(
                            identity=DownloadIdentity.from_task(task),
                            state=DownloadState.ERROR,
                            error="remote transfer failed",
                        ),
                    )
                )
            )

            await syncer.sync_tasks([task], cast(DownloaderDriver, driver))
            return await Notification.get()
        finally:
            await Tortoise.close_connections()

    notification = asyncio.run(run())

    assert notification.title == "DOWNLOAD_FAILED"
    assert json.loads(notification.content) == {
        "name": "movie.mkv",
        "error": "remote transfer failed",
    }


@pytest.mark.parametrize(
    ("task_identity", "remote_identity"),
    [
        (
            {
                "unique_id": "task-id",
                "info_hash": "shared-v1",
                "info_hash_v2": "task-v2",
            },
            {
                "unique_id": "remote-id",
                "info_hash": "shared-v1",
                "info_hash_v2": "remote-v2",
            },
        ),
        (
            {
                "unique_id": "shared-id",
                "info_hash": "task-v1",
                "info_hash_v2": "task-v2",
            },
            {
                "unique_id": "shared-id",
                "info_hash": "remote-v1",
                "info_hash_v2": "remote-v2",
            },
        ),
    ],
)
def test_identity_match(monkeypatch, task_identity, remote_identity):
    task = cast(
        DownloadTask,
        SimpleNamespace(
            id=1,
            downloader_id=2,
            dir="/downloads",
            name="Original",
            state=DownloadState.DOWNLOADING,
            raw_state="downloading",
            up_speed=0,
            dl_speed=0,
            total_size=100,
            completed_size=10,
            files=[],
            **task_identity,
        ),
    )
    item = {
        "name": "Matched",
        "raw_state": "downloading",
        "percentage": 50,
        "total_size": 100,
        "completed_size": 50,
        "files": [],
        **remote_identity,
    }
    query = RecordingQuery()
    driver = RpcDriver(_rpc_config())
    driver.client = cast(RpcClient, RecordingClient(item))
    monkeypatch.setattr(
        syncer, "DownloadTask", SimpleNamespace(filter=lambda **_filters: query)
    )

    asyncio.run(syncer.sync_tasks([task], driver))

    assert query.values is not None
    assert query.values["name"] == "Matched"


@pytest.mark.parametrize(
    ("directory", "path"),
    [
        ("/downloads", "/downloads/torrent/nested/one.mkv"),
        ("/downloads/", "/downloads/torrent/nested/one.mkv"),
        ("/downloads/", "torrent/nested/one.mkv"),
    ],
)
def test_relative_files(monkeypatch, directory, path):
    task = cast(
        DownloadTask,
        SimpleNamespace(
            id=1,
            unique_id="task-id",
            info_hash=None,
            info_hash_v2=None,
            dir=directory,
            name="torrent",
            state=DownloadState.PAUSED,
            raw_state="paused",
            up_speed=0,
            dl_speed=0,
            total_size=100,
            completed_size=0,
        ),
    )
    query = RecordingQuery()
    driver = RpcDriver(_rpc_config())
    driver.client = cast(
        RpcClient, RecordingClient({"unique_id": "task-id", "files": [path]})
    )
    monkeypatch.setattr(
        syncer, "DownloadTask", SimpleNamespace(filter=lambda **_filters: query)
    )

    asyncio.run(syncer.sync_tasks([task], driver))

    assert query.values is not None
    assert query.values["files"] == ["torrent/nested/one.mkv"]


def test_fast_sync(monkeypatch):
    client = RecordingClient(None)
    driver = RpcDriver(_rpc_config())
    driver.client = cast(RpcClient, client)
    load_count = 0

    def load_driver(_config):
        nonlocal load_count
        load_count += 1
        return driver

    monkeypatch.setattr(syncer, "load_driver", load_driver)
    original_sync_tasks = syncer.sync_tasks

    async def sync_without_tasks(_tasks, cached_driver):
        await original_sync_tasks([], cached_driver)

    sleep_count = 0

    async def stop_after_two_cycles(_seconds):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count == 2:
            raise asyncio.CancelledError

    runner = cast(Any, object.__new__(syncer.DLSyncer))
    runner._app = SimpleNamespace(
        shared_ctx=SimpleNamespace(
            dl_sync_fast=SimpleNamespace(is_set=lambda: True),
            dl_task_actions={},
        )
    )
    runner._last_sync_tasks = datetime.now()
    runner._last_check_plans = datetime.now()
    runner._drivers = {}
    runner._consume_actions = AsyncMock(return_value=False)

    downloader = SimpleNamespace(id=1, config="invalid")
    task = SimpleNamespace(downloader_id=1)

    async def filtered_tasks(*_args, **filters):
        return [] if filters.get("transfer_pending") else [task]

    monkeypatch.setattr(syncer, "DownloadTask", SimpleNamespace(filter=filtered_tasks))
    monkeypatch.setattr(
        syncer, "Downloader", SimpleNamespace(get=AsyncMock(return_value=downloader))
    )
    monkeypatch.setattr(syncer, "sync_tasks", sync_without_tasks)
    monkeypatch.setattr(syncer.asyncio, "sleep", stop_after_two_cycles)

    asyncio.run(runner.interval())

    assert client.calls == ["list", "list"]
    assert load_count == 1


def test_cleanup_retry(monkeypatch):
    class CleanupClient:
        def __init__(self):
            self.calls = []

        async def remove(self, directory, name):
            self.calls.append((directory, name))
            if len(self.calls) == 1:
                raise OpenListClientError(OpenListErrorKind.TRANSIENT)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir="/downloads",
                name="result",
                state=DownloadState.COMPLETED,
            )
            job = await OfflineDownloadJob.create(
                download=task,
                job_uuid="1234567890ab4def81234567890abcde",
                source_fingerprint="1" * 64,
                remote_dir="/Kaloscope/1234567890ab4def81234567890abcde",
                next_poll_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            client = CleanupClient()
            driver = OpenListDriver(
                OpenListConfig(
                    protocol="https",
                    host="openlist.example.com",
                    port=443,
                    auth=OpenListAuth(token=SecretStr("secret")),
                    tool="Future Tool",
                    remote_cleanup=RemoteCleanupPolicy.DELETE_ON_SUCCESS,
                )
            )
            driver.client = cast(OpenListClient, client)
            runner = cast(Any, object.__new__(syncer.DLSyncer))
            runner._app = SimpleNamespace(
                shared_ctx=SimpleNamespace(
                    dl_sync_fast=SimpleNamespace(is_set=lambda: True),
                    dl_task_actions={},
                )
            )
            runner._last_sync_tasks = datetime.now()
            runner._last_check_plans = datetime.now()
            runner._drivers = {downloader.id: (downloader.config, driver)}

            async def stop_after_cycle(_seconds):
                raise asyncio.CancelledError

            monkeypatch.setattr(syncer.asyncio, "sleep", stop_after_cycle)
            await runner.interval()
            await job.refresh_from_db()
            scheduled = job.next_poll_at
            retry_count = job.retry_count
            job.next_poll_at = datetime(2026, 1, 1, tzinfo=UTC)
            await job.save(update_fields=["next_poll_at"])
            await runner.interval()
            await job.refresh_from_db()
            return job, client, scheduled, retry_count
        finally:
            await Tortoise.close_connections()

    job, client, scheduled, retry_count = asyncio.run(run())

    assert client.calls == [
        ("/Kaloscope", "1234567890ab4def81234567890abcde"),
        ("/Kaloscope", "1234567890ab4def81234567890abcde"),
    ]
    assert scheduled is not None
    assert retry_count == 1
    assert job.last_error_kind is None
    assert job.next_poll_at is None
    assert job.retry_count == 0


def test_actions():
    class ActionDriver:
        def __init__(self):
            self.calls = []

        async def pause(self, _identity):
            return DownloadState.PAUSED

        async def resume(self, _identity):
            return DownloadState.PULLING

        async def cancel(self, identity):
            self.calls.append(("cancel", identity.task_id))

        async def retry(self, _identity):
            return DownloadState.REMOTE

        async def capabilities(self, *_args, **_kwargs):
            return frozenset({DownloadAction.CANCEL, DownloadAction.DELETE})

        async def delete(self, identity, *, local=False):
            task = await DownloadTask.get(id=identity.task_id)
            self.calls.append(("delete", identity.task_id, local, task.state))

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            states = (
                DownloadState.PULLING,
                DownloadState.PAUSED,
                DownloadState.REMOTE,
                DownloadState.ERROR,
                DownloadState.REMOTE,
            )
            tasks = [
                await DownloadTask.create(
                    downloader=downloader,
                    dir="/downloads",
                    name=f"task-{index}",
                    state=state,
                    raw_state="failed" if state is DownloadState.ERROR else None,
                    percentage=80 if state is DownloadState.ERROR else 0,
                    dl_speed=1024,
                    error_msg="pull failed" if state is DownloadState.ERROR else None,
                )
                for index, state in enumerate(states, start=1)
            ]
            retry_job = await OfflineDownloadJob.create(
                download=tasks[3],
                job_uuid="retry-job",
                source_fingerprint="source",
                remote_dir="/remote/retry-job",
                next_poll_at=datetime.now(UTC) + timedelta(minutes=5),
                retry_count=3,
                last_error_kind=OfflineDownloadErrorKind.REMOTE_FAILED,
            )
            actions = {}
            driver = ActionDriver()
            runner = cast(Any, object.__new__(syncer.DLSyncer))
            runner._app = SimpleNamespace(
                shared_ctx=SimpleNamespace(dl_task_actions=actions)
            )
            runner._drivers = {downloader.id: (downloader.config, driver)}
            requested = (
                DownloadAction.PAUSE,
                DownloadAction.RESUME,
                DownloadAction.CANCEL,
                DownloadAction.RETRY,
                DownloadAction.DELETE,
            )
            for task, action, state in zip(tasks, requested, states, strict=True):
                runner.publish(task.id, action, state, local=True)

            await runner._consume_actions()
            for task in tasks[:-1]:
                await task.refresh_from_db()
            await retry_job.refresh_from_db()
            deleted = await DownloadTask.get_or_none(id=tasks[-1].id)
            return actions, driver, tasks, retry_job, deleted
        finally:
            await Tortoise.close_connections()

    actions, driver, tasks, retry_job, deleted = asyncio.run(run())

    assert actions == {}
    assert [task.state for task in tasks[:-1]] == [
        DownloadState.PAUSED,
        DownloadState.PULLING,
        DownloadState.REMOTE,
        DownloadState.REMOTE,
    ]
    assert [task.dl_speed for task in (tasks[0], tasks[1], tasks[3])] == [0, 0, 0]
    assert tasks[3].error_msg is None
    assert tasks[3].raw_state is None
    assert tasks[3].percentage == 0
    assert retry_job.last_error_kind is None
    assert retry_job.next_poll_at is None
    assert retry_job.retry_count == 0
    assert deleted is None
    assert driver.calls == [
        ("cancel", tasks[2].id),
        ("cancel", tasks[4].id),
        ("delete", tasks[4].id, True, DownloadState.REMOTE),
    ]


def test_slow_sync(monkeypatch):
    waits = []

    async def stop_after_wait(seconds):
        waits.append(seconds)
        raise asyncio.CancelledError

    runner = cast(Any, object.__new__(syncer.DLSyncer))
    runner._app = SimpleNamespace(
        shared_ctx=SimpleNamespace(
            dl_sync_fast=SimpleNamespace(is_set=lambda: False),
        )
    )
    runner._last_sync_tasks = datetime.now()
    runner._consume_actions = AsyncMock(return_value=False)
    monkeypatch.setattr(syncer.asyncio, "sleep", stop_after_wait)
    asyncio.run(runner.interval())

    runner._consume_actions.assert_awaited_once()
    assert waits == [1]


@pytest.mark.parametrize(
    ("previous", "remote", "speed", "progress"),
    [
        (DownloadState.DOWNLOADING, DownloadState.PAUSED, 999, 100),
        (DownloadState.PAUSED, DownloadState.DOWNLOADING, 0, 10),
        (DownloadState.DOWNLOADING, DownloadState.ERROR, 999, 100),
    ],
)
def test_remote_state(monkeypatch, previous, remote, speed, progress):
    # remote state overrides stale speed and progress reported by an RPC API
    task = cast(
        DownloadTask,
        SimpleNamespace(
            id=1,
            downloader_id=2,
            dir="/downloads",
            name="Task",
            state=previous,
            raw_state="",
            up_speed=0,
            dl_speed=0,
            total_size=100,
            completed_size=10,
            files=[],
            unique_id="remote",
            info_hash="",
            info_hash_v2="",
        ),
    )
    item = {
        "unique_id": "remote",
        "state": remote,
        "dl_speed": speed,
        "up_speed": speed,
        "percentage": progress,
        "files": [],
    }
    query = RecordingQuery()
    driver = RpcDriver(_rpc_config())
    driver.client = cast(RpcClient, RecordingClient(item))
    monkeypatch.setattr(
        syncer, "DownloadTask", SimpleNamespace(filter=lambda **_: query)
    )
    monkeypatch.setattr(syncer.Notifications, "send", AsyncMock())
    asyncio.run(syncer.sync_tasks([task], driver))
    assert query.values is not None
    assert query.values["state"] == remote
    assert query.values["dl_speed"] == 0
    assert query.values["up_speed"] == 0


@pytest.mark.parametrize(
    ("source", "files", "paths"),
    [
        ("list", None, ["/tasks", "/tasks/remote"]),
        ("list", [], ["/tasks"]),
        ("details", None, ["/tasks", "/tasks/remote"]),
        ("details", [], ["/tasks", "/tasks/remote"]),
        ("followed", None, ["/tasks", "/tasks/remote", "/tasks/child"]),
    ],
)
def test_rpc_files_lookup(source, files, paths):
    requests = []
    remote_id = "child" if source == "followed" else "remote"
    item = {
        "unique_id": remote_id,
        "state": "paused",
        "files": files,
        "percentage": 25,
    }

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/tasks":
            return httpx.Response(200, json=[item] if source == "list" else [])
        if source == "followed" and request.url.path == "/tasks/remote":
            return httpx.Response(
                200, json={"files": ["[METADATA]"], "followed_by": ["child"]}
            )
        return httpx.Response(200, json=item)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="RPC", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                unique_id="remote",
                dir="/downloads",
                name="movie.mkv",
                state=DownloadState.DOWNLOADING,
            )
            driver = RpcDriver(
                RpcConfig(
                    name="RPC",
                    host="localhost",
                    port=80,
                    methods={
                        "list": API(get="/tasks"),
                        "details": API(get="/tasks/{{id}}"),
                    },
                )
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as http:
                driver.client = RpcClient(driver.config, http)
                await syncer.sync_tasks([task], driver)
            await task.refresh_from_db()
            assert task.state == DownloadState.PAUSED
            assert task.unique_id == remote_id
            assert task.files == files
            assert requests == paths
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_rpc_error_precedes_file_lookup():
    notifications_at_lookup = []

    async def handler(request):
        if request.url.path == "/tasks":
            return httpx.Response(
                200,
                json=[
                    {
                        "unique_id": "remote",
                        "error_msg": "Disk full",
                        "raw_state": "error",
                    }
                ],
            )
        assert request.url.path == "/tasks/remote"
        notifications_at_lookup.append(
            await Notification.filter(title="DOWNLOAD_FAILED").count()
        )
        return httpx.Response(503)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="RPC", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                unique_id="remote",
                dir="/downloads",
                name="movie.mkv",
                state=DownloadState.DOWNLOADING,
            )
            driver = RpcDriver(
                RpcConfig(
                    name="RPC",
                    host="localhost",
                    port=80,
                    methods={
                        "list": API(get="/tasks"),
                        "details": API(get="/tasks/{{id}}"),
                    },
                )
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as http:
                driver.client = RpcClient(driver.config, http)
                await syncer.sync_tasks([task], driver)
            assert notifications_at_lookup == [1]
            notification = await Notification.get()
            assert notification.title == "DOWNLOAD_FAILED"
            assert json.loads(notification.content) == {
                "name": "movie.mkv",
                "error": "Disk full",
            }
            await task.refresh_from_db()
            assert task.state == DownloadState.DOWNLOADING
            assert task.completed_at is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("listed", [False, True])
@pytest.mark.parametrize(
    "availability",
    [
        "missing_file",
        "empty_directory",
        "temporary_files",
        "missing_path",
        "unreadable_root",
        "unreadable_subdirectory",
    ],
)
def test_xunlei_waits_for_files(tmp_path, monkeypatch, listed, availability):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "ready.mkv").write_bytes(b"done")
    directory = availability in {
        "empty_directory",
        "temporary_files",
        "unreadable_root",
        "unreadable_subdirectory",
    }
    late_path = downloads / ("late" if directory else "late.mkv")
    late_file = late_path / "movie.mkv" if directory else late_path
    if availability == "unreadable_subdirectory":
        late_file = late_path / "nested/movie.mkv"
    relative_file = str(late_file.relative_to(downloads))
    expected_files = [relative_file]
    late_params = {"real_path": str(late_path)}
    if directory:
        late_path.mkdir()
    if availability == "temporary_files":
        (late_path / "movie.mkv.xltd").write_bytes(b"partial")
        (late_path / "movie.mkv.xltd.cfg").write_bytes(b"config")
    if availability == "missing_path":
        late_params.clear()
        late_file.write_bytes(b"late")
    unreadable = None
    if availability in {"unreadable_root", "unreadable_subdirectory"}:
        late_file.parent.mkdir(parents=True, exist_ok=True)
        late_file.write_bytes(b"late")
        unreadable = late_file.parent
        if availability == "unreadable_subdirectory":
            # a readable sibling must not make the incomplete scan successful
            (late_path / "visible.mkv").write_bytes(b"late")
            expected_files.append("late/visible.mkv")
        scandir = os.scandir

        def scan(path):
            # simulate denied access even when the tests run as root
            if unreadable is not None and Path(path) == unreadable:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return scandir(path)

        monkeypatch.setattr(os, "scandir", scan)
    library_dir = tmp_path / "library"
    config = (Path(__file__).parents[1] / "static/downloaders/Xunlei.yaml").read_text()
    app = SimpleNamespace(ctx=SimpleNamespace())
    monkeypatch.setattr(Sanic, "get_app", lambda: app)

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(
                200, text='function uiauth(value) { return "test-token" }'
            )
        if request.url.path == "/device/info/watch":
            return httpx.Response(200, json={"is_login": True, "target": "device#test"})
        assert request.url.path == "/drive/v1/tasks"
        ids = json.loads(request.url.params["filters"])["id"]["in"].split(",")
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "id": name,
                        "name": f"{name}.mkv",
                        "phase": "PHASE_TYPE_COMPLETE",
                        "file_size": str(4 * len(expected_files))
                        if name == "late"
                        else "4",
                        "params": late_params
                        if name == "late"
                        else {"real_path": str(downloads / "ready.mkv")},
                    }
                    for name in ids
                    if listed or name != "late" or request.url.params["limit"] == "1"
                ]
            },
        )

    async def run():
        nonlocal unreadable
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config=config, name="NAS Xunlei", priority=1
            )
            library = await MediaLib.create(
                dir=str(library_dir), name="library", priority=1, lib_type=LibType.MOVIE
            )
            for name in ("late", "ready"):
                await DownloadTask.create(
                    downloader=downloader,
                    unique_id=name,
                    dir=str(downloads),
                    name=f"{name}.mkv",
                    state=DownloadState.DOWNLOADING,
                    files=[],
                    percentage=25,
                    completed_size=1,
                    total_size=4,
                    transfer_lib=library,
                    transfer_method=TransferMethod.COPY,
                )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as http:
                driver = cast(RpcDriver, load_driver(config))
                driver.client = RpcClient(driver.config, http)
                for _ in range(2):
                    pending = await DownloadTask.filter(
                        state=DownloadState.DOWNLOADING
                    ).order_by("id")
                    await syncer.sync_tasks(pending, driver)
                    late = await DownloadTask.get(unique_id="late")
                    assert late.state == DownloadState.DOWNLOADING
                    assert late.completed_at is None
                    assert late.percentage == 25
                    assert late.files == []
                    notification = await Notification.get()
                    assert notification.title == "DOWNLOAD_COMPLETED"
                    assert json.loads(notification.content) == {"name": "ready.mkv"}
                    assert (library_dir / "ready.mkv").read_bytes() == b"done"
                    assert all(
                        not (library_dir / file).exists() for file in expected_files
                    )

                unreadable = None
                late_file.write_bytes(b"late")
                late_params["real_path"] = str(late_path)
                pending = await DownloadTask.filter(state=DownloadState.DOWNLOADING)
                await syncer.sync_tasks(pending, driver)
                late = await DownloadTask.get(unique_id="late")
                assert late.state == DownloadState.COMPLETED
                assert late.completed_at is not None
                assert late.percentage == 100
                assert late.files == sorted(expected_files)
                for file in expected_files:
                    assert (library_dir / file).read_bytes() == b"late"
                assert (
                    await Notification.filter(title="DOWNLOAD_COMPLETED").count() == 2
                )
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("paths", ["task_alias", "remote_alias", "both", "outside"])
def test_xunlei_file_paths(tmp_path, monkeypatch, directory, paths):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    task_alias = tmp_path / "share"
    task_alias.symlink_to(downloads, target_is_directory=True)
    remote_alias = tmp_path / "package-share"
    remote_alias.symlink_to(downloads, target_is_directory=True)
    task_dir = task_alias if paths in {"task_alias", "both", "outside"} else downloads
    remote_dir = remote_alias if paths in {"remote_alias", "both"} else downloads
    if paths == "outside":
        remote_dir = tmp_path / "downloads-other"
        remote_dir.mkdir()
    relative_file = "torrent/nested/movie.mkv" if directory else "movie.mkv"
    source = remote_dir / relative_file
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"done")
    root = remote_dir / "torrent" if directory else source
    library_dir = tmp_path / "library"
    config = (Path(__file__).parents[1] / "static/downloaders/Xunlei.yaml").read_text()
    app = SimpleNamespace(ctx=SimpleNamespace())
    monkeypatch.setattr(Sanic, "get_app", lambda: app)

    def handler(request):
        if request.url.path == "/":
            return httpx.Response(
                200, text='function uiauth(value) { return "test-token" }'
            )
        if request.url.path == "/device/info/watch":
            return httpx.Response(200, json={"is_login": True, "target": "device#test"})
        assert request.url.path == "/drive/v1/tasks"
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "id": "remote",
                        "name": root.name,
                        "phase": "PHASE_TYPE_COMPLETE",
                        "file_size": "4",
                        "params": {"real_path": str(root)},
                    }
                ]
            },
        )

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config=config, name="NAS Xunlei", priority=1
            )
            library = await MediaLib.create(
                dir=str(library_dir), name="library", priority=1, lib_type=LibType.MOVIE
            )
            task = await DownloadTask.create(
                downloader=downloader,
                unique_id="remote",
                dir=f"{task_dir}/",
                name=root.name,
                state=DownloadState.DOWNLOADING,
                files=[],
                transfer_lib=library,
                transfer_method=TransferMethod.COPY,
            )
            driver = cast(RpcDriver, load_driver(config))
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as http:
                driver.client = RpcClient(driver.config, http)
                await syncer.sync_tasks([task], driver)
            await task.refresh_from_db()
            if paths == "outside":
                assert task.state == DownloadState.DOWNLOADING
                assert task.completed_at is None
                assert task.files == []
                assert await Notification.all().count() == 0
                assert not library_dir.exists()
            else:
                assert task.state == DownloadState.COMPLETED
                assert task.files == [relative_file]
                assert (library_dir / relative_file).read_bytes() == b"done"
                assert (
                    await Notification.filter(title="DOWNLOAD_COMPLETED").count() == 1
                )
            assert source.read_bytes() == b"done"
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())
