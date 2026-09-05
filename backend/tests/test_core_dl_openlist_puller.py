import asyncio
import errno
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx
import pytest

from app.core.dl.openlist import puller
from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.manifest import RemoteManifestEntry
from app.core.dl.openlist.models import OpenListErrorKind, RemoteLink
from app.models.download import OfflineDownloadErrorKind

BASE_URL = "https://openlist.example.com/root/api"
ROOT_URL = "https://openlist.example.com/root"
JOB_ID = "1" * 32
OTHER_JOB_ID = "2" * 32


class _ByteStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self._content = content

    async def __aiter__(self):
        yield self._content


class _BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"ab"
        raise httpx.ReadError("connection lost")


class _LinkClient:
    def __init__(self, *links: RemoteLink):
        self._links = iter(links)
        self.paths: list[str] = []

    async def link(self, path: str) -> RemoteLink:
        self.paths.append(path)
        return next(self._links)


async def _no_sleep(_: float):
    pass


@asynccontextmanager
async def _data_client(handler):
    client = puller.DataClient(BASE_URL)
    internal = cast(Any, client)
    await internal._client.aclose()
    internal._client = httpx.AsyncClient(
        http2=True,
        follow_redirects=False,
        timeout=60,
        trust_env=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        yield client
    finally:
        await client.aclose()


def _link(url: str, headers=None) -> RemoteLink:
    return RemoteLink.model_validate({"url": url, "header": headers or {}})


def _assert_unavailable(url: str):
    with pytest.raises(puller.PullError) as caught:
        puller.validate_data_url(url, openlist_base_url=BASE_URL)

    assert caught.value.kind is OfflineDownloadErrorKind.DIRECT_LINK_UNAVAILABLE
    assert url not in str(caught.value)


def _entry(*, size: int, hashes=()):
    return RemoteManifestEntry(
        path="movie.mkv",
        is_dir=False,
        size=size,
        hashes=hashes,
    )


def _run_pull(handler, target, entry, *, headers=None):
    link_client = _LinkClient(
        _link(
            "https://cdn.example.com/movie.mkv?sign=private",
            {
                name: [values] if isinstance(values, str) else values
                for name, values in (headers or {}).items()
            },
        )
    )

    async def run():
        async with _data_client(handler) as client:
            return await puller.pull_file(
                client,
                cast(OpenListClient, link_client),
                target,
                "/remote/task/movie.mkv",
                entry=entry,
            )

    with patch.object(puller.asyncio, "sleep", _no_sleep):
        return asyncio.run(run())


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/movie.mkv",
        "/relative/movie.mkv",
        f"{ROOT_URL}/p/movie.mkv?sign=private",
        f"{ROOT_URL}/d/movie.mkv?sign=private",
    ],
)
def test_data_url(url: str):
    _assert_unavailable(url)


def test_request_scope():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        headers = {"Set-Cookie": "server-cookie=private"} if len(requests) == 1 else {}
        return httpx.Response(200, headers=headers, content=b"data")

    async def run():
        async with _data_client(handler) as client:
            async with client.stream(
                "https://cdn.example.com/first",
                headers={
                    "Cookie": ["file-cookie=private"],
                    "X-File": ["one", "two"],
                    "Range": "bytes=10-",
                },
            ) as response:
                await response.aread()
            async with client.stream("https://cdn.example.com/second") as response:
                await response.aread()

    asyncio.run(run())

    first, second = requests
    assert "authorization" not in first.headers
    assert first.headers["cookie"] == "file-cookie=private"
    assert first.headers.get_list("x-file") == ["one", "two"]
    assert first.headers["range"] == "bytes=10-"
    assert "authorization" not in second.headers
    assert "cookie" not in second.headers
    assert "x-file" not in second.headers


def test_log_redaction(caplog):
    caplog.set_level(logging.INFO, logger="httpx")

    def handler(_request: httpx.Request):
        return httpx.Response(200, content=b"data")

    async def run():
        async with (
            _data_client(handler) as client,
            client.stream("https://cdn.example.com/movie.mkv?sign=private") as response,
        ):
            await response.aread()

    asyncio.run(run())

    assert "https://cdn.example.com/movie.mkv" in caplog.text
    assert "sign=private" not in caplog.text


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside",
        "folder\\movie.mkv",
    ],
)
def test_local_path(tmp_path, relative_path: str):
    with pytest.raises(puller.PullError) as caught:
        puller.prepare_local_file(tmp_path, relative_path, JOB_ID)

    assert caught.value.kind is OfflineDownloadErrorKind.LOCAL_PATH_INVALID
    assert relative_path not in str(caught.value)


def test_symlink(tmp_path):
    task_dir = tmp_path / "downloads"
    outside = tmp_path / "outside"
    task_dir.mkdir()
    outside.mkdir()
    (task_dir / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(puller.PullError) as caught:
        puller.prepare_local_file(task_dir, "linked/movie.mkv", JOB_ID)

    assert caught.value.kind is OfflineDownloadErrorKind.LOCAL_PATH_INVALID
    assert not (outside / f".movie.mkv.{JOB_ID}.part").exists()


def test_file_conflict(tmp_path):
    final_path = tmp_path / "movie.mkv"
    final_path.write_bytes(b"existing")

    with pytest.raises(puller.PullError) as caught:
        puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    assert caught.value.kind is OfflineDownloadErrorKind.LOCAL_FILE_CONFLICT
    assert final_path.read_bytes() == b"existing"
    assert not (tmp_path / f".movie.mkv.{JOB_ID}.part").exists()


def test_part_isolation(tmp_path):
    first = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    first.part_path.write_bytes(b"partial")

    second = puller.prepare_local_file(tmp_path, "movie.mkv", OTHER_JOB_ID)

    assert second.part_path != first.part_path
    assert second.offset == 0
    assert first.part_path.read_bytes() == b"partial"


def test_long_name(tmp_path):
    name = "x" * 220
    final_path = tmp_path / name
    final_path.touch()
    final_path.unlink()

    target = puller.prepare_local_file(tmp_path, name, JOB_ID)

    assert target.final_path == final_path
    assert target.part_path.is_file()
    assert len(os.fsencode(target.part_path.name)) <= os.pathconf(
        tmp_path, "PC_NAME_MAX"
    )


def test_name_limit_fallback(tmp_path, monkeypatch):
    monkeypatch.delattr(puller.os, "pathconf")

    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    assert target.part_path.is_file()


def test_range_reset(tmp_path):
    part_path = tmp_path / f".movie.mkv.{JOB_ID}.part"
    part_path.write_bytes(b"stale")
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, stream=_ByteStream(b"fresh"))

    size = _run_pull(handler, target, _entry(size=5))

    assert requests[0].headers["range"] == "bytes=5-"
    assert not part_path.exists()
    assert target.marker_path.is_file()
    assert target.final_path.read_bytes() == b"fresh"
    assert size == 5


def test_owned_file(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    def handler(_request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"abc"))

    _run_pull(handler, target, _entry(size=3))

    async def find(job_id: str):
        return await puller.find_completed_file(tmp_path, _entry(size=3), job_id)

    assert asyncio.run(find(JOB_ID)) == target.final_path
    with pytest.raises(puller.PullError) as caught:
        asyncio.run(find(OTHER_JOB_ID))
    assert caught.value.kind is OfflineDownloadErrorKind.LOCAL_FILE_CONFLICT


def test_owned_invalid(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    def handler(_request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"abc"))

    _run_pull(handler, target, _entry(size=3))

    async def find():
        return await puller.find_completed_file(
            tmp_path,
            _entry(size=3, hashes=(("sha256", "0" * 64),)),
            JOB_ID,
        )

    assert asyncio.run(find()) is None
    assert not target.final_path.exists()
    assert not target.marker_path.exists()


def test_stale_owner(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    def handler(_request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"one"))

    _run_pull(handler, target, _entry(size=3))
    target.final_path.unlink()
    target.final_path.write_bytes(b"two")

    async def find():
        return await puller.find_completed_file(tmp_path, _entry(size=3), JOB_ID)

    with pytest.raises(puller.PullError):
        asyncio.run(find())
    puller.delete_local_file(tmp_path, "movie.mkv", JOB_ID)
    assert target.final_path.read_bytes() == b"two"


def test_delete_error(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    def handler(_request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"one"))

    _run_pull(handler, target, _entry(size=3))
    with (
        patch.object(type(target.final_path), "unlink", side_effect=PermissionError),
        pytest.raises(puller.PullError) as caught,
    ):
        puller.delete_local_file(tmp_path, "movie.mkv", JOB_ID)

    assert caught.value.kind is OfflineDownloadErrorKind.PULL_FAILED
    assert str(caught.value) == "Local file could not be deleted"


@pytest.mark.parametrize("hardlinks", [True, False])
def test_install_conflict(tmp_path, monkeypatch, hardlinks):
    if not hardlinks:

        def unsupported(*args, **kwargs):
            raise OSError(errno.EOPNOTSUPP, "hard links are unsupported")

        monkeypatch.setattr(puller.os, "link", unsupported)
    first = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    second = puller.prepare_local_file(tmp_path, "movie.mkv", OTHER_JOB_ID)

    def first_handler(_request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"one"))

    def second_handler(_request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"two"))

    _run_pull(first_handler, first, _entry(size=3))
    with pytest.raises(puller.PullError) as caught:
        _run_pull(second_handler, second, _entry(size=3))

    assert caught.value.kind is OfflineDownloadErrorKind.LOCAL_FILE_CONFLICT
    assert first.final_path.read_bytes() == b"one"
    assert first.marker_path.exists()
    assert not second.marker_path.exists()


def test_link_fallback(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    def handler(_request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"abc"))

    unsupported = OSError(errno.EOPNOTSUPP, "hard links are unsupported")
    with patch.object(puller.os, "link", side_effect=unsupported):
        size = _run_pull(handler, target, _entry(size=3))

    assert size == 3
    assert target.final_path.read_bytes() == b"abc"
    assert target.marker_path.is_file()
    assert not target.part_path.exists()


def test_library_conflict(tmp_path):
    source = puller.prepare_local_file(tmp_path, "first.mkv", JOB_ID)
    destination = puller.prepare_local_file(tmp_path, "second.mkv", JOB_ID)
    for target, content in ((source, b"one"), (destination, b"two")):
        target.part_path.write_bytes(content)
        puller._install_local_file_sync(target)

    puller.transfer_local_file(
        source.final_path, destination.final_path, JOB_ID, move=True
    )
    assert source.final_path.read_bytes() == b"one"
    assert destination.final_path.read_bytes() == b"two"
    assert source.marker_path.exists()
    assert destination.marker_path.exists()


def test_unmarked_hardlink(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    target.part_path.write_bytes(b"abc")
    os.link(target.part_path, target.final_path)

    completed = asyncio.run(
        puller.find_completed_file(tmp_path, _entry(size=3), JOB_ID)
    )
    assert completed == target.final_path
    assert completed.read_bytes() == b"abc"
    assert target.marker_path.exists()
    assert not target.part_path.exists()


@pytest.mark.parametrize("move", [False, True])
def test_library_recovery(tmp_path, move):
    program = """
import errno
import os
import sys
from pathlib import Path
from app.core.dl.openlist import puller

source = Path(sys.argv[1]) / 'movie.mkv'
destination = Path(sys.argv[1]) / 'library' / 'movie.mkv'
source.write_bytes(b'abc')
rename = puller._rename_exclusive

def cross_device(old, new):
    if old == source:
        raise OSError(errno.EXDEV, 'Different filesystem')
    return rename(old, new)

def crash_after_publication(frame, event, _arg):
    if (
        event == 'line'
        and frame.f_code.co_filename == puller.__file__
        and destination.exists()
    ):
        os._exit(73)
    return crash_after_publication

puller._rename_exclusive = cross_device
sys.settrace(crash_after_publication)
puller.transfer_local_file(source, destination, '1' * 32, move=sys.argv[2] == 'True')
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(move)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        timeout=30,
    )
    assert result.returncode == 73
    source = tmp_path / "movie.mkv"
    destination = tmp_path / "library" / "movie.mkv"
    puller.transfer_local_file(source, destination, JOB_ID, move=move)
    assert destination.read_bytes() == b"abc"
    assert source.exists() is not move
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("hardlinks", [True, False])
def test_install_process_exit(tmp_path, hardlinks):
    program = """
import errno
import os
import sys
from pathlib import Path
from app.core.dl.openlist import puller

target = puller.prepare_local_file(Path(sys.argv[1]), 'movie.mkv', '1' * 32)
target.part_path.write_bytes(b'abc')
if sys.argv[2] == 'False':
    def unsupported(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, 'hard links are unsupported')
    puller.os.link = unsupported

def crash_after_publication(frame, event, _arg):
    if (
        event == 'line'
        and frame.f_code.co_filename == puller.__file__
        and target.final_path.exists()
    ):
        os._exit(73)
    return crash_after_publication

sys.settrace(crash_after_publication)
puller._install_local_file_sync(target)
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), str(hardlinks)],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        timeout=30,
    )
    assert result.returncode == 73
    completed = asyncio.run(
        puller.find_completed_file(tmp_path, _entry(size=3), JOB_ID)
    )
    assert completed == tmp_path / "movie.mkv"
    assert completed.read_bytes() == b"abc"
    assert not (tmp_path / f".movie.mkv.{JOB_ID}.part").exists()


def test_hash_mismatch(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    def handler(request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"abc"))

    with pytest.raises(puller.PullError) as caught:
        _run_pull(
            handler,
            target,
            _entry(size=3, hashes=(("sha256", "0" * 64),)),
        )

    assert caught.value.kind is OfflineDownloadErrorKind.VERIFY_FAILED
    assert target.part_path.read_bytes() == b"abc"
    assert not target.final_path.exists()


def test_size_limit(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)

    def handler(request: httpx.Request):
        return httpx.Response(200, stream=_ByteStream(b"oversized"))

    with pytest.raises(puller.PullError) as caught:
        _run_pull(handler, target, _entry(size=3))

    assert caught.value.kind is OfflineDownloadErrorKind.VERIFY_FAILED
    assert target.part_path.read_bytes() == b""


def test_link_refresh(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    expired = "https://cdn.example.com/expired"
    fresh = "https://storage.example.com/fresh"
    link_client = _LinkClient(
        _link(expired, {"Cookie": ["old=secret"]}),
        _link(fresh, {"Cookie": ["new=secret"]}),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url == httpx.URL(expired):
            return httpx.Response(401)
        return httpx.Response(200, stream=_ByteStream(b"abc"))

    async def run():
        async with _data_client(handler) as client:
            return await puller.pull_file(
                client,
                cast(OpenListClient, link_client),
                target,
                "/remote/task/movie.mkv",
                entry=_entry(size=3),
            )

    with patch.object(puller.asyncio, "sleep", _no_sleep):
        assert asyncio.run(run()) == 3
    assert link_client.paths == [
        "/remote/task/movie.mkv",
        "/remote/task/movie.mkv",
    ]
    assert requests[0].headers["cookie"] == "old=secret"
    assert requests[1].headers["cookie"] == "new=secret"
    assert target.final_path.read_bytes() == b"abc"


def test_retry(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    link_client = _LinkClient(_link("https://cdn.example.com/movie.mkv"))
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, stream=_BrokenStream())
        if len(requests) == 2:
            return httpx.Response(503)
        return httpx.Response(
            206,
            headers={"Content-Range": "bytes 2-2/3"},
            stream=_ByteStream(b"c"),
        )

    async def record_sleep(delay: float):
        delays.append(delay)

    async def run():
        async with _data_client(handler) as client:
            return await puller.pull_file(
                client,
                cast(OpenListClient, link_client),
                target,
                "/remote/task/movie.mkv",
                entry=_entry(size=3),
            )

    with patch.object(puller.asyncio, "sleep", record_sleep):
        assert asyncio.run(run()) == 3
    assert "range" not in requests[0].headers
    assert requests[1].headers["range"] == "bytes=2-"
    assert requests[2].headers["range"] == "bytes=2-"
    assert delays == [1, 2]
    assert link_client.paths == ["/remote/task/movie.mkv"]
    assert target.final_path.read_bytes() == b"abc"


def test_rate_limit(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    requests: list[httpx.Request] = []
    delays: list[float] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, stream=_ByteStream(b"abc"))

    async def record_sleep(delay: float):
        delays.append(delay)

    async def run():
        async with _data_client(handler) as client:
            return await puller.pull_file(
                client,
                cast(
                    OpenListClient,
                    _LinkClient(_link("https://cdn.example.com/movie.mkv")),
                ),
                target,
                "/remote/task/movie.mkv",
                entry=_entry(size=3),
            )

    with (
        patch.object(puller.asyncio, "sleep", record_sleep),
        pytest.raises(OpenListClientError) as caught,
    ):
        asyncio.run(run())
    assert caught.value.kind is OpenListErrorKind.RATE_LIMIT
    assert caught.value.retry_after == "5"
    assert len(requests) == 1
    assert delays == []


def test_redirect_headers(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    link_client = _LinkClient(
        _link(
            "https://cdn.example.com/start",
            {
                "Authorization": ["Bearer file-secret"],
                "Cookie": ["file=secret"],
                "Referer": ["https://source.example.com"],
            },
        )
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/next"})
        if request.url.path == "/next":
            return httpx.Response(
                307, headers={"Location": "https://storage.example.com/file"}
            )
        return httpx.Response(200, stream=_ByteStream(b"abc"))

    assert _run_with_link(handler, target, link_client) == 3
    assert requests[1].headers["authorization"] == "Bearer file-secret"
    assert requests[1].headers["cookie"] == "file=secret"
    assert "authorization" not in requests[2].headers
    assert "cookie" not in requests[2].headers
    assert requests[2].headers["referer"] == "https://source.example.com"


def test_redirect_proxy(tmp_path):
    target = puller.prepare_local_file(tmp_path, "movie.mkv", JOB_ID)
    link_client = _LinkClient(_link("https://cdn.example.com/start"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": f"{ROOT_URL}/d/movie.mkv"})

    with pytest.raises(puller.PullError) as caught:
        _run_with_link(handler, target, link_client)

    assert caught.value.kind is OfflineDownloadErrorKind.DIRECT_LINK_UNAVAILABLE
    assert len(requests) == 1
    assert target.part_path.read_bytes() == b""


def _run_with_link(handler, target, link_client):
    async def run():
        async with _data_client(handler) as client:
            return await puller.pull_file(
                client,
                cast(OpenListClient, link_client),
                target,
                "/remote/task/movie.mkv",
                entry=_entry(size=3),
            )

    with patch.object(puller.asyncio, "sleep", _no_sleep):
        return asyncio.run(run())
