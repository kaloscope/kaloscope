import asyncio
from typing import cast

import pytest
from pydantic import SecretStr
from tortoise import Tortoise, timezone

from app.core.dl.driver import (
    DownloadIdentity,
    DownloadRequest,
    DownloadState,
)
from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.driver import OpenListDriver
from app.core.dl.openlist.manifest import RemoteManifestEntry, serialize_manifest
from app.core.dl.openlist.models import (
    OpenListAuth,
    OpenListConfig,
    OpenListErrorKind,
    RemoteCleanupPolicy,
    RemoteTask,
    RemoteTaskState,
)
from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.download import (
    Downloader,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)

_JOB_UUID = "1234567890ab4def81234567890abcde"
_OTHER_JOB_UUID = "abcdefabcdef4def8abcdefabcdefabc"


def _config(tool: str = "Future Tool", **overrides) -> OpenListConfig:
    return OpenListConfig(
        protocol="https",
        host="openlist.example.com",
        port=443,
        auth=OpenListAuth(token=SecretStr("private-token")),
        tool=tool,
        **overrides,
    )


def _driver(config: OpenListConfig, client: object) -> OpenListDriver:
    driver = OpenListDriver(config)
    driver.client = cast(OpenListClient, client)
    return driver


class ValidationClient:
    def __init__(
        self,
        *,
        writable=True,
        version="v4.1.2",
        submit_error: OpenListClientError | None = None,
        validate_error: OpenListClientError | None = None,
    ):
        self.writable = writable
        self.returned_version = version
        self.submit_error = submit_error
        self.validate_error = validate_error
        self.calls: list[object] = []

    async def tools(self, path: str) -> tuple[str, ...]:
        self.calls.append(("tools", path))
        return ("115 Open", "Future Tool")

    async def remote_root_writable(self) -> bool:
        self.calls.append("remote_root_writable")
        if self.validate_error:
            raise self.validate_error
        return self.writable

    async def require_admin(self):
        self.calls.append("require_admin")

    async def version(self) -> str:
        self.calls.append("version")
        return self.returned_version

    async def mkdir(self, path: str):
        self.calls.append(("mkdir", path))

    async def submit(self, source: str, path: str) -> tuple[str, ...]:
        self.calls.append(("submit", source, path))
        if self.submit_error:
            raise self.submit_error
        return ("remote-1",)

    async def remove(self, directory: str, name: str):
        self.calls.append(("remove", directory, name))


class TransferClient:
    def __init__(self, active: RemoteTask, failed: RemoteTask):
        self.active = active
        self.failed = failed
        self.calls: list[object] = []

    async def transfer_undone(self) -> tuple[RemoteTask, ...]:
        self.calls.append("transfer_undone")
        return (self.active,)

    async def transfer_done(self) -> tuple[RemoteTask, ...]:
        self.calls.append("transfer_done")
        return (self.failed,)

    async def cancel_transfer(self, task_id: str):
        self.calls.append(("cancel_transfer", task_id))

    async def retry_transfer(self, task_id: str):
        self.calls.append(("retry_transfer", task_id))


class DeleteClient:
    def __init__(
        self,
        *tasks: RemoteTask,
        transfers: tuple[RemoteTask, ...] = (),
    ):
        self.tasks = tasks
        self.transfers = transfers
        self.canceled: list[str] = []
        self.canceled_transfers: list[str] = []

    async def undone(self) -> tuple[RemoteTask, ...]:
        return self.tasks

    async def cancel(self, task_id: str):
        self.canceled.append(task_id)

    async def transfer_undone(self) -> tuple[RemoteTask, ...]:
        return self.transfers

    async def cancel_transfer(self, task_id: str):
        self.canceled_transfers.append(task_id)


def test_validate():
    client = ValidationClient()
    driver = _driver(_config(), client)

    version = asyncio.run(driver.validate())

    assert version == "v4.1.2"
    assert client.calls == [
        ("tools", "/Kaloscope"),
        "require_admin",
        "remote_root_writable",
        "version",
    ]


def test_read_only_root():
    driver = _driver(_config(), ValidationClient(writable=False))

    with pytest.raises(KaloscopeException) as caught:
        asyncio.run(driver.validate())

    assert caught.value.message == ErrorCode.INVALID_YAML_CONFIG


@pytest.mark.parametrize(
    ("response_message", "expected"),
    [
        ("remote root is unavailable", "remote root is unavailable"),
        (None, ErrorCode.HTTP_REQUEST_FAILED),
    ],
)
def test_validate_error(response_message: str | None, expected: str):
    error = OpenListClientError(
        OpenListErrorKind.API, response_message=response_message
    )
    driver = _driver(_config(), ValidationClient(validate_error=error))

    with pytest.raises(KaloscopeException) as caught:
        asyncio.run(driver.validate())

    assert caught.value.message == expected


def test_cleanup():
    client = ValidationClient()
    driver = _driver(
        _config(remote_cleanup=RemoteCleanupPolicy.DELETE_ON_SUCCESS),
        client,
    )

    asyncio.run(
        driver._cleanup_remote(
            state=DownloadState.COMPLETED,
            job_uuid=_JOB_UUID,
            remote_directory=f"/Kaloscope/{_JOB_UUID}",
            owner_count=1,
        )
    )

    assert client.calls == [("remove", "/Kaloscope", _JOB_UUID)]

    cases = (
        (_JOB_UUID, "/Kaloscope", 1),
        (_JOB_UUID, f"/Kaloscope/{_OTHER_JOB_UUID}", 1),
    )
    for job_uuid, remote_directory, owner_count in cases:
        with pytest.raises(ValueError):
            asyncio.run(
                driver._cleanup_remote(
                    state=DownloadState.COMPLETED,
                    job_uuid=job_uuid,
                    remote_directory=remote_directory,
                    owner_count=owner_count,
                )
            )
    assert client.calls == [("remove", "/Kaloscope", _JOB_UUID)]


@pytest.mark.parametrize("valid", [True, False])
def test_cleanup_recovery(valid):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="OpenList", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir="/downloads",
                name="movie",
                state=DownloadState.COMPLETED,
            )
            job = await OfflineDownloadJob.create(
                download=task,
                job_uuid=_JOB_UUID,
                source_fingerprint="1" * 64,
                remote_dir=f"/Kaloscope/{_JOB_UUID}" if valid else "/Kaloscope",
                next_poll_at=timezone.now(),
            )
            client = ValidationClient()
            driver = _driver(
                _config(
                    remote_root="/NewRoot",
                    remote_cleanup=RemoteCleanupPolicy.DELETE_ON_SUCCESS,
                ),
                client,
            )
            await driver._retry_cleanup((DownloadIdentity.from_task(task),))
            await job.refresh_from_db()
            if valid:
                assert client.calls == [("remove", "/Kaloscope", _JOB_UUID)]
                assert job.next_poll_at is None
                assert job.last_error_kind is None
            else:
                assert client.calls == []
                assert job.last_error_kind is OfflineDownloadErrorKind.CLEANUP_FAILED
                assert job.next_poll_at is not None
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_delete_failed_transfer():
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="OpenList", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir="/downloads",
                name="movie",
                state=DownloadState.ERROR,
            )
            await OfflineDownloadJob.create(
                download=task,
                job_uuid=_JOB_UUID,
                source_fingerprint="1" * 64,
                remote_dir=f"/Kaloscope/{_JOB_UUID}",
                last_error_kind=OfflineDownloadErrorKind.TRANSFER_FAILED,
            )
            client = DeleteClient(
                transfers=tuple(
                    RemoteTask(
                        id=job_id,
                        name=f"transfer to ({job_id})",
                        state=RemoteTaskState.RUNNING,
                        progress=10,
                        total_bytes=100,
                    )
                    for job_id in (_JOB_UUID, _OTHER_JOB_UUID)
                )
            )
            await _driver(_config(), client).delete(DownloadIdentity.from_task(task))
            assert client.canceled_transfers == [_JOB_UUID]
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


def test_delete_local(tmp_path):
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
                dir=str(tmp_path),
                name="result",
                state=DownloadState.PULLING,
            )
            await OfflineDownloadJob.create(
                download=task,
                job_uuid=_JOB_UUID,
                source_fingerprint="1" * 64,
                remote_dir=f"/Kaloscope/{_JOB_UUID}",
                manifest=serialize_manifest(
                    (
                        RemoteManifestEntry(path="result", is_dir=True, size=0),
                        RemoteManifestEntry(path="result/season", is_dir=True, size=0),
                        RemoteManifestEntry(
                            path="result/season/movie.mkv", is_dir=False, size=3
                        ),
                        RemoteManifestEntry(path="kept", is_dir=True, size=0),
                    )
                ),
            )
            part = tmp_path / "result" / "season" / f".movie.mkv.{_JOB_UUID}.part"
            part.parent.mkdir(parents=True)
            part.write_bytes(b"abc")
            kept = tmp_path / "kept" / "user.txt"
            kept.parent.mkdir()
            kept.write_text("keep")

            await OpenListDriver(_config()).delete(
                DownloadIdentity.from_task(task), local=True
            )
            return part, kept
        finally:
            await Tortoise.close_connections()

    part, kept = asyncio.run(run())

    assert not part.exists()
    assert not (tmp_path / "result").exists()
    assert kept.read_text() == "keep"
    assert tmp_path.is_dir()


def test_delete_artifacts(tmp_path):
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
                dir=str(tmp_path),
                name="movie.mkv",
                state=DownloadState.PULLING,
            )
            await OfflineDownloadJob.create(
                download=task,
                job_uuid=_JOB_UUID,
                source_fingerprint="1" * 64,
                remote_dir=f"/Kaloscope/{_JOB_UUID}",
                manifest=serialize_manifest(
                    (RemoteManifestEntry(path="movie.mkv", is_dir=False, size=3),)
                ),
            )
            final = tmp_path / "movie.mkv"
            part = tmp_path / f".movie.mkv.{_JOB_UUID}.part"
            marker = tmp_path / f".movie.mkv.{_JOB_UUID}.done"
            final.write_bytes(b"abc")
            part.write_bytes(b"ab")
            marker.write_text("internal")

            await OpenListDriver(_config()).delete(
                DownloadIdentity.from_task(task), local=False
            )
            return final, part, marker
        finally:
            await Tortoise.close_connections()

    final, part, marker = asyncio.run(run())

    assert final.read_bytes() == b"abc"
    assert not part.exists()
    assert not marker.exists()


def test_delete_unknown(tmp_path):
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
                dir=str(tmp_path),
                name="result",
                state=DownloadState.SUBMIT_UNKNOWN,
            )
            source = "https://example.com/file"
            remote_dir = f"/Kaloscope/{_JOB_UUID}"
            await OfflineDownloadJob.create(
                download=task,
                job_uuid=_JOB_UUID,
                source_fingerprint="b14834559f5ee66a791f348bb05b37a925ab7875ac8229fc6c51ab670d2889a3",
                remote_dir=remote_dir,
            )
            client = DeleteClient(
                RemoteTask(
                    id="accepted",
                    name=f"download {source} to ({remote_dir})",
                    state=RemoteTaskState.RUNNING,
                    progress=10,
                    total_bytes=100,
                ),
                RemoteTask(
                    id="unrelated",
                    name=f"download {source} to (/Kaloscope/other)",
                    state=RemoteTaskState.RUNNING,
                    progress=10,
                    total_bytes=100,
                ),
                transfers=(
                    RemoteTask(
                        id="active-transfer",
                        name=f"transfer to ({_JOB_UUID})",
                        state=RemoteTaskState.RUNNING,
                        progress=25,
                        total_bytes=100,
                    ),
                ),
            )

            await _driver(_config(), client).delete(DownloadIdentity.from_task(task))
            return client.canceled, client.canceled_transfers
        finally:
            await Tortoise.close_connections()

    assert asyncio.run(run()) == (["accepted"], ["active-transfer"])


def test_transfer_actions(tmp_path):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            downloader = await Downloader.create(
                config="config", name="openlist", priority=1
            )
            failed = await DownloadTask.create(
                downloader=downloader,
                dir=str(tmp_path),
                name="failed",
                state=DownloadState.ERROR,
            )
            await OfflineDownloadJob.create(
                download=failed,
                job_uuid=_JOB_UUID,
                source_fingerprint="1" * 64,
                remote_dir=f"/Kaloscope/{_JOB_UUID}",
                last_error_kind=OfflineDownloadErrorKind.TRANSFER_FAILED,
            )
            settling = await DownloadTask.create(
                downloader=downloader,
                dir=str(tmp_path),
                name="settling",
                state=DownloadState.SETTLING,
            )
            await OfflineDownloadJob.create(
                download=settling,
                job_uuid=_OTHER_JOB_UUID,
                source_fingerprint="2" * 64,
                remote_dir=f"/Kaloscope/{_OTHER_JOB_UUID}",
            )
            client = TransferClient(
                RemoteTask(
                    id="active-transfer",
                    name=f"transfer to ({_OTHER_JOB_UUID})",
                    state=RemoteTaskState.RUNNING,
                    progress=25,
                    total_bytes=100,
                ),
                RemoteTask(
                    id="failed-transfer",
                    name=f"transfer to ({_JOB_UUID})",
                    state=RemoteTaskState.FAILED,
                    progress=50,
                    total_bytes=100,
                    error="transfer failed",
                ),
            )
            driver = _driver(_config(), client)

            state = await driver.retry(DownloadIdentity.from_task(failed))
            await driver.delete(DownloadIdentity.from_task(settling))
            return state, client.calls
        finally:
            await Tortoise.close_connections()

    state, calls = asyncio.run(run())

    assert state is DownloadState.SETTLING
    assert calls == [
        "transfer_undone",
        "transfer_done",
        ("retry_transfer", "failed-transfer"),
        "transfer_undone",
        ("cancel_transfer", "active-transfer"),
    ]


def test_unknown_submission():
    source = "magnet:?xt=urn:btih:" + "3" * 40
    request = DownloadRequest(
        directory="/downloads",
        remote_directory=f"/Kaloscope/{_JOB_UUID}",
        link=source,
        identity=DownloadIdentity(task_id=44, info_hash="3" * 40),
    )
    client = ValidationClient(
        submit_error=OpenListClientError(OpenListErrorKind.TRANSIENT)
    )

    snapshot = asyncio.run(_driver(_config(), client).add(request))

    assert snapshot.identity.remote_id is None
    assert snapshot.state == DownloadState.SUBMIT_UNKNOWN
    assert snapshot.percentage == 0
    assert snapshot.error == "OpenList submission result is unknown"
    assert client.calls == [
        ("mkdir", request.remote_directory),
        ("submit", source, request.remote_directory),
    ]


def test_failed_submission_cleanup():
    source = "magnet:?xt=urn:btih:" + "4" * 40
    request = DownloadRequest(
        directory="/downloads",
        remote_directory=f"/Kaloscope/{_JOB_UUID}",
        link=source,
        identity=DownloadIdentity(task_id=45, info_hash="4" * 40),
    )
    client = ValidationClient(submit_error=OpenListClientError(OpenListErrorKind.API))

    with pytest.raises(OpenListClientError):
        asyncio.run(_driver(_config(), client).add(request))

    assert client.calls == [
        ("mkdir", request.remote_directory),
        ("submit", source, request.remote_directory),
        ("remove", "/Kaloscope", _JOB_UUID),
    ]
