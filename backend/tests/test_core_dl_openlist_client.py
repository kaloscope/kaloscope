import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from app.core.dl.openlist.client import OpenListClient, OpenListClientError
from app.core.dl.openlist.models import (
    OpenListAuth,
    OpenListConfig,
    OpenListErrorKind,
    RemoteTaskState,
)

CONFIG = OpenListConfig(
    protocol="https",
    host="openlist.example.com",
    port=443,
    path="/root/api/",
    auth=OpenListAuth(token=SecretStr("private-token")),
    tool="115 Open",
)


def _response(*, status=200, code=200, message="failed", data=None, raw=False):
    if raw:
        return httpx.Response(status, text="not-json")
    return httpx.Response(status, json={"code": code, "message": message, "data": data})


def _call(handler, *, secrets=()):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as http:
            return await OpenListClient(CONFIG, http)._request(
                "GET", "/test", secrets=secrets
            )

    return asyncio.run(run())


def _call_method(handler, method, *args):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await getattr(OpenListClient(CONFIG, http), method)(*args)

    return asyncio.run(run())


def _task(task_id: str):
    return {
        "id": task_id,
        "name": "offline download",
        "creator": "admin",
        "creator_role": 2,
        "state": 1,
        "status": "pending",
        "progress": 0.0,
        "start_time": None,
        "end_time": None,
        "total_bytes": 0,
        "error": "",
    }


def test_redirect():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/api"})

    with pytest.raises(OpenListClientError) as caught:
        _call(handler)

    assert caught.value.kind is OpenListErrorKind.INVALID_RESPONSE
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("handler", "kind"),
    [
        (lambda _: _response(status=401), OpenListErrorKind.AUTH),
        (lambda _: _response(status=503), OpenListErrorKind.TRANSIENT),
        (lambda _: _response(code=500), OpenListErrorKind.TRANSIENT),
        (lambda _: _response(raw=True), OpenListErrorKind.INVALID_RESPONSE),
    ],
)
def test_errors(handler, kind: OpenListErrorKind):
    with pytest.raises(OpenListClientError) as caught:
        _call(handler)

    assert caught.value.kind is kind


@pytest.mark.parametrize(("status", "code"), [(429, 200), (200, 429)])
def test_rate_limit(status: int, code: int):
    response = httpx.Response(
        status,
        headers={"Retry-After": "120"},
        json={"code": code, "message": "limited", "data": None},
    )

    with pytest.raises(OpenListClientError) as caught:
        _call(lambda _: response)

    assert caught.value.kind is OpenListErrorKind.RATE_LIMIT
    assert caught.value.retry_after == "120"


def test_error_redaction():
    source = "magnet:?xt=urn:btih:private-source"
    message = (
        "failed Authorization: Bearer private-token "
        "Cookie: sid=cookie-secret https://cdn.example/file?sign=query-secret "
        f"source={source}"
    )

    with pytest.raises(OpenListClientError) as caught:
        _call(lambda _: _response(code=400, message=message), secrets=(source,))

    error = str(caught.value)
    assert caught.value.kind is OpenListErrorKind.API
    assert caught.value.response_message == error
    assert "failed" in error
    for secret in ("private-token", "cookie-secret", "query-secret", source):
        assert secret not in error


def test_tools():
    def handler(request: httpx.Request):
        assert request.method == "GET"
        assert request.url.path == "/root/api/public/offline_download_tools"
        assert request.url.params["path"] == "/Kaloscope"
        return _response(
            data=[
                "115 Open",
                "SimpleHttp",
                "aria2",
                "qBittorrent",
                "Transmission",
                "Future Tool",
            ]
        )

    assert _call_method(handler, "tools", "/Kaloscope") == ("115 Open", "Future Tool")


def test_empty_list():
    def handler(request: httpx.Request):
        assert request.method == "POST"
        assert request.url.path == "/root/api/fs/list"
        return _response(data={"content": None, "total": 0})

    page = _call_method(handler, "list", "/Kaloscope/task", 1, 100)

    assert page.content == []


def test_submit():
    source = "magnet:?xt=urn:btih:private-source"

    def handler(request: httpx.Request):
        assert request.method == "POST"
        assert str(request.url) == (
            "https://openlist.example.com/root/api/fs/add_offline_download"
        )
        assert request.headers["Authorization"] == "private-token"
        assert json.loads(request.content) == {
            "urls": [source],
            "path": "/Kaloscope/task-uuid",
            "tool": "115 Open",
            "delete_policy": "delete_on_upload_succeed",
        }
        return _response(data={"tasks": [_task("remote-1")]})

    assert _call_method(handler, "submit", source, "/Kaloscope/task-uuid") == (
        "remote-1",
    )


@pytest.mark.parametrize(
    ("raw_state", "state"),
    [
        (0, RemoteTaskState.PENDING),
        (1, RemoteTaskState.RUNNING),
        (2, RemoteTaskState.SUCCEEDED),
        (7, RemoteTaskState.FAILED),
        (8, RemoteTaskState.RETRYING),
    ],
)
def test_tasks(raw_state: int, state: RemoteTaskState):
    payload = _task("remote-1")
    payload.update(
        state=raw_state,
        status="downloading",
        progress=37.5,
        total_bytes=1024,
        error="provider failed https://cdn.example/file?sign=private",
    )

    def handler(request: httpx.Request):
        assert request.method == "GET"
        assert request.url.path == "/root/api/task/offline_download/undone"
        return _response(data=[payload])

    (task,) = _call_method(handler, "undone")

    assert task.id == "remote-1"
    assert task.name == "offline download"
    assert task.state is state
    assert task.progress == 37.5
    assert task.total_bytes == 1024
    assert task.error == "provider failed https://cdn.example/file?<redacted>"


def test_transfer_actions():
    requests = []

    def handler(request: httpx.Request):
        requests.append((request.method, request.url.path, request.url.params["tid"]))
        return _response(data=None)

    _call_method(handler, "cancel_transfer", "transfer-1")
    _call_method(handler, "retry_transfer", "transfer-2")

    assert requests == [
        ("POST", "/root/api/task/offline_download_transfer/cancel", "transfer-1"),
        ("POST", "/root/api/task/offline_download_transfer/retry", "transfer-2"),
    ]


def test_link():
    def handler(request: httpx.Request):
        assert request.method == "POST"
        assert request.url.path == "/root/api/fs/link"
        assert json.loads(request.content) == {"path": "/Kaloscope/task/file.mkv"}
        return _response(
            data={
                "url": "https://cdn.example/file?sign=private",
                "header": {
                    "Cookie": ["sid=cookie-secret"],
                    "User-Agent": ["OpenList"],
                },
                "concurrency": 2,
                "part_size": 1048576,
                "content_length": 1024,
            }
        )

    link = _call_method(handler, "link", "/Kaloscope/task/file.mkv")

    assert link.url == "https://cdn.example/file?sign=private"
    assert link.headers == {
        "Cookie": ["sid=cookie-secret"],
        "User-Agent": ["OpenList"],
    }
    assert "private" not in repr(link)
    assert "cookie-secret" not in repr(link)
