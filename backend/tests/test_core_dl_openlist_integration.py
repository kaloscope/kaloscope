import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr
from torrentool.bencode import Bencode
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.dl.driver import DownloadIdentity, DownloadRequest, DownloadState
from app.core.dl.openlist import coordinator as coordinator_module
from app.core.dl.openlist import driver as driver_module
from app.core.dl.openlist import puller as puller_module
from app.core.dl.openlist import runtime as runtime_module
from app.core.dl.openlist import state as state_module
from app.core.dl.openlist.client import OpenListClient
from app.core.dl.openlist.coordinator import OpenListCoordinator
from app.core.dl.openlist.driver import OpenListDriver
from app.core.dl.openlist.finalizer import finalize_job
from app.core.dl.openlist.manifest import (
    RemoteManifestEntry,
    manifest_fingerprint,
    serialize_manifest,
)
from app.core.dl.openlist.models import (
    OpenListAuth,
    OpenListConfig,
    RemoteCleanupPolicy,
)
from app.core.dl.openlist.puller import DataClient
from app.core.dl.openlist.runtime import OpenListPullRuntime
from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.download import (
    Downloader,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)
from app.models.general import Notification
from app.services.download import DownloadTaskService


class _ByteStream(httpx.AsyncByteStream):
    def __init__(
        self,
        content: bytes,
        *,
        split_at: int = 0,
        blocked: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ):
        self._content = content
        self._split_at = split_at
        self._blocked = blocked
        self._release = release

    async def __aiter__(self):
        if (
            self._blocked is not None
            and self._release is not None
            and 0 < self._split_at < len(self._content)
        ):
            yield self._content[: self._split_at]
            self._blocked.set()
            await self._release.wait()
            yield self._content[self._split_at :]
            return
        yield self._content


@dataclass(slots=True)
class FakeOpenList:
    remote_id: str | None = None
    remote_ids: tuple[str, ...] = ()
    remote_state: int = 2
    pending_transfers: int = 0
    block_data: bool = False
    split_at: int = 0
    empty_result: bool = False
    filename: str = "movie.mkv"
    content: bytes = b"direct OpenList result"
    token: str = "private-token"
    link_status: int = 200
    remote_dir: str | None = field(default=None, init=False)
    control_requests: list[str] = field(default_factory=list, init=False)
    submitted_sources: list[str] = field(default_factory=list, init=False)
    data_requests: int = field(default=0, init=False)
    completed_transfers: int = field(default=0, init=False)
    range_requests: list[str | None] = field(default_factory=list, init=False)
    data_blocked: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    data_release: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def finish_transfer(self):
        if self.pending_transfers < 1:
            raise ValueError("No active transfer remains")
        self.pending_transfers -= 1
        self.completed_transfers += 1

    def resume_data(self):
        self.block_data = False
        self.data_release.set()

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "cdn.example.com":
            return self._data(request)
        self.control_requests.append(request.url.path)
        if request.headers.get("Authorization") != self.token:
            return httpx.Response(401)

        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/api/fs/mkdir":
            self.remote_dir = body["path"]
            return self._response()
        if path == "/api/fs/add_offline_download":
            self.remote_dir = body["path"]
            self.submitted_sources.extend(body["urls"])
            tasks = [{"id": self.remote_id}] if self.remote_id else []
            return self._response({"tasks": tasks})
        if path.endswith("/offline_download/undone"):
            tasks = (
                [self._task(remote_id) for remote_id in self._known_remote_ids()]
                if self.remote_state in {0, 1, 3, 5, 6, 8, 9}
                else []
            )
            return self._response(tasks)
        if path.endswith("/offline_download/done"):
            tasks = (
                [self._task(remote_id) for remote_id in self._known_remote_ids()]
                if self.remote_state in {2, 4, 7}
                else []
            )
            return self._response(tasks)
        if path.endswith("/offline_download_transfer/undone"):
            return self._response(
                [self._transfer(index, 1) for index in range(self.pending_transfers)]
            )
        if path.endswith("/offline_download_transfer/done"):
            return self._response(
                [self._transfer(index, 2) for index in range(self.completed_transfers)]
            )
        if path == "/api/fs/list":
            return self._response(self._list(body["path"], body["page"]))
        if path == "/api/fs/link":
            if self.link_status != 200:
                return httpx.Response(self.link_status)
            return self._response(
                {
                    "url": "https://cdn.example.com/file?sign=private",
                    "header": {"Cookie": ["session=private"]},
                }
            )
        return httpx.Response(404)

    def _data(self, request: httpx.Request) -> httpx.Response:
        self.data_requests += 1
        range_header = request.headers.get("Range")
        self.range_requests.append(range_header)
        if request.method != "GET" or request.url.path != "/file":
            return httpx.Response(404)
        if request.headers.get("Cookie") != "session=private":
            return httpx.Response(403)
        start = (
            int(range_header.removeprefix("bytes=").removesuffix("-"))
            if range_header
            else 0
        )
        status = 206 if range_header else 200
        end = len(self.content) - 1
        total = len(self.content)
        headers = (
            {"Content-Range": f"bytes {start}-{end}/{total}"} if range_header else None
        )
        return httpx.Response(
            status,
            headers=headers,
            stream=_ByteStream(
                self.content[start:],
                split_at=self.split_at,
                blocked=self.data_blocked if self.block_data else None,
                release=self.data_release if self.block_data else None,
            ),
        )

    def _known_remote_ids(self) -> tuple[str, ...]:
        return self.remote_ids or ((self.remote_id,) if self.remote_id else ())

    def _task(self, remote_id: str | None = None) -> dict[str, object]:
        running = self.remote_state in {0, 1, 3, 5, 6, 8, 9}
        return {
            "id": remote_id or self.remote_id,
            "name": f"direct result to ({self.remote_dir})",
            "state": self.remote_state,
            "status": "running" if running else "succeeded",
            "progress": 0.0 if running else 100.0,
            "total_bytes": len(self.content),
            "error": "",
        }

    def _transfer(self, index: int, state: int) -> dict[str, object]:
        return {
            "id": f"transfer-{state}-{index}",
            "name": f"transfer to ({self.remote_dir})",
            "state": state,
            "status": "running" if state == 1 else "succeeded",
            "progress": 0.0 if state == 1 else 100.0,
            "total_bytes": len(self.content),
            "error": "",
        }

    def _list(self, path: str, page: int) -> dict[str, object]:
        content = []
        if (
            path == self.remote_dir
            and page == 1
            and not self.pending_transfers
            and not self.empty_result
        ):
            content.append(
                {
                    "name": self.filename,
                    "size": len(self.content),
                    "is_dir": False,
                    "hash_info": {"sha256": hashlib.sha256(self.content).hexdigest()},
                }
            )
        return {"content": content, "total": len(content)}

    @staticmethod
    def _response(data: object = None) -> httpx.Response:
        return httpx.Response(
            200, json={"code": 200, "message": "success", "data": data}
        )


@dataclass(slots=True)
class _DriverStack:
    driver: OpenListDriver
    control: httpx.AsyncClient

    async def close(self):
        await self.driver.close()
        await self.control.aclose()


async def _driver_stack(
    config: OpenListConfig,
    fake: FakeOpenList,
    now: list[datetime],
    monkeypatch: pytest.MonkeyPatch,
) -> _DriverStack:
    monkeypatch.setattr(coordinator_module, "now", lambda: now[0])
    monkeypatch.setattr(state_module, "random", lambda: 0.5)
    control = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
    data = DataClient(config.base_url)
    data_internal = cast(Any, data)
    await data_internal._client.aclose()
    data_internal._client = httpx.AsyncClient(
        http2=True,
        follow_redirects=False,
        timeout=60,
        trust_env=False,
        transport=httpx.MockTransport(fake.handle),
    )
    client = OpenListClient(config, control)
    runtime = OpenListPullRuntime(config, client)
    runtime_internal = cast(Any, runtime)
    runtime_internal._data_client = data
    driver = OpenListDriver(config)
    driver.client = client
    driver.coordinator = OpenListCoordinator(config, client, runtime)
    return _DriverStack(driver, control)


async def _wait_for_state(task, state: DownloadState):
    async with asyncio.timeout(5):
        while True:
            await task.refresh_from_db()
            if task.state is state:
                return
            await asyncio.sleep(0.01)


async def _pulled_job(tmp_path, extra_entries: tuple[RemoteManifestEntry, ...] = ()):
    job_uuid = "1" * 32
    old_entry = RemoteManifestEntry(path="movie.mkv", is_dir=False, size=3)
    target = puller_module.prepare_local_file(tmp_path, old_entry.path, job_uuid)
    target.part_path.write_bytes(b"old")
    await puller_module._complete_local_file(target, old_entry)
    for entry in extra_entries:
        if entry.is_dir:
            puller_module.prepare_local_directory(tmp_path, entry.path)

    downloader = await Downloader.create(config="unused", name="OpenList", priority=1)
    task = await DownloadTask.create(
        downloader=downloader,
        dir=str(tmp_path),
        name="movie.mkv",
        state=DownloadState.VERIFYING,
        percentage=100,
        completed_size=3,
        total_size=3,
    )
    entries = (old_entry, *extra_entries)
    job = await OfflineDownloadJob.create(
        download=task,
        job_uuid=job_uuid,
        source_fingerprint="1" * 64,
        remote_dir=f"/Kaloscope/{job_uuid}",
        manifest=serialize_manifest(entries),
        manifest_fingerprint=manifest_fingerprint(entries),
    )
    return task, job, target


@pytest.mark.parametrize(
    ("hybrid", "expected_hash"),
    [
        (False, "786af49023fb4e5883d1a2c85f077a70a500dcfa"),
        (True, "67265d20f7b6f7e3d09162ca2b61432f3d6d3a0b"),
    ],
    ids=["v1", "hybrid"],
)
def test_torrent_submission(hybrid, expected_hash):
    payload = b"abc"
    info = {
        "length": len(payload),
        "name": "movie.mkv",
        "piece length": 16384,
        "pieces": hashlib.sha1(payload).digest(),
    }
    if hybrid:
        info.update(
            {
                "meta version": 2,
                "file tree": {
                    "movie.mkv": {
                        "": {
                            "length": len(payload),
                            "pieces root": hashlib.sha256(payload).digest(),
                        }
                    }
                },
            }
        )
    torrent = (
        "movie.torrent",
        Bencode.encode({"info": info, "piece layers": {}}),
        "application/x-bittorrent",
    )
    fake = FakeOpenList(remote_id="remote-torrent")
    config = OpenListConfig(
        host="openlist.example.com",
        port=80,
        auth=OpenListAuth(token=SecretStr(fake.token)),
        tool="Future Tool",
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fake.handle)
        ) as http:
            driver = OpenListDriver(config)
            driver.client = OpenListClient(config, http)
            request = await DownloadTaskService._normalize_source(
                driver, DownloadRequest(directory="/downloads", torrent=torrent)
            )
            assert request is not None
            await driver.add(driver.prepare(request).request)
            return request

    request = asyncio.run(run())

    assert fake.submitted_sources == [f"magnet:?xt=urn:btih:{expected_hash}"]
    assert request.identity is not None
    assert request.identity.info_hash == expected_hash
    assert request.torrent is torrent


@pytest.mark.parametrize(
    ("remote_id", "initial_state"),
    [
        (None, DownloadState.SETTLING),
        ("remote-1", DownloadState.REMOTE),
    ],
)
def test_direct_download(
    tmp_path,
    tmp_path_factory,
    remote_id: str | None,
    initial_state: DownloadState,
    monkeypatch,
):
    temp_dir = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(temp_dir))

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList(remote_id=remote_id)
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Direct Tool",
        )
        now = [datetime(2026, 8, 6, tzinfo=UTC)]
        stack = await _driver_stack(config, fake, now, monkeypatch)
        driver = stack.driver
        try:
            downloader = await Downloader.create(
                config="unused", name="OpenList", priority=1
            )
            task = await DownloadTaskService.add_request(
                downloader.id,
                driver,
                DownloadRequest(
                    directory=str(tmp_path),
                    identity=DownloadIdentity(info_hash="1" * 40),
                    link="magnet:?xt=urn:btih:" + "1" * 40,
                ),
            )
            assert task.state is initial_state
            assert task.name == "1" * 40

            identity = DownloadIdentity.from_task(task)
            if task.state is DownloadState.REMOTE:
                await driver.sync((identity,))
                await task.refresh_from_db()
                assert task.state is DownloadState.SETTLING

            job = await OfflineDownloadJob.get(download_id=task.id)
            if job.next_poll_at is not None:
                now[0] = job.next_poll_at
            await driver.sync((identity,))
            await task.refresh_from_db()
            await job.refresh_from_db()
            assert task.state is DownloadState.SETTLING
            assert job.next_poll_at is not None

            now[0] = job.next_poll_at
            await driver.sync((identity,))
            await _wait_for_state(task, DownloadState.VERIFYING)
            assert task.state is DownloadState.VERIFYING
            assert task.percentage == 100

            (snapshot,) = await driver.sync((identity,))
            await task.refresh_from_db()
            return task, snapshot, fake
        finally:
            await stack.close()
            await Tortoise.close_connections()

    task, snapshot, fake = asyncio.run(run())

    assert task.state is DownloadState.COMPLETED
    assert task.percentage == 100
    assert task.completed_size == len(fake.content)
    assert task.total_size == len(fake.content)
    assert task.files == [fake.filename]
    assert task.name == fake.filename
    assert snapshot.state is DownloadState.COMPLETED
    assert (tmp_path / fake.filename).read_bytes() == fake.content
    assert fake.data_requests == 1


@pytest.mark.parametrize(
    ("second_name", "state", "existing"),
    [
        ("Movie.mkv", DownloadState.PULLING, False),
        ("Movie.mkv", DownloadState.PULLING, True),
        ("Movie.mkv", DownloadState.VERIFYING, True),
        ("other.mkv", DownloadState.PULLING, False),
    ],
)
def test_local_aliases(tmp_path, monkeypatch, second_name, state, existing):
    probe = tmp_path / "movie.mkv"
    probe.write_bytes(b"old")
    aliases = (tmp_path / second_name).exists()
    probe.unlink()
    contents = {"movie.mkv": b"old", second_name: b"new"}
    removed = []

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList()
        original_handle = fake.handle

        def handle(_self, request):
            path = request.url.path
            body = json.loads(request.content) if request.content else {}
            if request.url.host == "cdn.example.com":
                return httpx.Response(
                    200, stream=_ByteStream(contents[path.removeprefix("/")])
                )
            if path == "/api/fs/list":
                return fake._response(
                    {
                        "content": [
                            {"name": name, "size": 3, "is_dir": False, "hash_info": {}}
                            for name in contents
                        ],
                        "total": 2,
                    }
                )
            if path == "/api/fs/link":
                name = body["path"].rsplit("/", 1)[-1]
                return fake._response({"url": f"https://cdn.example.com/{name}"})
            if path == "/api/fs/remove":
                removed.append(body)
                return fake._response()
            return original_handle(request)

        monkeypatch.setattr(FakeOpenList, "handle", handle)
        config = OpenListConfig(
            host="openlist.example.com",
            port=80,
            auth={"token": fake.token},
            tool="Direct Tool",
            pull_concurrency=1,
            remote_cleanup=RemoteCleanupPolicy.DELETE_ON_SUCCESS,
        )
        stack = await _driver_stack(config, fake, [datetime.now(UTC)], monkeypatch)
        try:
            task, job, target = await _pulled_job(
                tmp_path,
                (RemoteManifestEntry(path=second_name, is_dir=False, size=3),),
            )
            if not existing:
                puller_module.delete_local_file(tmp_path, "movie.mkv", job.job_uuid)
            elif state is DownloadState.VERIFYING and not aliases:
                (tmp_path / second_name).write_bytes(b"new")
            task.state = state
            task.total_size = 6
            await task.save()
            identity = DownloadIdentity.from_task(task)
            await stack.driver.sync((identity,))
            if state is DownloadState.PULLING:
                runtime = cast(Any, stack.driver.coordinator).pull_runtime
                async with asyncio.timeout(5):
                    await asyncio.gather(*tuple(runtime._tasks.values()))
                await stack.driver.sync((identity,))
            await task.refresh_from_db()
            await job.refresh_from_db()
            if aliases:
                assert task.state is DownloadState.ERROR
                assert (
                    job.last_error_kind is OfflineDownloadErrorKind.LOCAL_FILE_CONFLICT
                )
                assert job.completion_due_at is None
                assert job.next_poll_at is None
                assert not removed
                assert target.final_path.read_bytes() == b"old"
            else:
                assert task.state is DownloadState.COMPLETED
                assert task.completed_size == 6
                assert len(removed) == 1
                for name, content in contents.items():
                    assert (tmp_path / name).read_bytes() == content
        finally:
            await stack.close()
            await Tortoise.close_connections()

    asyncio.run(run())


def test_cleanup_pending(tmp_path, monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList(content=b"old")
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Direct Tool",
            remote_cleanup=RemoteCleanupPolicy.DELETE_ON_SUCCESS,
        )
        now = [datetime(2026, 8, 6, tzinfo=UTC)]
        stack = await _driver_stack(config, fake, now, monkeypatch)
        try:
            task, job, _ = await _pulled_job(tmp_path)
            fake.remote_dir = job.remote_dir
            entry = RemoteManifestEntry(
                path=fake.filename,
                is_dir=False,
                size=len(fake.content),
                hashes=(("sha256", hashlib.sha256(fake.content).hexdigest()),),
            )
            job.manifest = serialize_manifest((entry,))
            job.manifest_fingerprint = manifest_fingerprint((entry,))
            await job.save(update_fields=["manifest", "manifest_fingerprint"])

            async def interrupt_cleanup(_driver, _snapshot):
                raise asyncio.CancelledError

            monkeypatch.setattr(OpenListDriver, "_cleanup_completed", interrupt_cleanup)
            with pytest.raises(asyncio.CancelledError):
                await stack.driver.sync((DownloadIdentity.from_task(task),))
            await task.refresh_from_db()
            await job.refresh_from_db()
            return task, job
        finally:
            await stack.close()
            await Tortoise.close_connections()

    task, job = asyncio.run(run())

    assert task.state is DownloadState.COMPLETED
    assert job.next_poll_at == datetime(2026, 8, 6, tzinfo=UTC)
    assert job.last_error_kind is None


@pytest.mark.parametrize("message", ["failed get object: storage unavailable", ""])
def test_submission_rejected(tmp_path, monkeypatch, message):
    class RejectedOpenList(FakeOpenList):
        def handle(self, request):
            if request.url.path == "/api/fs/add_offline_download":
                return httpx.Response(
                    200, json={"code": 500, "message": message, "data": None}
                )
            if request.url.path == "/api/fs/remove":
                return self._response()
            return super().handle(request)

    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = RejectedOpenList()
        config = OpenListConfig(
            host="openlist.example.com",
            port=80,
            auth={"token": fake.token},
            tool="115 Open",
        )
        stack = await _driver_stack(config, fake, [datetime.now(UTC)], monkeypatch)
        try:
            downloader = await Downloader.create(
                config="unused", name="OpenList", priority=1
            )
            with pytest.raises(KaloscopeException) as caught:
                await DownloadTaskService.add_request(
                    downloader.id,
                    stack.driver,
                    DownloadRequest(
                        directory=str(tmp_path),
                        identity=DownloadIdentity(info_hash="a" * 40),
                        link="magnet:?xt=urn:btih:" + "a" * 40,
                    ),
                )
            assert caught.value.message == (message or ErrorCode.HTTP_REQUEST_FAILED)
            assert await DownloadTask.all().count() == 0
            assert await OfflineDownloadJob.all().count() == 0
            assert not await DownloadTaskService.hash_collision("a" * 40)
        finally:
            await stack.close()
            await Tortoise.close_connections()

    asyncio.run(run())


def test_cleanup_rate_limit(monkeypatch):
    class LimitedOpenList(FakeOpenList):
        remove_calls = 0

        def handle(self, request):
            if request.url.path == "/api/fs/remove":
                self.remove_calls += 1
                if self.remove_calls == 1:
                    return httpx.Response(429, headers={"Retry-After": "600"})
                return self._response()
            return super().handle(request)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = LimitedOpenList()
        config = OpenListConfig(
            host="openlist.example.com",
            port=80,
            auth={"token": fake.token},
            tool="115 Open",
            remote_cleanup=RemoteCleanupPolicy.DELETE_ON_SUCCESS,
        )
        now = [datetime(2026, 8, 6, tzinfo=UTC)]
        monkeypatch.setattr(driver_module.timezone, "now", lambda: now[0])
        stack = await _driver_stack(config, fake, now, monkeypatch)
        try:
            downloader = await Downloader.create(
                config="unused", name="OpenList", priority=1
            )

            async def completed_job(number):
                task = await DownloadTask.create(
                    downloader=downloader,
                    dir="/downloads",
                    name="movie.mkv",
                    state=DownloadState.COMPLETED,
                )
                return await OfflineDownloadJob.create(
                    download=task,
                    job_uuid=str(number) * 32,
                    source_fingerprint="a" * 64,
                    remote_dir="/Kaloscope/" + str(number) * 32,
                    next_poll_at=now[0],
                )

            jobs = [await completed_job(1), await completed_job(2)]
            identities = tuple(DownloadIdentity.from_task(job.download) for job in jobs)
            await stack.driver.sync(identities)
            deadline = now[0] + timedelta(seconds=600)
            for job in jobs:
                await job.refresh_from_db()
                assert job.next_poll_at == deadline
                assert (
                    job.last_error_kind is OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT
                )
            assert fake.remove_calls == 1

            # a new completion must share the persisted limit after driver replacement
            await stack.close()
            stack = await _driver_stack(config, fake, now, monkeypatch)
            now[0] = deadline - timedelta(seconds=1)
            jobs.append(await completed_job(3))
            identities = tuple(DownloadIdentity.from_task(job.download) for job in jobs)
            await stack.driver.sync(identities)
            await jobs[-1].refresh_from_db()
            assert jobs[-1].next_poll_at == deadline
            assert fake.remove_calls == 1

            now[0] = deadline
            await stack.driver.sync(identities)
            assert fake.remove_calls == 4
            for job in jobs:
                await job.refresh_from_db()
                await job.download.refresh_from_db()
                assert job.next_poll_at is None
                assert job.last_error_kind is None
                assert job.download.state is DownloadState.COMPLETED
        finally:
            await stack.close()
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize("new_name", ["movie.mkv", "renamed.mkv"])
def test_manifest_change(tmp_path, new_name: str):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList(filename=new_name, content=b"updated")
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Direct Tool",
        )
        control = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
        try:
            old_directories = (
                RemoteManifestEntry(path="extras", is_dir=True, size=0),
                RemoteManifestEntry(path="extras/subtitles", is_dir=True, size=0),
            )
            _, job, target = await _pulled_job(tmp_path, old_directories)
            fake.remote_dir = job.remote_dir

            snapshot = await finalize_job(
                OpenListClient(config, control),
                job,
                datetime(2026, 8, 6, tzinfo=UTC),
            )
            return snapshot, target
        finally:
            await control.aclose()
            await Tortoise.close_connections()

    snapshot, target = asyncio.run(run())

    assert snapshot.state is DownloadState.SETTLING
    assert not target.final_path.exists()
    assert not target.marker_path.exists()
    assert not (tmp_path / "extras").exists()


def test_manifest_failure(tmp_path):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList(filename="renamed.mkv", content=b"updated")
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Direct Tool",
        )
        control = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
        try:
            task, job, target = await _pulled_job(tmp_path)
            fake.remote_dir = job.remote_dir
            with patch.object(
                type(target.final_path), "unlink", side_effect=PermissionError
            ):
                snapshot = await finalize_job(
                    OpenListClient(config, control),
                    job,
                    datetime(2026, 8, 6, tzinfo=UTC),
                )
            await task.refresh_from_db()
            await job.refresh_from_db()
            return snapshot, task, job
        finally:
            await control.aclose()
            await Tortoise.close_connections()

    snapshot, task, job = asyncio.run(run())

    assert snapshot.state is DownloadState.ERROR
    assert task.error_msg == "Local file could not be deleted"
    assert job.last_error_kind is OfflineDownloadErrorKind.PULL_FAILED
    assert job.next_poll_at is None


def test_empty_manifest(tmp_path):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList(empty_result=True)
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Direct Tool",
        )
        control = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
        try:
            task, job, target = await _pulled_job(tmp_path)
            fake.remote_dir = job.remote_dir

            snapshot = await finalize_job(
                OpenListClient(config, control),
                job,
                datetime(2026, 8, 6, tzinfo=UTC),
            )
            client = OpenListClient(config, control)
            runtime = OpenListPullRuntime(config, client)
            coordinator = OpenListCoordinator(config, client, runtime)
            await coordinator._sync_settling(
                (job,), datetime(2026, 8, 6, 0, 1, tzinfo=UTC)
            )
            await task.refresh_from_db()
            await job.refresh_from_db()
            total_size = task.total_size
            manifest = job.manifest
            exists_after_empty = target.final_path.exists()

            fake.empty_result = False
            fake.filename = "renamed.mkv"
            await coordinator._sync_settling(
                (job,), datetime(2026, 8, 6, 0, 2, tzinfo=UTC)
            )
            await runtime.close()
            return snapshot, target, total_size, manifest, exists_after_empty
        finally:
            await control.aclose()
            await Tortoise.close_connections()

    snapshot, target, total_size, manifest, exists_after_empty = asyncio.run(run())

    assert snapshot.state is DownloadState.SETTLING
    assert total_size == 3
    assert manifest
    assert exists_after_empty
    assert not target.final_path.exists()
    assert not target.marker_path.exists()


def test_partial_manifest(tmp_path):
    now = datetime(2026, 8, 6, tzinfo=UTC)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList(content=b"x")
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Direct Tool",
        )
        control = httpx.AsyncClient(transport=httpx.MockTransport(fake.handle))
        try:
            task, job, target = await _pulled_job(tmp_path)
            fake.remote_dir = job.remote_dir
            original_manifest = job.manifest
            original_fingerprint = job.manifest_fingerprint

            snapshot = await finalize_job(OpenListClient(config, control), job, now)
            await task.refresh_from_db()
            await job.refresh_from_db()
            return (
                snapshot,
                task,
                job,
                target,
                original_manifest,
                original_fingerprint,
            )
        finally:
            await control.aclose()
            await Tortoise.close_connections()

    snapshot, task, job, target, manifest, fingerprint = asyncio.run(run())

    assert snapshot.state is DownloadState.SETTLING
    assert task.total_size == 3
    assert job.manifest == manifest
    assert job.manifest_fingerprint == fingerprint
    assert job.manifest_changed_at is None
    assert job.next_poll_at == now + timedelta(minutes=1)
    assert target.final_path.read_bytes() == b"old"
    assert target.marker_path.exists()


def test_restart_resume(tmp_path, tmp_path_factory, monkeypatch):
    temp_dir = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(temp_dir))

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList(
            remote_id="remote-1",
            pending_transfers=2,
            block_data=True,
            split_at=7,
        )
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Local Tool",
        )
        now = [datetime(2026, 8, 6, tzinfo=UTC)]
        stack = await _driver_stack(config, fake, now, monkeypatch)
        try:
            downloader = await Downloader.create(
                config="unused", name="OpenList", priority=1
            )
            task = await DownloadTaskService.add_request(
                downloader.id,
                stack.driver,
                DownloadRequest(
                    directory=str(tmp_path),
                    identity=DownloadIdentity(),
                    link="https://source.example/movie.mkv",
                ),
            )
            identity = DownloadIdentity.from_task(task)

            await stack.close()
            stack = await _driver_stack(config, fake, now, monkeypatch)
            await stack.driver.sync((identity,))
            await task.refresh_from_db()
            assert task.state is DownloadState.SETTLING

            job = await OfflineDownloadJob.get(download_id=task.id)
            now[0] = job.next_poll_at
            await stack.driver.sync((identity,))
            fake.finish_transfer()
            await job.refresh_from_db()

            await stack.close()
            stack = await _driver_stack(config, fake, now, monkeypatch)
            now[0] = job.next_poll_at
            await stack.driver.sync((identity,))
            assert "/api/fs/list" not in fake.control_requests
            assert "/api/fs/link" not in fake.control_requests
            fake.finish_transfer()

            await job.refresh_from_db()
            now[0] = job.next_poll_at
            await stack.driver.sync((identity,))
            await job.refresh_from_db()
            assert job.next_poll_at is not None

            await stack.close()
            stack = await _driver_stack(config, fake, now, monkeypatch)
            now[0] = job.next_poll_at
            await stack.driver.sync((identity,))
            await asyncio.wait_for(fake.data_blocked.wait(), 5)
            part = tmp_path / f".{fake.filename}.{job.job_uuid}.part"

            await stack.close()
            assert part.read_bytes() == fake.content[: fake.split_at]
            fake.resume_data()
            stack = await _driver_stack(config, fake, now, monkeypatch)
            request_offset = len(fake.control_requests)
            await stack.driver.sync((identity,))
            await _wait_for_state(task, DownloadState.VERIFYING)
            assert fake.control_requests[request_offset:] == ["/api/fs/link"]

            await stack.close()
            stack = await _driver_stack(config, fake, now, monkeypatch)
            (snapshot,) = await stack.driver.sync((identity,))
            await task.refresh_from_db()
            return task, snapshot, fake
        finally:
            await stack.close()
            await Tortoise.close_connections()

    task, snapshot, fake = asyncio.run(run())

    assert task.state is DownloadState.COMPLETED
    assert snapshot.state is DownloadState.COMPLETED
    assert task.files == [fake.filename]
    assert (tmp_path / fake.filename).read_bytes() == fake.content
    assert fake.range_requests == [None, "bytes=7-"]
    assert fake.control_requests.count("/api/fs/add_offline_download") == 1


def test_poll_throttling(monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        remote_ids = ("remote-1", "remote-2")
        fake = FakeOpenList(remote_ids=remote_ids, remote_state=1)
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Local Tool",
        )
        started = datetime(2026, 8, 6, tzinfo=UTC)
        now = [started]
        stack = await _driver_stack(config, fake, now, monkeypatch)
        try:
            downloader = await Downloader.create(
                config="unused", name="OpenList", priority=1
            )
            tasks = []
            for index, remote_id in enumerate(remote_ids, start=1):
                task = await DownloadTask.create(
                    downloader=downloader,
                    dir="/downloads",
                    name=f"Task {index}",
                    unique_id=remote_id,
                    state=DownloadState.REMOTE,
                    percentage=0,
                )
                await OfflineDownloadJob.create(
                    download=task,
                    job_uuid=f"{index:032x}",
                    source_fingerprint=f"{index}" * 64,
                    remote_dir=f"/Kaloscope/{index:032x}",
                )
                tasks.append(task)
            identities = tuple(DownloadIdentity.from_task(task) for task in tasks)

            for second in range(60):
                now[0] = started + timedelta(seconds=second)
                await stack.driver.sync(identities)
            return fake.control_requests
        finally:
            await stack.close()
            await Tortoise.close_connections()

    requests = asyncio.run(run())

    assert requests.count("/api/task/offline_download/undone") == 3
    assert "/api/task/offline_download_transfer/undone" not in requests
    assert "/api/fs/list" not in requests
    assert "/api/fs/link" not in requests


def test_pull_concurrency(tmp_path, monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        release = asyncio.Event()
        ready = asyncio.Event()
        entered = 0

        async def find_completed(*_args):
            nonlocal entered
            entered += 1
            if entered == 2:
                ready.set()
            await release.wait()
            return tmp_path / "completed"

        monkeypatch.setattr(runtime_module, "find_completed_file", find_completed)
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr("private-token")),
            tool="Direct Tool",
            pull_concurrency=2,
        )
        runtime = OpenListPullRuntime(config, cast(OpenListClient, object()))
        try:
            downloader = await Downloader.create(
                config="unused", name="OpenList", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(tmp_path),
                name="result",
                state=DownloadState.PULLING,
            )
            entries = tuple(
                RemoteManifestEntry(path=f"{index}.mkv", is_dir=False, size=1)
                for index in range(5)
            )
            job = await OfflineDownloadJob.create(
                download=task,
                job_uuid="1" * 32,
                source_fingerprint="1" * 64,
                remote_dir="/Kaloscope/" + "1" * 32,
                manifest=serialize_manifest(entries),
            )

            handle = asyncio.create_task(runtime._pull_job(job))
            await asyncio.wait_for(ready.wait(), 1)
            await asyncio.sleep(0)
            active = entered
            release.set()
            await handle
            return active
        finally:
            await runtime.close()
            await Tortoise.close_connections()

    assert asyncio.run(run()) == 2


def test_pull_error(tmp_path, monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList(link_status=401)
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Direct Tool",
        )
        now = [datetime(2026, 8, 6, tzinfo=UTC)]
        stack = await _driver_stack(config, fake, now, monkeypatch)
        try:
            downloader = await Downloader.create(
                config="unused", name="OpenList", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(tmp_path),
                name="movie.mkv",
                state=DownloadState.PULLING,
            )
            job = await OfflineDownloadJob.create(
                download=task,
                job_uuid="1" * 32,
                source_fingerprint="1" * 64,
                remote_dir="/Kaloscope/" + "1" * 32,
                manifest=[
                    {"path": "movie.mkv", "is_dir": False, "size": 3, "hashes": {}}
                ],
            )

            await stack.driver.sync((DownloadIdentity.from_task(task),))
            await _wait_for_state(task, DownloadState.ERROR)
            await job.refresh_from_db()
            notification = await Notification.get()
            retry = await stack.driver.retry(DownloadIdentity.from_task(task))
            return job, retry, notification
        finally:
            await stack.close()
            await Tortoise.close_connections()

    job, retry, notification = asyncio.run(run())

    assert job.last_error_kind is OfflineDownloadErrorKind.INSTANCE_AUTH
    assert retry is DownloadState.PULLING
    assert notification.title == "DOWNLOAD_FAILED"
    assert json.loads(notification.content) == {
        "name": "movie.mkv",
        "error": "OpenList request failed with HTTP 401",
    }


@pytest.mark.parametrize(
    ("status", "refresh", "data_rate_limit"),
    [
        (429, False, False),
        (429, True, False),
        (503, False, False),
        (503, True, False),
        (429, False, True),
    ],
)
def test_pull_backoff_resume(tmp_path, monkeypatch, status, refresh, data_rate_limit):
    class FlakyOpenList(FakeOpenList):
        link_calls = 0
        expired = False

        def handle(self, request):
            if request.url.path == "/api/fs/link":
                self.link_calls += 1
                if not data_rate_limit and self.link_calls == (2 if refresh else 1):
                    return httpx.Response(status, headers={"Retry-After": "120"})
            if (
                request.url.host == "cdn.example.com"
                and data_rate_limit
                and not self.data_requests
            ):
                self.data_requests += 1
                return httpx.Response(429, headers={"Retry-After": "120"})
            if request.url.host == "cdn.example.com" and refresh and not self.expired:
                self.expired = True
                return httpx.Response(401)
            return super().handle(request)

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FlakyOpenList(content=b"old")
        config = OpenListConfig(
            host="openlist.example.com",
            port=80,
            auth={"token": fake.token},
            tool="115 Open",
        )
        now = [datetime(2026, 8, 6, tzinfo=UTC)]
        stack = await _driver_stack(config, fake, now, monkeypatch)
        try:
            task, job, _ = await _pulled_job(tmp_path)
            puller_module.delete_local_file(tmp_path, "movie.mkv", job.job_uuid)
            await DownloadTask.filter(id=task.id).update(state=DownloadState.PULLING)
            identity = DownloadIdentity.from_task(task)
            requested_at = datetime.now(UTC)
            await stack.driver.sync((identity,))
            async with asyncio.timeout(5):
                await asyncio.gather(
                    *tuple(stack.driver.coordinator.pull_runtime._tasks.values())
                )
            await task.refresh_from_db()
            await job.refresh_from_db()
            assert task.state is DownloadState.PULLING
            assert job.retry_count == 1
            assert job.next_poll_at is not None
            assert await Notification.all().count() == 0
            first_calls = (fake.link_calls, fake.data_requests)
            if status == 429:
                assert (
                    job.last_error_kind is OfflineDownloadErrorKind.INSTANCE_RATE_LIMIT
                )
                assert job.next_poll_at >= requested_at + timedelta(seconds=120)
            else:
                assert (
                    job.last_error_kind is OfflineDownloadErrorKind.INSTANCE_TRANSIENT
                )
            await stack.close()
            stack = await _driver_stack(config, fake, now, monkeypatch)
            now[0] = job.next_poll_at - timedelta(seconds=1)
            await stack.driver.sync((identity,))
            assert (fake.link_calls, fake.data_requests) == first_calls
            now[0] = job.next_poll_at
            await stack.driver.sync((identity,))
            await _wait_for_state(task, DownloadState.VERIFYING)
            assert (tmp_path / "movie.mkv").read_bytes() == b"old"
            assert task.error_msg is None
            await job.refresh_from_db()
            assert job.last_error_kind is None
        finally:
            await stack.close()
            await Tortoise.close_connections()

    asyncio.run(run())


def test_pull_crash(tmp_path, monkeypatch):
    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        fake = FakeOpenList()
        config = OpenListConfig(
            protocol="https",
            host="openlist.example.com",
            port=443,
            auth=OpenListAuth(token=SecretStr(fake.token)),
            tool="Direct Tool",
        )
        now = [datetime(2026, 8, 6, tzinfo=UTC)]
        stack = await _driver_stack(config, fake, now, monkeypatch)

        def crash(*_args):
            raise RuntimeError("unexpected pull failure")

        monkeypatch.setattr(runtime_module, "prepare_local_file", crash)
        try:
            downloader = await Downloader.create(
                config="unused", name="OpenList", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(tmp_path),
                name="movie.mkv",
                state=DownloadState.PULLING,
            )
            job = await OfflineDownloadJob.create(
                download=task,
                job_uuid="1" * 32,
                source_fingerprint="1" * 64,
                remote_dir="/Kaloscope/" + "1" * 32,
                manifest=[
                    {"path": "movie.mkv", "is_dir": False, "size": 3, "hashes": {}}
                ],
            )

            await stack.driver.sync((DownloadIdentity.from_task(task),))
            await _wait_for_state(task, DownloadState.ERROR)
            await job.refresh_from_db()
            return task, job
        finally:
            await stack.close()
            await Tortoise.close_connections()

    task, job = asyncio.run(run())

    assert task.error_msg == "OpenList local pull failed"
    assert job.last_error_kind is OfflineDownloadErrorKind.PULL_FAILED
