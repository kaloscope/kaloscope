import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.dl.driver import (
    DownloadAction,
    DownloadDraft,
    DownloadIdentity,
    DownloadSnapshot,
    DownloadState,
    OfflineJobDraft,
)
from app.core.dl.rpc import RpcConfig, RpcDriver
from app.core.dl.rpc.models import API
from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.download import (
    DownloadAdd,
    Downloader,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)
from app.services import download as download_service


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


class DraftDriver:
    source_types = frozenset({"raw", "magnet", "torrent"})
    job_uuid = "1234567890ab4def81234567890abcde"

    def __init__(
        self,
        *,
        fail: bool = False,
        remote_id: str | None = "remote-1",
        state: DownloadState | None = None,
    ):
        self.fail = fail
        self.remote_id = remote_id
        self.state = state

    def prepare(self, request):
        identity = request.identity
        source = request.link
        assert identity is not None
        assert source is not None
        name = identity.info_hash or identity.info_hash_v2
        assert name is not None
        remote_directory = f"/Kaloscope/{self.job_uuid}"
        topics = []
        if identity.info_hash:
            topics.append(f"xt=urn:btih:{identity.info_hash}")
        if identity.info_hash_v2:
            topics.append(f"xt=urn:btmh:{identity.info_hash_v2}")
        return DownloadDraft(
            request=replace(request, remote_directory=remote_directory),
            name=name,
            state=DownloadState.SUBMITTING,
            magnet_link="magnet:?" + "&".join(topics),
            percentage=0,
            job=OfflineJobDraft(
                job_uuid=self.job_uuid,
                source_fingerprint=hashlib.sha256(source.encode()).hexdigest(),
                remote_directory=remote_directory,
            ),
        )

    async def add(self, request):
        task = await DownloadTask.get(id=request.identity.task_id)
        job = await OfflineDownloadJob.get(download_id=task.id)
        assert request.remote_directory == job.remote_dir
        if self.fail:
            raise RuntimeError("simulated submission failure")
        state = self.state or (
            DownloadState.REMOTE if self.remote_id else DownloadState.SETTLING
        )
        return DownloadSnapshot(
            identity=replace(request.identity, remote_id=self.remote_id),
            state=state,
            error=(
                "OpenList submission result is unknown"
                if state is DownloadState.SUBMIT_UNKNOWN
                else None
            ),
            percentage=50 if state is DownloadState.SETTLING else 0,
        )


async def _create_offline_task(state, *, error_kind=None):
    downloader = await Downloader.create(config="invalid", name="OpenList", priority=1)
    task = await DownloadTask.create(
        downloader=downloader,
        dir="/downloads",
        name="Example",
        unique_id="remote-1",
        state=state,
    )
    await OfflineDownloadJob.create(
        download=task,
        job_uuid="1234567890ab4def81234567890abcde",
        source_fingerprint="a" * 64,
        remote_dir="/Kaloscope/1234567890ab4def81234567890abcde",
        last_error_kind=error_kind,
    )
    return task


@pytest.mark.parametrize(
    ("remote_id", "expected_state", "expected_percentage", "expected_error_kind"),
    [
        ("remote-1", DownloadState.REMOTE, 0, None),
        (None, DownloadState.SETTLING, 50, None),
        (
            None,
            DownloadState.SUBMIT_UNKNOWN,
            0,
            OfflineDownloadErrorKind.SUBMIT_UNKNOWN,
        ),
    ],
)
def test_draft_add(
    monkeypatch,
    remote_id,
    expected_state,
    expected_percentage,
    expected_error_kind,
):
    config = """driver: openlist
name: OpenList
protocol: https
host: openlist.example.com
port: 443
path: /api
auth:
  token: private-token
tool: Future Tool
"""
    source = (
        "magnet:?xt=urn:btih:1111111111111111111111111111111111111111"
        "&dn=private-title&tr=https%3A%2F%2Ftracker.example%2Fannounce"
    )
    driver = DraftDriver(remote_id=remote_id, state=expected_state)
    monkeypatch.setattr(download_service, "load_driver", lambda _config: driver)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config=config, name="OpenList", priority=1
            )
            created_task = await download_service.DownloadTaskService.add(
                DownloadAdd(
                    downloader_id=downloader.id,
                    dir="/downloads",
                    link=source,
                )
            )
            task = await DownloadTask.get(id=created_task.id)
            job = await OfflineDownloadJob.get(download_id=task.id)
            return task, job, await DownloadTask.all().count()
        finally:
            await Tortoise.close_connections()

    task, job, task_count = asyncio.run(run())

    assert task.state == expected_state
    assert task.percentage == expected_percentage
    assert task.unique_id == remote_id
    assert task.error_msg == (
        "OpenList submission result is unknown" if expected_error_kind else None
    )
    assert task_count == 1
    assert task.magnet_link == "magnet:?xt=urn:btih:" + "1" * 40
    assert job.download_id == task.id
    assert job.source_fingerprint == (
        "0094f242b6757381be7457c10146c86c629014fcc8ce216832330ee3c00c65f5"
    )
    assert job.job_uuid == DraftDriver.job_uuid
    assert job.remote_dir == f"/Kaloscope/{DraftDriver.job_uuid}"
    assert job.last_error_kind == expected_error_kind


def test_add_rollback(monkeypatch):
    driver = DraftDriver(fail=True)
    monkeypatch.setattr(download_service, "load_driver", lambda _config: driver)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="invalid", name="OpenList", priority=1
            )
            with pytest.raises(RuntimeError, match="simulated submission failure"):
                await download_service.DownloadTaskService.add(
                    DownloadAdd(
                        downloader_id=downloader.id,
                        dir="/downloads",
                        link="magnet:?xt=urn:btih:" + "3" * 40,
                    )
                )
            return (
                await DownloadTask.all().count(),
                await OfflineDownloadJob.all().count(),
            )
        finally:
            await Tortoise.close_connections()

    assert asyncio.run(run()) == (0, 0)


def test_concurrent_rpc_add(monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        submitted = []

        class Client:
            async def call(self, method, variables):
                submitted.append(variables["link"])
                await asyncio.sleep(0.05)
                return {"unique_id": "remote-1"}

        driver = RpcDriver(
            RpcConfig(
                name="RPC", host="localhost", port=80, methods={"add_link": API()}
            )
        )
        driver.client = Client()
        monkeypatch.setattr(download_service, "load_driver", lambda _: driver)
        try:
            downloaders = [
                await Downloader.create(config="rpc", name=str(i), priority=i)
                for i in (1, 2)
            ]
            outcomes = await asyncio.gather(
                *(
                    download_service.DownloadTaskService.add(
                        DownloadAdd(
                            downloader_id=downloader.id,
                            dir="/downloads",
                            link="magnet:?xt=urn:btih:" + "1" * 40,
                        )
                    )
                    for downloader in downloaders
                ),
                return_exceptions=True,
            )
            assert await DownloadTask.all().count() == 1
            assert len(submitted) == 1
            errors = [value for value in outcomes if isinstance(value, Exception)]
            assert len(errors) == 1
            assert isinstance(errors[0], KaloscopeException)
            assert errors[0].message == ErrorCode.INFO_HASH_COLLISION
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_unsupported_action(monkeypatch):
    task = SimpleNamespace(
        id=1,
        downloader_id=2,
        unique_id="task-1",
        info_hash="hash-v1",
        info_hash_v2=None,
        name="Example",
        dir="/downloads",
        state=DownloadState.PULLING,
    )
    downloader = SimpleNamespace(config="invalid", preset=None)
    query = SimpleNamespace(update=AsyncMock())
    task_model = SimpleNamespace(
        get=AsyncMock(return_value=task),
        filter=lambda **_filters: query,
    )
    downloader_model = SimpleNamespace(get=AsyncMock(return_value=downloader))
    driver = SimpleNamespace(capabilities=AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(download_service, "DownloadTask", task_model)
    monkeypatch.setattr(download_service, "Downloader", downloader_model)
    monkeypatch.setattr(
        download_service,
        "load_driver",
        lambda _config: driver,
    )
    monkeypatch.setattr(
        download_service,
        "OfflineDownloadJob",
        SimpleNamespace(get_or_none=AsyncMock(return_value=None)),
    )

    asyncio.run(download_service.DownloadTaskService.pause(1))

    query.update.assert_not_awaited()


def test_deferred_delete(monkeypatch):
    driver = SimpleNamespace(
        capabilities=AsyncMock(return_value=frozenset({DownloadAction.DELETE})),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(download_service, "load_driver", lambda _config: driver)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task = await _create_offline_task(DownloadState.REMOTE)
            command = await download_service.DownloadTaskService.delete(task.id, True)
            stored = await DownloadTask.get_or_none(id=task.id)
            return command, stored.state if stored else None
        finally:
            await Tortoise.close_connections()

    command, stored_state = asyncio.run(run())

    assert command == (DownloadAction.DELETE, DownloadState.REMOTE)
    assert stored_state is DownloadState.REMOTE
    driver.delete.assert_not_awaited()


def test_retry_target(monkeypatch):
    driver = SimpleNamespace(
        capabilities=AsyncMock(return_value=frozenset({DownloadAction.RETRY}))
    )
    monkeypatch.setattr(download_service, "load_driver", lambda _config: driver)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            task = await _create_offline_task(
                DownloadState.ERROR,
                error_kind=OfflineDownloadErrorKind.PULL_FAILED,
            )
            command = await download_service.DownloadTaskService.retry(task.id)
            await task.refresh_from_db()
            return command, task
        finally:
            await Tortoise.close_connections()

    command, task = asyncio.run(run())

    assert command == (DownloadAction.RETRY, DownloadState.ERROR)
    assert task.state is DownloadState.ERROR
    driver.capabilities.assert_awaited_once_with(
        DownloadIdentity.from_task(task),
        DownloadState.ERROR,
        retry_target=DownloadState.PULLING,
    )


def test_task_dump():
    config = """driver: openlist
protocol: https
host: openlist.example.com
port: 443
path: /api
auth:
  token: private-token
tool: Future Tool
"""
    cases = (
        (DownloadState.REMOTE, None, [DownloadAction.CANCEL, DownloadAction.DELETE]),
        (DownloadState.PULLING, None, [DownloadAction.PAUSE, DownloadAction.DELETE]),
        (DownloadState.PAUSED, None, [DownloadAction.RESUME, DownloadAction.DELETE]),
        (
            DownloadState.ERROR,
            OfflineDownloadErrorKind.REMOTE_FAILED,
            [DownloadAction.RETRY, DownloadAction.DELETE],
        ),
    )

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config=config, name="Cloud", priority=1
            )
            tasks = []
            for index, (state, error_kind, _expected) in enumerate(cases, start=1):
                task = await DownloadTask.create(
                    downloader=downloader,
                    dir="/downloads",
                    name=f"Task {index}",
                    unique_id=f"remote-{index}",
                    state=state,
                    percentage=index * 10,
                )
                await OfflineDownloadJob.create(
                    download=task,
                    job_uuid=f"{index:032x}",
                    source_fingerprint=f"{index}" * 64,
                    remote_dir=f"/Kaloscope/{index:032x}",
                    last_error_kind=error_kind,
                )
                tasks.append(task)
            return await download_service.DownloadTaskService.dump_list(tasks)
        finally:
            await Tortoise.close_connections()

    data = asyncio.run(run())

    assert [item.get("capabilities") for item in data] == [
        expected for _state, _error_kind, expected in cases
    ]
    assert [item.get("error_kind") for item in data] == [
        error_kind for _state, error_kind, _expected in cases
    ]
    assert [item["state"] for item in data] == [case[0] for case in cases]
    assert [item["percentage"] for item in data] == [10, 20, 30, 40]
    assert all("offline_job" not in item for item in data)
