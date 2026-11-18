"""Unit tests for the shared workflow HTTP cache."""

import asyncio
import sqlite3
import time
from contextlib import closing
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from hishel import AsyncSqliteStorage

from app import main
from app.core.flow.nodes.general.http import HTTPNode
from app.core.http_cache import workflow_cache_extensions
from app.core.network import NetworkTransport


@pytest.mark.parametrize(
    ("controls", "expected"),
    [
        ({"Ttl": "0"}, {"workflow_max_age": 0, "caching": {"ttl": 0}}),
        ({"Ttl": "nan", "Unknown": "x"}, {}),
        ({"Ttl": "invalid"}, {}),
        (
            {"Ttl": "60", "Refresh-Ttl-On-Access": "true", "Spec-Ignore": "true"},
            {"workflow_max_age": 60, "caching": {"tti": 60, "rfc": False}},
        ),
    ],
)
def test_controls(controls, expected):
    headers = httpx.Headers({"Accept": "text/plain"})
    headers.update({f"X-Hishel-{key}": value for key, value in controls.items()})

    assert workflow_cache_extensions(headers) == expected
    assert headers == {"accept": "text/plain"}


def test_upstream_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(main.app_config, "get_workspace", lambda _: str(tmp_path))
    monkeypatch.setattr(
        main, "AsyncSqliteStorage", lambda: AsyncSqliteStorage(database_path="next.db")
    )

    for name in ("worker-0", "worker-1"):
        app = SimpleNamespace(m=SimpleNamespace(name=name))
        assert (
            main._http_cache_storage(app).database_path == tmp_path / name / "next.db"
        )


def test_worker_cache(tmp_path, monkeypatch):
    with closing(sqlite3.connect(tmp_path / "main.db")) as legacy:
        legacy.execute(
            "CREATE TABLE entries (id, cache_key, data, created_at, deleted_at)"
        )
    old_cache = (tmp_path / "main.db").read_bytes()
    app = SimpleNamespace(
        debug=False,
        ctx=SimpleNamespace(),
        shared_ctx=SimpleNamespace(cookies={}, cookies_lock=RLock()),
    )
    monkeypatch.setattr(main.app_config, "get_workspace", lambda _: str(tmp_path))
    monkeypatch.setattr(main.SQLiteCookieJar, "load", AsyncMock())
    monkeypatch.setattr(main.SQLiteCookieJar, "save", AsyncMock())
    now = 1_700_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    cookies = ["a=one; Expires=Wed, 09 Jun 2038 10:18:14 GMT", "b=two; Path=/"]
    requests = []

    async def origin(_, request):
        requests.append(request)
        return httpx.Response(
            200,
            headers=[
                ("Cache-Control", "no-store"),
                ("Vary", "Accept-Language"),
                *(("Set-Cookie", cookie) for cookie in cookies),
            ],
            text=str(len(requests)),
        )

    monkeypatch.setattr(NetworkTransport, "handle_async_request", origin)

    async def run():
        nonlocal now
        await main.start_http_client(app)

        async def fetch(cache_headers=None, language="en", body=None):
            headers = {"Accept-Language": language, **(cache_headers or {})}
            options = HTTPNode.prepare_request(
                {
                    "url": "https://example.test/page",
                    "timeout": 17,
                    "headers": [{"key": k, "value": v} for k, v in headers.items()],
                    "body": body,
                },
                {},
            )
            return await app.ctx.httpx.request(**options)

        try:
            for _ in range(2):
                uncached = await fetch()
                assert not uncached.extensions["caching"]["stored"]
                assert uncached.headers["cache-control"] == "no-store"
            ttl = {"X-Hishel-Ttl": "60"}
            first = await fetch(ttl)
            hit = await fetch(ttl)
            assert first.text == hit.text == "3"
            assert hit.extensions["caching"]["from_cache"]
            for response in (first, hit):
                assert response.headers.get_list("set-cookie") == cookies
                assert response.headers["cache-control"] == "max-age=60"
            assert dict(app.ctx.httpx.cookies) == {"a": "one", "b": "two"}
            assert (await fetch(ttl, language="fr")).text == "4"
            now += 61
            assert (await fetch(ttl)).text == "5"

            keyed = {**ttl, "X-Hishel-Body-Key": "true"}
            assert (await fetch(keyed, body="alpha")).text == "6"
            assert (await fetch(keyed, body="beta")).text == "7"
            assert (await fetch(keyed, body="alpha")).text == "6"

            url = "https://example.test/downloader"
            direct = await app.ctx.httpx.get(url, headers=ttl, timeout=17)
            cached = await app.ctx.httpx.get(url, timeout=17)
            assert direct.text == cached.text == "8"
            assert cached.extensions["caching"]["from_cache"]
        finally:
            await main.close_http_client(app)

    asyncio.run(run())
    for request in requests:
        assert not any(key.startswith("x-hishel-") for key in request.headers)
        assert request.extensions["timeout"] == httpx.Timeout(17).as_dict()
    assert (tmp_path / "main.db").read_bytes() == old_cache
    assert (tmp_path / "main" / AsyncSqliteStorage().database_path.name).is_file()
