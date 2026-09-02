import asyncio
import errno
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.dl import syncer
from app.core.dl.driver import (
    DownloadAction,
    DownloadIdentity,
    DownloadRequest,
    DownloadSnapshot,
)
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
from app.core.dl.rpc.models import API
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
from app.models.media import LibType, MediaLib
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
            host="localhost", port=80, auth={"token": "test"}, tool="115 Open"
        )
        driver = OpenListDriver(config)
        client = Client()
        driver.client = client
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
            driver.client = client
            runner = _openlist_runner(downloader, driver)
            await runner.interval()
            assert (library_dir / completed.name).read_bytes() == b"abc"
            await runner.interval()
            assert await Notification.filter(title="DOWNLOAD_COMPLETED").count() == 1
        finally:
            await driver.close()
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("scenario", ["copy", "notification", "delete"])
def test_completion_pending(tmp_path, monkeypatch, scenario):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        driver = OpenListDriver(
            OpenListConfig(
                host="localhost", port=80, auth={"token": "test"}, tool="115 Open"
            )
        )
        driver.client = SimpleNamespace()
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

            def interrupted_copy(_source, destination):
                Path(destination).write_bytes(b"a")
                raise OSError(errno.ENOSPC, "Disk is full")

            with monkeypatch.context() as patcher:
                if scenario == "copy":
                    patcher.setattr(syncer.shutil, "copy2", interrupted_copy)
                else:
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

            await driver.close()
            driver = OpenListDriver(driver.config)
            driver.client = SimpleNamespace()
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
                host="localhost", port=80, auth={"token": "test"}, tool="115 Open"
            )
        )
        client = Client()
        driver.client = client
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
            action, state = await download_service.DownloadTaskService.delete(
                task.id, local=True
            )
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


async def _run_download_plan(monkeypatch, driver):
    magnet = f"magnet:?xt=urn:btih:{'a' * 40}"
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
            lambda: SimpleNamespace(ctx=SimpleNamespace(flow_engine=engine)),
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


def test_driver_sync():
    driver = SimpleNamespace(sync=AsyncMock(return_value=()))
    task = cast(
        DownloadTask,
        SimpleNamespace(
            id=1,
            unique_id="remote-1",
            info_hash="1" * 40,
            info_hash_v2=None,
        ),
    )

    asyncio.run(syncer.sync_tasks([task], driver))

    driver.sync.assert_awaited_once_with((DownloadIdentity.from_task(task),))


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
                            identity=DownloadIdentity.from_task(task),
                            state=DownloadState.ERROR,
                            error="remote transfer failed",
                        ),
                    )
                )
            )

            await syncer.sync_tasks([task], driver)
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
    monkeypatch.setattr(
        syncer, "DownloadTask", SimpleNamespace(filter=AsyncMock(return_value=[task]))
    )
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
