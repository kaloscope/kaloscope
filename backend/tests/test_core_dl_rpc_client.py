import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from jinja2 import TemplateError
from sanic import Sanic

from app.core.dl.rpc import RpcClient, RpcConfig
from app.core.dl.rpc.models import API
from app.core.exceptions import KaloscopeException


@pytest.fixture
def app(monkeypatch):
    app = SimpleNamespace(
        ctx=SimpleNamespace(), shared_ctx=SimpleNamespace(csrf_tokens={})
    )
    monkeypatch.setattr(Sanic, "get_app", lambda: app)
    return app


def _config(methods, **kwargs):
    return RpcConfig(name="Test", host="localhost", port=80, methods=methods, **kwargs)


def _call(cfg, handler, method="list", variables=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await RpcClient(cfg, http).call(method, variables)

    return asyncio.run(run())


def _session_config():
    return _config(
        {
            "list": {
                "steps": [
                    {"post": "/create", "save_as": "created"},
                    {"patch": "/tasks", "json": {"id": "{{steps.created.id}}"}},
                ]
            }
        },
        headers={"Authorization": "{{session.token}}"},
        session={"request": {"get": "/token"}, "ttl": 600},
    )


def _xunlei_config():
    path = Path(__file__).parents[1] / "static/downloaders/Xunlei.yaml"
    return RpcConfig.model_validate(yaml.safe_load(path.read_text()))


def _xunlei_auth(request):
    if request.url.path == "/":
        return httpx.Response(
            200, text='function uiauth(value) { return "private-token" }'
        )
    if request.url.path == "/device/info/watch":
        assert request.headers["pan-auth"] == "private-token"
        return httpx.Response(200, json={"is_login": True, "target": "device#test"})
    assert request.headers["pan-auth"] == "private-token"


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_request(method):
    def handler(request):
        assert request.method == method.upper()
        assert request.url.params.get_list("id") == ["a", "b"]
        assert request.url.params["keep"] == "yes"
        assert json.loads(request.content) == {
            "ids": ["a", "b"],
            "paused": False,
            "zero": 0,
        }
        return httpx.Response(200, json=[])

    cfg = _config(
        {
            "list": {
                method: "/tasks?keep=yes",
                "params": {"id": "{{ids}}"},
                "json": {
                    "ids": "{{ids|default([])}}",
                    "paused": "{{pause}}",
                    "zero": "{{0}}",
                },
            }
        }
    )
    variables = {"ids": ["a", "b"], "pause": False}
    assert not cfg.extended
    assert _call(cfg, handler, variables=variables) == []
    assert variables == {"ids": ["a", "b"], "pause": False}


@pytest.mark.parametrize("payload", [[], False, 0, None])
def test_response(payload):
    assert (
        _call(
            _config({"list": {"get": "/"}}),
            lambda _: httpx.Response(200, content=json.dumps(payload)),
        )
        == payload
    )


def test_form():
    torrent = ("test.torrent", b"content", "application/x-bittorrent")
    cfg = _config({})
    _, data, files, _ = RpcClient(cfg)._request_body(
        API(form={"file": "{{torrent}}", "paused": "{{pause}}"}),
        {"torrent": torrent, "pause": True},
    )
    assert files == {"file": torrent}
    assert data == {"paused": True}


def test_csrf(app):
    seen = []

    def handler(request):
        seen.append(request.headers.get("X-Session"))
        if len(seen) == 1:
            return httpx.Response(409, headers={"X-Session": "new"})
        return httpx.Response(
            200, json={"result": "success", "arguments": {"version": "4"}}
        )

    cfg = _config(
        {
            "list": {
                "json": {"method": "session-get"},
                "response": {"mappings": {"version": "$.arguments.version"}},
            }
        },
        csrf={"header": "X-Session"},
        response={"expected": {"success": "$.result"}},
    )
    assert _call(cfg, handler) == {"version": "4"}
    assert seen == [None, "new"]


def test_login(app):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/login":
            return httpx.Response(
                200, text="Ok.", headers={"set-cookie": "sid=ok; Path=/"}
            )
        if request.headers.get("cookie") != "sid=ok":
            return httpx.Response(403)
        return httpx.Response(200, json=[])

    cfg = _config(
        {"list": {"get": "/tasks"}, "login": {"post": "/login"}},
        auth={"username": "user", "password": "password"},
    )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            app.ctx.httpx = http
            return await RpcClient(cfg).call("list")

    assert asyncio.run(run()) == []
    assert seen == ["/tasks", "/login", "/tasks"]


def test_session_refresh(app):
    seen = []
    tokens = 0

    def handler(request):
        nonlocal tokens
        seen.append((request.method, request.url.path))
        if request.url.path == "/token":
            tokens += 1
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"token": str(tokens)})
        if request.url.path == "/create":
            return httpx.Response(200, json={"id": "once"})
        assert json.loads(request.content) == {"id": "once"}
        if request.headers["Authorization"] == "1":
            return httpx.Response(401, json={"error": "secret-token"})
        return httpx.Response(200, json={"ok": True})

    assert _call(_session_config(), handler) == {"ok": True}
    assert seen == [
        ("GET", "/token"),
        ("POST", "/create"),
        ("PATCH", "/tasks"),
        ("GET", "/token"),
        ("PATCH", "/tasks"),
    ]


def test_session_cache(app):
    tokens = 0

    async def handler(request):
        nonlocal tokens
        if request.url.path == "/token":
            tokens += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={"token": str(tokens)})
        return httpx.Response(200, json={"id": "task"})

    cfg = _session_config()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await asyncio.gather(*(RpcClient(cfg, http).call("list") for _ in range(2)))
            assert tokens == 1
            state = next(iter(app.ctx.rpc_sessions.values()))
            state.expires = 0
            await RpcClient(cfg, http).call("list")
            assert tokens == 2
            other = cfg.model_copy(update={"name": "Other account"})
            await RpcClient(other, http).call("list")
            assert tokens == 3

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["401", "503", "timeout"])
def test_retry(app, failure):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "secret"})
        if request.url.path == "/create":
            return httpx.Response(200, json={"id": "once"})
        if failure == "timeout":
            raise httpx.ReadTimeout("secret")
        return httpx.Response(int(failure), json={"error": {"message": "secret"}})

    with pytest.raises(KaloscopeException) as caught:
        _call(_session_config(), handler)
    assert "secret" not in str(caught.value)
    assert calls.count("/create") == 1
    assert calls.count("/tasks") == (2 if failure == "401" else 1)


@pytest.mark.parametrize("max_pages", [1, 10])
def test_pagination_limit(max_pages):
    cfg = _config(
        {
            "list": {
                "get": "/",
                "pagination": {"next": "$.next", "max_pages": max_pages},
                "response": {"each": "$.items[*]"},
            }
        }
    )
    with pytest.raises(KaloscopeException):
        _call(cfg, lambda _: httpx.Response(200, json={"items": [1], "next": "same"}))


def test_require():
    def handler(_):
        pytest.fail("Request must not be sent")

    for api in (
        {"patch": "/", "json": {"id": "{{missing}}"}},
        {"delete": "/", "require": "{{not local}}"},
    ):
        with pytest.raises((KaloscopeException, TemplateError)):
            _call(_config({"list": api}), handler, variables={"local": True})


def test_unix_socket(app):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def run(path):
        cfg = _config({"list": {"get": "/"}}, unix_socket=path)
        for _ in range(2):
            async with await asyncio.start_unix_server(handle, path):
                assert await RpcClient(cfg).call("list") == []
            Path(path).unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="rpc-") as directory:
        asyncio.run(run(str(Path(directory) / "rpc.sock")))


@pytest.mark.parametrize("variables", [{}, {"ids": []}, {"ids": ["one", "two"]}])
def test_xunlei_tasks(app, variables):
    def handler(request):
        if response := _xunlei_auth(request):
            return response
        assert request.url.params["space"] == "device#test"
        filters = {"type": {"in": "user#download-url,user#download"}}
        if variables.get("ids"):
            filters["id"] = {"in": "one,two"}
        assert json.loads(request.url.params["filters"]) == filters
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "id": "one",
                        "phase": "PHASE_TYPE_PAUSED",
                        "progress": 5,
                        "file_size": "100",
                        "message": "non-error",
                        "params": {"speed": "999", "checked_size": "5"},
                    },
                    {
                        "id": "two",
                        "phase": "PHASE_TYPE_COMPLETE",
                        "progress": 99,
                        "file_size": "200",
                        "message": "完成",
                        "params": {"speed": "999"},
                    },
                ]
            },
        )

    first, second = _call(_xunlei_config(), handler, variables=variables)
    assert (first["state"], first["dl_speed"], first["error_msg"]) == ("paused", 0, "")
    assert first["files"] == []
    assert second["files"] is None
    assert (second["state"], second["percentage"], second["completed_size"]) == (
        "completed",
        100,
        200,
    )


@pytest.mark.parametrize(
    ("phase", "params", "message"),
    [
        ("PHASE_TYPE_COMPLETE", {"error": "partial download"}, "partial download"),
        (
            "PHASE_TYPE_COMPLETE",
            {"error": '{"error_description":"disk full"}'},
            "disk full",
        ),
        (
            "PHASE_TYPE_COMPLETE",
            {"is_deleted": "true"},
            "Local files have been deleted",
        ),
        ("PHASE_TYPE_ERROR", {}, "Xunlei task failed"),
    ],
)
def test_xunlei_errors(app, phase, params, message):
    def handler(request):
        if response := _xunlei_auth(request):
            return response
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "id": "failed",
                        "phase": phase,
                        "progress": 37,
                        "file_size": "100",
                        "params": params | {"checked_size": "37"},
                    }
                ]
            },
        )

    cfg = _xunlei_config()
    task = _call(cfg, handler)[0]
    assert task["state"] == "error"
    assert task["error_msg"] == message
    assert task["percentage"] == task["completed_size"] == 37
    assert task["files"] == []
    assert _call(cfg, handler, "details", {"id": "failed"}) == task


@pytest.mark.parametrize("local", [False, True])
def test_xunlei_delete(app, local):
    def handler(request):
        if response := _xunlei_auth(request):
            return response
        if local:
            assert (request.method, request.url.path) == ("PATCH", "/drive/v1/task")
            assert json.loads(request.content) == {
                "id": "one",
                "space": "device#test",
                "type": "user#download-url",
                "set_params": {"spec": '{"phase":"delete"}'},
            }
        else:
            assert (request.method, request.url.path) == ("DELETE", "/drive/v1/tasks")
            assert dict(request.url.params) == {
                "space": "device#test",
                "task_ids": "one",
            }
        return httpx.Response(200, json={"HttpStatus": 200} if local else {})

    _call(_xunlei_config(), handler, "delete", {"id": "one", "local": local})


@pytest.mark.parametrize(
    ("directory", "root_path", "parent_id", "created_directories", "torrent"),
    [
        (
            "/volume2/kaloscope/downloads/new/nested/",
            "/volume2/",
            "nested-id",
            ["new", "nested"],
            False,
        ),
        ("/volume2/kaloscope/downloads", "/volume2/", "downloads", [], False),
        ("/volume1", "/volume1/", "volume", [], False),
        ("/volume1", "/volume1/", "volume", [], True),
        (
            "/var/packages/pan-xunlei-com/shares/迅雷/下载/kaloscope/downloads/new",
            "/var/packages/pan-xunlei-com/shares/迅雷/下载/",
            "new-id",
            ["new"],
            False,
        ),
        (
            "/tmp/SynologyAuthService/kaloscope/downloads/new",
            "/tmp/SynologyAuthService",
            "new-id",
            ["new"],
            False,
        ),
    ],
)
def test_xunlei_add(app, directory, root_path, parent_id, created_directories, torrent):
    submitted = []
    created = []
    listed_folders = {""}
    torrent_file = (
        "movie.torrent",
        b"torrent metadata\x00\xff",
        "application/x-bittorrent",
    )
    url = "magnet:?xt=uploaded&dn=movie" if torrent else "magnet:?xt=test"

    def handler(request):
        if response := _xunlei_auth(request):
            return response
        path = request.url.path
        if path == "/device/btinfo":
            assert torrent
            assert request.method == "POST"
            assert request.headers["content-type"].startswith("multipart/form-data;")
            assert b'name="file"; filename="movie.torrent"' in request.content
            assert torrent_file[1] in request.content
            assert b'name="pan-auth"\r\n\r\nprivate-token' in request.content
            return httpx.Response(200, json={"error": "ok", "url": url})
        if path == "/drive/v1/files":
            if request.method == "POST":
                body = json.loads(request.content)
                assert body["parent_id"] == (
                    "downloads" if body["name"] == "new" else "new-id"
                )
                assert body["name"] in ("new", "nested")
                created.append(body["name"])
                return httpx.Response(200, json={"file": {"id": f"{body['name']}-id"}})
            parent = request.url.params["parent_id"]
            # a new folder ID is unavailable until its parent is listed
            if parent not in listed_folders:
                return httpx.Response(404, json={"error": "file_not_found"})
            children = {
                "": [
                    {"id": "parent-root", "params": {"RealPath": "/"}},
                    {"id": "other-volume", "params": {"RealPath": "/volume3/"}},
                    {"id": "missing-path", "params": {}},
                    {"id": "volume", "params": {"RealPath": root_path}},
                ],
                "volume": [{"id": "kalo", "name": "kaloscope"}],
                "kalo": [{"id": "downloads", "name": "downloads"}],
                "downloads": (
                    [{"id": "new-id", "name": "new"}] if "new" in created else []
                ),
                "new-id": (
                    [{"id": "nested-id", "name": "nested"}]
                    if "nested" in created
                    else []
                ),
            }
            if parent == "":
                if "page_token" not in request.url.params:
                    return httpx.Response(
                        200,
                        json={
                            "files": children[parent][:-1],
                            "next_page_token": "next",
                        },
                    )
                assert request.url.params["page_token"] == "next"
                children[parent] = children[parent][-1:]
            listed_folders.update(child["id"] for child in children[parent])
            return httpx.Response(200, json={"files": children[parent]})
        if path == "/drive/v1/resource/list":
            assert json.loads(request.content) == {"urls": url}
            return httpx.Response(
                200,
                json={
                    "list": {
                        "resources": [
                            {"name": "movie", "file_size": 100, "file_count": 2}
                        ]
                    }
                },
            )
        assert path == "/drive/v1/task"
        body = json.loads(request.content)
        assert body["params"]["parent_folder_id"] == parent_id
        assert body["params"]["parent_folder_path"] == f"{directory.rstrip('/')}/"
        assert json.loads(body["params"]["spec"]) == {"phase": "pause"}
        assert body["file_size"] == "100"
        assert body["params"]["url"] == url
        assert "file_id" not in body["params"]
        assert "sub_file_index" not in body["params"]
        submitted.append(body)
        return httpx.Response(200, json={"task": {"id": "created"}})

    result = _call(
        _xunlei_config(),
        handler,
        "add_torrent" if torrent else "add_link",
        {
            "dir": directory,
            "link": "magnet:?xt=test",
            "torrent": torrent_file if torrent else None,
            "pause": True,
        },
    )
    assert result == {"unique_id": "created"}
    assert len(submitted) == 1
    assert created == created_directories


@pytest.mark.parametrize(
    "directory",
    ["/volume10/downloads", "relative", "/volume1/../secret"],
)
def test_xunlei_invalid_path(app, directory):
    def handler(request):
        if response := _xunlei_auth(request):
            return response
        assert (request.method, request.url.path) == ("GET", "/drive/v1/files")
        return httpx.Response(
            200,
            json={"files": [{"id": "volume", "params": {"RealPath": "/volume1/"}}]},
        )

    with pytest.raises(KaloscopeException):
        _call(
            _xunlei_config(),
            handler,
            "add_link",
            {"dir": directory, "link": "magnet:?xt=test", "pause": True},
        )


def test_xunlei_details(app, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    folder = alias / "torrent"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    (folder / "one.mkv").write_text("one")
    (nested / "two.mkv").write_text("two")
    (folder / "partial.xltd").write_text("incomplete")
    (folder / "link").symlink_to(tmp_path / "outside")
    (tmp_path / "outside").write_text("secret")
    cfg = _xunlei_config()

    def handler(request):
        if response := _xunlei_auth(request):
            return response
        task_id = json.loads(request.url.params["filters"])["id"]["in"]
        if task_id == "missing":
            return httpx.Response(200, json={"tasks": []})
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "id": "done",
                        "phase": "PHASE_TYPE_COMPLETE",
                        "file_size": "3",
                        "params": {
                            "real_path": str(
                                folder / "link" if task_id == "link" else folder
                            ),
                        },
                    }
                ]
            },
        )

    assert _call(cfg, handler, "details", {"id": "done"})["files"] == [
        str(nested / "two.mkv"),
        str(folder / "one.mkv"),
    ]
    assert _call(cfg, handler, "details", {"id": "missing"}) == {}
    with pytest.raises(KaloscopeException):
        _call(cfg, handler, "details", {"id": "link"})


@pytest.mark.parametrize(
    "availability",
    [
        "missing_file",
        "missing_directory",
        "empty_directory",
        "temporary_files",
        "missing_path",
    ],
)
def test_xunlei_missing_files_recover(app, tmp_path, monkeypatch, availability):
    directory = availability not in {"missing_file", "missing_path"}
    path = tmp_path / ("torrent" if directory else "movie.mkv")
    cfg = _xunlei_config()
    params = {"real_path": str(path)}
    if availability in {"empty_directory", "temporary_files"}:
        path.mkdir()
    if availability == "temporary_files":
        (path / "movie.mkv.xltd").write_bytes(b"partial")
        (path / "movie.mkv.xltd.cfg").write_bytes(b"config")
    if availability == "missing_path":
        params.clear()
        monkeypatch.chdir(tmp_path)
        (tmp_path / "unrelated.mkv").write_bytes(b"unrelated")

    def handler(request):
        if response := _xunlei_auth(request):
            return response
        assert request.url.path == "/drive/v1/tasks"
        return httpx.Response(
            200,
            json={
                "tasks": [
                    {
                        "id": "done",
                        "phase": "PHASE_TYPE_COMPLETE",
                        "file_size": "4",
                        "params": params,
                    }
                ]
            },
        )

    with pytest.raises(KaloscopeException):
        _call(cfg, handler, "details", {"id": "done"})

    if directory:
        path.mkdir(exist_ok=True)
    file = path / "movie.mkv" if directory else path
    file.write_bytes(b"done")
    params["real_path"] = str(path)
    assert _call(cfg, handler, "details", {"id": "done"})["files"] == [str(file)]
