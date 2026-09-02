import asyncio
import inspect
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx

from app.core.dl.endpoint import Endpoint
from app.routes import download as download_routes
from app.services import download as download_service


def test_openlist_tools():
    requests = []

    def handler(request):
        requests.append(
            (request.method, str(request.url), "authorization" in request.headers)
        )
        return httpx.Response(
            200,
            json={
                "code": 200,
                "message": "success",
                "data": ["Future Tool", "Cloud Tool"],
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            request = SimpleNamespace(
                app=SimpleNamespace(ctx=SimpleNamespace(httpx=client)),
                args={"path": "/Cloud/Kaloscope"},
            )
            body = Endpoint(
                protocol="https",
                host="OpenList.Example.COM",
                port=443,
                path="/root/api/",
            )
            route = inspect.unwrap(cast(Any, download_routes.get_openlist_tools))
            return await route(request, body)

    response = asyncio.run(run())

    assert json.loads(response.body) == ["Future Tool", "Cloud Tool"]
    assert requests == [
        (
            "GET",
            "https://openlist.example.com/root/api/public/offline_download_tools?path=%2FCloud%2FKaloscope",
            False,
        )
    ]


def test_validate_downloader(monkeypatch):
    source = "https://example.com/file.iso?token=secret"
    driver = SimpleNamespace(source_types=frozenset({"raw"}))
    downloader_model = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(config="invalid"))
    )
    magnet_probe = AsyncMock(return_value=None)
    monkeypatch.setattr(download_service, "Downloader", downloader_model)
    monkeypatch.setattr(download_service, "decrypt_config", lambda config: config)
    monkeypatch.setattr(download_service, "load_driver", lambda _config: driver)
    monkeypatch.setattr(download_service, "standardize_magnet", magnet_probe)
    request = SimpleNamespace(
        body=source.encode(),
        args={"downloader_id": "2"},
    )

    handler = cast(Any, download_routes.valid_magnet_link)
    response = asyncio.run(handler(request))

    assert json.loads(response.body) is True
    magnet_probe.assert_not_awaited()


def test_validate_magnet(monkeypatch):
    magnet_probe = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(download_routes, "standardize_magnet", magnet_probe)
    request = SimpleNamespace(body=b"legacy-magnet", args={})

    handler = cast(Any, download_routes.valid_magnet_link)
    response = asyncio.run(handler(request))

    assert json.loads(response.body) is True
    magnet_probe.assert_awaited_once_with("legacy-magnet")
