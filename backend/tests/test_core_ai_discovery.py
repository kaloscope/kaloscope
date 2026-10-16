"""Unit tests for AI model discovery and upstream failure isolation."""

import asyncio

import httpx
import pytest

from app.core.ai.discovery import fetch_models
from app.core.ai.presets import get_provider_presets
from app.core.exceptions import KaloscopeException
from app.models.ai import AIProviderPreset


@pytest.mark.parametrize(
    "preset",
    get_provider_presets() + [{"id": None, "base_url": "https://custom.example/v1"}],
)
def test_catalog(preset):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"data": [{"id": "z-model"}, {"id": " a-model "}, {"id": "z-model"}]},
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_models(
                client, preset["base_url"], preset["id"], "secret"
            )

    assert asyncio.run(run()) == ["a-model", "z-model"]
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url).split("?")[0] == preset["base_url"] + "/models"
    assert request.headers["Cache-Control"] == "no-cache, no-store"
    if preset["id"] == "anthropic":
        assert request.headers["x-api-key"] == "secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in request.headers
    else:
        assert request.headers["Authorization"] == "Bearer secret"


def test_keyless_empty():
    def respond(request):
        assert "Authorization" not in request.headers
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"data": []})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert (
                await fetch_models(client, "http://localhost:11434/v1", None, None)
                == []
            )

    asyncio.run(run())


def test_pagination():
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.params["limit"] == "1000"
        if "after_id" not in request.url.params:
            return httpx.Response(
                200,
                json={"data": [{"id": "first"}], "has_more": True, "last_id": "first"},
            )
        assert request.url.params["after_id"] == "first"
        return httpx.Response(200, json={"data": [{"id": "second"}], "has_more": False})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_models(
                client,
                "https://api.anthropic.com/v1",
                AIProviderPreset.ANTHROPIC,
                "key",
            )

    assert asyncio.run(run()) == ["first", "second"]
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "ai_models_auth_failed"),
        (403, "ai_models_auth_failed"),
        (404, "ai_models_unsupported"),
        (405, "ai_models_unsupported"),
        (429, "ai_models_rate_limited"),
        (500, "ai_models_fetch_failed"),
        (302, "ai_models_fetch_failed"),
    ],
)
def test_upstream_error(status, code):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            status,
            text="secret-credential",
            headers={"Location": "https://other.example/models"},
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        ) as client:
            await fetch_models(
                client, "https://example.com/v1", None, "secret-credential"
            )

    with pytest.raises(KaloscopeException) as error:
        asyncio.run(run())
    assert str(error.value) == code
    assert error.value.status_code == 502
    assert "secret-credential" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("failure", "code", "status"),
    [
        (httpx.ReadTimeout, "ai_models_timeout", 504),
        (httpx.ConnectError, "ai_models_fetch_failed", 502),
    ],
)
def test_network_error(failure, code, status):
    def respond(request):
        raise failure("private upstream details", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await fetch_models(client, "https://example.com/v1", None, "secret")

    with pytest.raises(KaloscopeException) as error:
        asyncio.run(run())
    assert str(error.value) == code
    assert error.value.status_code == status


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"data": None},
        {"data": ["model"]},
        {"data": [{"id": 3}]},
        {"data": [{"id": ""}]},
        {"data": [{"id": "bad\nmodel"}]},
        {"data": [{"id": "x" * 256}]},
        {"data": [{"id": str(i)} for i in range(10001)]},
        {"data": [], "has_more": True},
        {"data": [{"id": "one"}], "has_more": True, "last_id": "one"},
    ],
)
def test_invalid_catalog(payload):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as client:
            await fetch_models(
                client, "https://example.com/v1", AIProviderPreset.ANTHROPIC, None
            )

    with pytest.raises(KaloscopeException, match="ai_models_invalid_response"):
        asyncio.run(run())


@pytest.mark.parametrize(
    "body", [b"<html>not JSON</html>", b"\xff", b" " * (8 * 1024 * 1024 + 1)]
)
def test_invalid_body(body):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
        ) as client:
            await fetch_models(client, "https://example.com/v1", None, None)

    with pytest.raises(KaloscopeException, match="ai_models_invalid_response"):
        asyncio.run(run())
