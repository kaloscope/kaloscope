"""Unit tests for the core middleware."""

import asyncio
from types import SimpleNamespace
from typing import Any

from sanic import HTTPResponse
from sanic.response import html, raw

from app.core.constants import URL_PREFIX
from app.core.middleware import on_response, set_cache_headers


def _request(path: str) -> Any:
    return SimpleNamespace(path=path, server_path=path, ctx=SimpleNamespace())


def _run_response(path: str, response: HTTPResponse) -> HTTPResponse:
    result = asyncio.run(on_response(_request(path), response))
    assert result is not None
    return result


def test_html_no_cache():
    """Frontend HTML must be revalidated after deployments."""
    response = html("<!doctype html>")

    result = _run_response("/", response)

    assert result.headers["Cache-Control"] == (
        "no-store, no-cache, must-revalidate, max-age=0"
    )
    assert result.headers["Pragma"] == "no-cache"
    assert result.headers["Expires"] == "0"


def test_pwa_files_no_cache():
    """PWA update metadata must not be served stale by browser caches."""
    response = raw(b"self.__WB_MANIFEST = []")

    result = _run_response("/service-worker.js", response)

    assert result.headers["Cache-Control"] == (
        "no-store, no-cache, must-revalidate, max-age=0"
    )


def test_api_cache_unchanged():
    """API responses should not receive frontend cache headers."""
    response = html("<!doctype html>")

    set_cache_headers(_request(f"{URL_PREFIX}/status"), response)

    assert "Cache-Control" not in response.headers
    assert "Pragma" not in response.headers
    assert "Expires" not in response.headers


def test_asset_cache_unchanged():
    """Non-HTML frontend assets should not receive no-cache headers."""
    response = raw(b"image")

    result = _run_response("/logo.png", response)

    assert "Cache-Control" not in result.headers
    assert "Pragma" not in result.headers
    assert "Expires" not in result.headers
