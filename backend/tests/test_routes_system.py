"""Unit tests for system version checks."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.exceptions import ErrorCode, KaloscopeException
from app.routes import system as system_routes


@pytest.fixture
def version_file(tmp_path, monkeypatch):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nversion = "0.9.0"\n')
    monkeypatch.setattr(system_routes, "_PYPROJECT", path)
    return path


async def _get_version(handler, check_update=True):
    """Call the version route with an isolated HTTP client.

    Args:
        handler: Mock transport handler for Docker Hub requests.
        check_update: Whether to request an update check.

    Returns:
        Decoded version response.
    """
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        request = SimpleNamespace(
            app=SimpleNamespace(ctx=SimpleNamespace(httpx=client)),
            args={"check_update": "true"} if check_update else {},
        )
        response = await system_routes.get_app_version(request)
        return json.loads(response.body)


def test_current_version(version_file):
    def handler(_):
        pytest.fail("Reading the current version must not contact Docker Hub")

    result = asyncio.run(_get_version(handler, check_update=False))

    assert result == {"version": "0.9.0"}


@pytest.mark.parametrize(
    ("current", "has_update"),
    [("0.9.0", True), ("0.10.2", False), ("0.11.0", False)],
)
def test_updates(version_file, current, has_update):
    version_file.write_text(f'[project]\nversion = "{current}"\n')

    def handler(_):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": name}
                    for name in (
                        "latest",
                        "0.10",
                        "0.9.9",
                        "0.10.1",
                        "v0.10.2",
                        "1.0.0-rc.1",
                        "main",
                    )
                ],
                "next": None,
            },
        )

    result = asyncio.run(_get_version(handler))

    assert result == {
        "version": current,
        "latest_version": "0.10.2",
        "has_update": has_update,
    }


def test_pagination(version_file):
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        assert request.url.host == "hub.docker.com"
        assert request.url.params["page_size"] == "100"
        return httpx.Response(
            200,
            json={
                "results": [{"name": "0.9.0" if page == 1 else "0.12.0"}],
                "next": "https://hub.docker.com/?page=2" if page == 1 else None,
            },
        )

    result = asyncio.run(_get_version(handler))

    assert pages == [1, 2]
    assert result["latest_version"] == "0.12.0"
    assert result["has_update"] is True


@pytest.mark.parametrize("failure", ["http", "timeout", "empty", "invalid", "missing"])
def test_failed_check(version_file, failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("Timed out", request=request)
        if failure == "http":
            return httpx.Response(429)
        if failure == "invalid":
            return httpx.Response(200, text="invalid JSON")
        if failure == "missing":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"results": [{"name": "latest"}], "next": None})

    with pytest.raises(KaloscopeException) as error:
        asyncio.run(_get_version(handler))

    assert error.value.status_code == 502
    assert error.value.message == ErrorCode.HTTP_REQUEST_FAILED


def test_missing_version(version_file):
    version_file.unlink()

    def handler(_):
        pytest.fail("An unknown current version cannot be compared")

    assert asyncio.run(_get_version(handler, check_update=False)) == {"version": ""}
    with pytest.raises(KaloscopeException):
        asyncio.run(_get_version(handler))
