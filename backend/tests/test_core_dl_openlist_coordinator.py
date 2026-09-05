import asyncio
import hashlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import SecretStr
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.dl import syncer as syncer_module
from app.core.dl.driver import DownloadIdentity, DownloadState
from app.core.dl.openlist import coordinator as coordinator_module
from app.core.dl.openlist import state as state_module
from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.coordinator import OpenListCoordinator
from app.core.dl.openlist.models import (
    OpenListAuth,
    OpenListConfig,
    OpenListErrorKind,
    RemoteEntry,
    RemoteEntryPage,
    RemoteTask,
    RemoteTaskState,
)
from app.core.dl.openlist.puller import PullError
from app.core.dl.openlist.runtime import OpenListPullRuntime
from app.models.download import (
    Downloader,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)


def test_submission_process_exit(tmp_path, monkeypatch):
    config_path = Path(__file__).resolve().parents[1] / "app/config.toml"
    config = KaloscopeConfig(config_path, tmp_path)
    monkeypatch.setattr(KaloscopeConfig, "_config", config)
    lock_dir = tmp_path / "workspace/temp"
    assert Path(config.get_workspace("temp")) == lock_dir
    lock_dir.mkdir(parents=True)
    program = """
import os
import sys
from pathlib import Path
from filelock import Timeout
from app.core.config import KaloscopeConfig
from app.core.dl import syncer

KaloscopeConfig._config = KaloscopeConfig(Path('app/config.toml'), Path(sys.argv[1]))
try:
    with syncer.submission_lock('1' * 32):
        os._exit(73)
except Timeout:
    os._exit(74)
"""
    command = [sys.executable, "-c", program, str(tmp_path)]
    options = {
        "cwd": Path(__file__).resolve().parents[1],
        "check": False,
        "timeout": 30,
    }
    with syncer_module.submission_lock("1" * 32) as held:
        name = hashlib.sha256(("1" * 32).encode()).hexdigest()
        assert Path(held.lock_file) == lock_dir / f"openlist_{name}.lock"
        assert subprocess.run(command, **options).returncode == 74
    assert subprocess.run(command, **options).returncode == 73
    with syncer_module.submission_lock("1" * 32) as held:
        assert held.is_locked


class RecordingClient:
    def __init__(
        self,
        *,
        undone=(),
        done=(),
        transfer_undone=(),
        transfer_done=(),
        pages=None,
    ):
        self.undone_tasks = tuple(undone)
        self.done_tasks = tuple(done)
        self.transfer_undone_tasks = tuple(transfer_undone)
        self.transfer_done_tasks = tuple(transfer_done)
        self.pages = pages or {}
        self.calls: list[object] = []

    async def undone(self) -> tuple[RemoteTask, ...]:
        self.calls.append("undone")
        return self.undone_tasks

    async def done(self) -> tuple[RemoteTask, ...]:
        self.calls.append("done")
        return self.done_tasks

    async def transfer_undone(self) -> tuple[RemoteTask, ...]:
        self.calls.append("transfer_undone")
        return self.transfer_undone_tasks

    async def transfer_done(self) -> tuple[RemoteTask, ...]:
        self.calls.append("transfer_done")
        return self.transfer_done_tasks

    async def list(
        self, path: str, page: int, per_page: int, refresh: bool = False
    ) -> RemoteEntryPage:
        self.calls.append(("list", path, page, refresh))
        result = self.pages[(path, page)]
        if isinstance(result, OpenListClientError):
            raise result
        return result


class RecordingPullRuntime:
    def start(self, _job: OfflineDownloadJob):
        pass


class RecoveringClient(RecordingClient):
    def __init__(self, errors, **kwargs):
        super().__init__(**kwargs)
        self.errors = list(errors)

    async def undone(self) -> tuple[RemoteTask, ...]:
        self.calls.append("undone")
        if self.errors:
            raise self.errors.pop(0)
        return self.undone_tasks


def _config(token: str = "private-token", **overrides) -> OpenListConfig:
    return OpenListConfig(
        protocol="https",
        host="openlist.example.com",
        port=443,
        auth=OpenListAuth(token=SecretStr(token)),
        tool="115 Open",
        **overrides,
    )


def _coordinator(monkeypatch, config, client, clock) -> OpenListCoordinator:
    monkeypatch.setattr(coordinator_module, "now", clock)
    monkeypatch.setattr(state_module, "random", lambda: 0.5)
    return OpenListCoordinator(
        config,
        cast(OpenListClient, client),
        cast(OpenListPullRuntime, RecordingPullRuntime()),
    )


def _remote(
    task_id: str,
    state: RemoteTaskState,
    *,
    name: str | None = None,
    progress: float = 0,
    total_bytes: int = 0,
    error: str = "",
) -> RemoteTask:
    return RemoteTask(
        id=task_id,
        name=name or f"remote-{task_id}",
        state=state,
        progress=progress,
        total_bytes=total_bytes,
        error=error,
    )


async def _create_job(
    downloader: Downloader,
    index: int,
    remote_id: str | None,
    state: DownloadState = DownloadState.REMOTE,
) -> tuple[DownloadTask, OfflineDownloadJob]:
    task = await DownloadTask.create(
        downloader=downloader,
        dir="/downloads",
        name=f"task-{index}",
        unique_id=remote_id,
        info_hash=f"{index:040x}",
        state=state,
        percentage=0,
    )
    value = f"{index:032x}"
    job = await OfflineDownloadJob.create(
        download=task,
        job_uuid=value,
        source_fingerprint="a" * 64,
        remote_dir=f"/Kaloscope/{value}",
    )
    return task, job


def test_submission_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))
    now = datetime(2026, 8, 5, tzinfo=UTC)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            task1, job1 = await _create_job(
                downloader, 1, None, DownloadState.SUBMITTING
            )
            task1.created_at = now - timedelta(seconds=31)
            await task1.save(update_fields=["created_at"])
            task2, job2 = await _create_job(
                downloader, 2, None, DownloadState.SUBMIT_UNKNOWN
            )
            task3, job3 = await _create_job(
                downloader, 3, None, DownloadState.SUBMIT_UNKNOWN
            )
            sources = [f"https://example.com/{index}" for index in range(1, 4)]
            for job, source in zip((job1, job2, job3), sources, strict=True):
                job.source_fingerprint = hashlib.sha256(source.encode()).hexdigest()
                await job.save(update_fields=["source_fingerprint"])
            client = RecordingClient(
                undone=(
                    _remote(
                        "remote-1",
                        RemoteTaskState.RUNNING,
                        name=f"download {sources[0]} to ({job1.remote_dir})",
                    ),
                ),
                done=(
                    _remote(
                        "duplicate-1",
                        RemoteTaskState.SUCCEEDED,
                        name=f"download {sources[2]} to ({job3.remote_dir})",
                    ),
                    _remote(
                        "duplicate-2",
                        RemoteTaskState.SUCCEEDED,
                        name=f"download {sources[2]} to ({job3.remote_dir})",
                    ),
                ),
                pages={
                    (job2.remote_dir, 1): RemoteEntryPage(
                        content=[RemoteEntry(name="movie.mkv", size=1, is_dir=False)],
                        total=1,
                    ),
                    (job3.remote_dir, 1): RemoteEntryPage(content=[], total=0),
                },
            )
            tasks = (task1, task2, task3)
            snapshots = await _coordinator(
                monkeypatch, _config(), client, lambda: now
            ).sync(tuple(DownloadIdentity.from_task(task) for task in tasks))
            for task in tasks:
                await task.refresh_from_db()
            for job in (job1, job2, job3):
                await job.refresh_from_db()
            return snapshots, client, tasks, (job1, job2, job3)
        finally:
            await Tortoise.close_connections()

    snapshots, client, tasks, jobs = asyncio.run(run())

    assert [snapshot.state for snapshot in snapshots] == [
        DownloadState.REMOTE,
        DownloadState.SETTLING,
        DownloadState.SUBMIT_UNKNOWN,
    ]
    assert tasks[0].unique_id == "remote-1"
    assert tasks[1].percentage == 100
    assert tasks[2].error_msg == "OpenList submission result is unknown"
    assert jobs[2].last_error_kind is OfflineDownloadErrorKind.SUBMIT_UNKNOWN
    assert jobs[2].next_poll_at == now + timedelta(seconds=30)
    assert client.calls == [
        "undone",
        "done",
        ("list", jobs[1].remote_dir, 1, False),
        ("list", jobs[2].remote_dir, 1, False),
    ]


def test_poll_batching(monkeypatch):
    now = datetime(2026, 8, 4, tzinfo=UTC)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            task1, job1 = await _create_job(downloader, 1, "remote-1")
            task2, job2 = await _create_job(downloader, 2, "remote-2")
            task3, job3 = await _create_job(downloader, 3, "pruned-3")
            client = RecordingClient(
                undone=(_remote("remote-1", RemoteTaskState.RUNNING, progress=20),),
                done=(_remote("remote-2", RemoteTaskState.SUCCEEDED),),
                pages={
                    (job3.remote_dir, 1): RemoteEntryPage(
                        content=[RemoteEntry(name="movie.mkv", size=3, is_dir=False)],
                        total=1,
                    )
                },
            )
            coordinator = _coordinator(monkeypatch, _config(), client, lambda: now)
            tasks = (task1, task2, task3)
            jobs = (job1, job2, job3)
            identities = tuple(DownloadIdentity.from_task(task) for task in tasks)

            snapshots = await coordinator.sync(identities)
            gated = await coordinator.sync(identities)
            for task in tasks:
                await task.refresh_from_db()
            for job in jobs:
                await job.refresh_from_db()
            return snapshots, gated, client, tasks, jobs
        finally:
            await Tortoise.close_connections()

    snapshots, gated, client, tasks, jobs = asyncio.run(run())

    assert client.calls == [
        "undone",
        "done",
        ("list", jobs[2].remote_dir, 1, False),
    ]
    assert gated == ()
    assert [snapshot.state for snapshot in snapshots] == [
        DownloadState.REMOTE,
        DownloadState.SETTLING,
        DownloadState.SETTLING,
    ]
    assert [(task.raw_state, task.percentage) for task in tasks] == [
        ("running", 20),
        ("succeeded", 100),
        (None, 100),
    ]
    assert [job.next_poll_at for job in jobs] == [
        now + timedelta(seconds=10),
        now + timedelta(seconds=10),
        now + timedelta(seconds=10),
    ]
    assert jobs[2].last_error_kind is None


def test_refresh_shared(monkeypatch):
    now = datetime(2026, 8, 4, tzinfo=UTC)
    first = _coordinator(monkeypatch, _config(), RecordingClient(), lambda: now)
    second = _coordinator(monkeypatch, _config(), RecordingClient(), lambda: now)

    assert first.refresh_limiter.acquire("job-1", now)
    assert not second.refresh_limiter.acquire("job-2", now)
    assert not first.refresh_limiter.acquire("job-1", now + timedelta(minutes=1))
    assert second.refresh_limiter.acquire("job-2", now + timedelta(minutes=1))
    assert first.refresh_limiter.acquire("job-1", now + timedelta(minutes=2))
    assert not first.refresh_limiter.acquire("job-1", now + timedelta(minutes=2))
    assert first.refresh_limiter.acquire("job-1", now + timedelta(minutes=3))


def test_task_errors(monkeypatch):
    now = datetime(2026, 8, 4, tzinfo=UTC)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            remote_task, remote_job = await _create_job(downloader, 1, "remote-1")
            transfer_task, transfer_job = await _create_job(
                downloader, 2, "remote-2", DownloadState.SETTLING
            )
            client = RecordingClient(
                done=(
                    _remote(
                        "remote-1",
                        RemoteTaskState.FAILED,
                        error="115 download failed",
                    ),
                ),
                transfer_done=(
                    _remote(
                        "transfer-2",
                        RemoteTaskState.FAILED,
                        name=f"transfer {transfer_job.job_uuid}",
                        error="115 upload failed",
                    ),
                ),
            )
            tasks = (remote_task, transfer_task)
            snapshots = await _coordinator(
                monkeypatch, _config(), client, lambda: now
            ).sync(tuple(DownloadIdentity.from_task(task) for task in tasks))
            for task in tasks:
                await task.refresh_from_db()
            for job in (remote_job, transfer_job):
                await job.refresh_from_db()
            return snapshots, tasks, (remote_job, transfer_job)
        finally:
            await Tortoise.close_connections()

    snapshots, tasks, jobs = asyncio.run(run())

    assert [snapshot.error for snapshot in snapshots] == [
        "115 download failed",
        "115 upload failed",
    ]
    assert [task.error_msg for task in tasks] == [
        "115 download failed",
        "115 upload failed",
    ]
    assert [job.last_error_kind for job in jobs] == [
        OfflineDownloadErrorKind.REMOTE_FAILED,
        OfflineDownloadErrorKind.TRANSFER_FAILED,
    ]


def test_job_isolation(monkeypatch):
    current = datetime(2026, 8, 4, tzinfo=UTC)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            task1, job1 = await _create_job(
                downloader, 1, "remote-1", DownloadState.SETTLING
            )
            task2, job2 = await _create_job(
                downloader, 2, "remote-2", DownloadState.SETTLING
            )
            task3, job3 = await _create_job(
                downloader, 3, "remote-3", DownloadState.SETTLING
            )
            client = RecordingClient(
                pages={
                    (job1.remote_dir, 1): OpenListClientError(OpenListErrorKind.API),
                    (job2.remote_dir, 1): RemoteEntryPage(
                        content=[RemoteEntry(name="movie.mkv", size=3, is_dir=False)],
                        total=1,
                    ),
                    (job3.remote_dir, 1): RemoteEntryPage(
                        content=[RemoteEntry(name="movie.mkv", size=3, is_dir=False)],
                        total=1,
                    ),
                }
            )
            reconcile = coordinator_module.reconcile_local_manifest

            def reconcile_job(job, entries):
                if job.id == job3.id:
                    raise PullError(
                        OfflineDownloadErrorKind.PULL_FAILED,
                        "Local file could not be deleted",
                    )
                return reconcile(job, entries)

            monkeypatch.setattr(
                coordinator_module, "reconcile_local_manifest", reconcile_job
            )
            tasks = (task1, task2, task3)
            jobs = (job1, job2, job3)
            snapshots = await _coordinator(
                monkeypatch, _config(), client, lambda: current
            ).sync(tuple(DownloadIdentity.from_task(task) for task in tasks))
            for task in tasks:
                await task.refresh_from_db()
            for job in jobs:
                await job.refresh_from_db()
            return snapshots, client, tasks, jobs
        finally:
            await Tortoise.close_connections()

    snapshots, client, tasks, jobs = asyncio.run(run())

    assert [snapshot.identity.task_id for snapshot in snapshots] == [
        jobs[1].download_id,
        jobs[2].download_id,
    ]
    assert jobs[0].last_error_kind is OfflineDownloadErrorKind.INSTANCE_TRANSIENT
    assert jobs[0].next_poll_at == current + timedelta(seconds=30)
    assert jobs[1].last_error_kind is None
    assert tasks[2].state is DownloadState.ERROR
    assert tasks[2].error_msg == "Local file could not be deleted"
    assert jobs[2].last_error_kind is OfflineDownloadErrorKind.PULL_FAILED
    assert jobs[2].next_poll_at is None
    assert ("list", jobs[1].remote_dir, 1, False) in client.calls


def test_auth_block(monkeypatch):
    now = datetime(2026, 8, 4, tzinfo=UTC)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            task, job = await _create_job(downloader, 1, "remote-1")
            client = RecoveringClient(
                [OpenListClientError(OpenListErrorKind.AUTH)],
                undone=(_remote("remote-1", RemoteTaskState.RUNNING, progress=20),),
            )
            coordinator = _coordinator(monkeypatch, _config(), client, lambda: now)
            identity = DownloadIdentity.from_task(task)

            failed = await coordinator.sync((identity,))
            blocked = await coordinator.sync((identity,))
            replacement = _coordinator(
                monkeypatch, _config("updated-token"), client, lambda: now
            )
            recovered = await replacement.sync((identity,))
            await task.refresh_from_db()
            await job.refresh_from_db()
            return failed, blocked, recovered, client, task, job
        finally:
            await Tortoise.close_connections()

    failed, blocked, recovered, client, task, job = asyncio.run(run())

    assert failed == blocked == ()
    assert recovered[0].state is DownloadState.REMOTE
    assert client.calls == ["undone", "undone"]
    assert task.state is DownloadState.REMOTE
    assert job.last_error_kind is None
    assert job.retry_count == 0
    assert job.next_poll_at == now + timedelta(seconds=10)


def test_rate_limit(monkeypatch):
    now = datetime(2026, 8, 4, tzinfo=UTC)
    current = [now]

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            task1, job1 = await _create_job(downloader, 1, "remote-1")
            task2, job2 = await _create_job(downloader, 2, "remote-2")
            client = RecoveringClient(
                [OpenListClientError(OpenListErrorKind.RATE_LIMIT, retry_after="120")]
            )
            coordinator = _coordinator(
                monkeypatch, _config(), client, lambda: current[0]
            )
            identities = (
                DownloadIdentity.from_task(task1),
                DownloadIdentity.from_task(task2),
            )

            snapshots = await coordinator.sync(identities)
            current[0] = now + timedelta(seconds=119)
            gated = await coordinator.sync(identities)
            await task1.refresh_from_db()
            await task2.refresh_from_db()
            await job1.refresh_from_db()
            await job2.refresh_from_db()
            return snapshots, gated, client, task1, task2, job1, job2
        finally:
            await Tortoise.close_connections()

    snapshots, gated, client, task1, task2, job1, job2 = asyncio.run(run())

    assert snapshots == gated == ()
    assert client.calls == ["undone"]
    assert task1.state is task2.state is DownloadState.REMOTE
    assert job1.last_error_kind is job2.last_error_kind
    assert job1.last_error_kind is OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT
    assert job1.retry_count == job2.retry_count == 1
    assert job1.next_poll_at == job2.next_poll_at == now + timedelta(seconds=120)


def test_transient_backoff(monkeypatch):
    now = datetime(2026, 8, 4, tzinfo=UTC)
    current = [now]

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            task, job = await _create_job(downloader, 1, "remote-1")
            client = RecoveringClient(
                [
                    OpenListClientError(OpenListErrorKind.TRANSIENT),
                    OpenListClientError(OpenListErrorKind.TRANSIENT),
                ],
                undone=(_remote("remote-1", RemoteTaskState.RUNNING, progress=20),),
            )
            coordinator = _coordinator(
                monkeypatch, _config(), client, lambda: current[0]
            )
            identity = DownloadIdentity.from_task(task)

            await coordinator.sync((identity,))
            current[0] = now + timedelta(seconds=30)
            await coordinator.sync((identity,))
            await job.refresh_from_db()
            backed_off = (job.last_error_kind, job.retry_count, job.next_poll_at)
            current[0] = now + timedelta(seconds=90)
            await coordinator.sync((identity,))
            await job.refresh_from_db()
            return task, backed_off, job
        finally:
            await Tortoise.close_connections()

    task, backed_off, job = asyncio.run(run())

    assert task.state is DownloadState.REMOTE
    assert backed_off == (
        OfflineDownloadErrorKind.INSTANCE_TRANSIENT,
        2,
        now + timedelta(seconds=90),
    )
    assert job.last_error_kind is None
    assert job.retry_count == 0
    assert job.next_poll_at == now + timedelta(seconds=100)
