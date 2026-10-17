"""Unit tests for AI requests and structured output validation."""

import asyncio
import json
import traceback

import httpx
import pytest

from app.core.ai.completion import complete, output_validator
from app.core.exceptions import KaloscopeException
from app.models.ai import AICompletionConfig, AIProviderPreset

API_KEY = "private-key"

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "year": {"type": "integer"}},
    "required": ["title", "year"],
    "additionalProperties": False,
}


def config(**overrides):
    return AICompletionConfig.model_validate(
        {
            "model": {"provider_id": 1, "model_id": "test-model"},
            "prompt": "Summarize this title",
            "output_schema": json.dumps(SCHEMA),
            **overrides,
        }
    )


def response(content="Hello", **choice):
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": content}, **choice}
        ],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }


async def call(handler, options=None, preset=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await complete(
            client, "https://example.com/v1", preset, API_KEY, options or config()
        )


@pytest.mark.parametrize(
    ("mode", "native_schema"),
    [("text", False), ("text", True), ("json_object", False), ("json_object", True)],
)
def test_output_modes(mode, native_schema):
    requests = []
    content = "Hello" if mode == "text" else '{"title":"Example","year":2026}'

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response(content))

    result = asyncio.run(
        call(handler, config(response_format=mode, native_schema=native_schema))
    )

    assert result == {
        "text": content,
        "json": None if mode == "text" else {"title": "Example", "year": 2026},
        "model": "test-model",
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://example.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer private-key"
    assert request.headers["cache-control"] == "no-cache, no-store"
    body = json.loads(request.content)
    assert body["stream"] is False
    assert "max_tokens" not in body and "max_completion_tokens" not in body
    if mode == "text":
        assert "response_format" not in body
    else:
        assert body["response_format"]["type"] == (
            "json_schema" if native_schema else "json_object"
        )
        assert "JSON Schema" in body["messages"][0]["content"]
    if mode == "json_object" and native_schema:
        assert body["response_format"]["json_schema"]["schema"] == SCHEMA
        assert body["response_format"]["json_schema"]["strict"] is True


@pytest.mark.parametrize("native_schema", [False, True])
@pytest.mark.parametrize(
    "content",
    [
        "not JSON",
        "```json\n{}\n```",
        "[]",
        "null",
        "NaN",
        "{}",
        '{"title":"Example","year":"2026"}',
        '{"title":"Example","year":true}',
        '{"title":"Example","year":2026,"extra":1}',
    ],
)
def test_invalid_structure(content, native_schema):
    with pytest.raises(KaloscopeException, match="ai_output_invalid"):
        asyncio.run(
            call(
                lambda _: httpx.Response(200, json=response(content)),
                config(response_format="json_object", native_schema=native_schema),
            )
        )


@pytest.mark.parametrize(
    "schema",
    [
        "",
        "invalid",
        "[]",
        '{"type":"array"}',
        '{"type":"object","properties":{"title":{"type":"unknown"}}}',
        '{"type":"object","$ref":"https://example.com/private"}',
        '{"type":"object","properties":{"title":{"$dynamicRef":"#a"}}}',
        '{"type":"object","$schema":"http://json-schema.org/draft-07/schema#"}',
    ],
)
def test_invalid_schema(schema):
    with pytest.raises(KaloscopeException, match="ai_schema_invalid"):
        output_validator(config(response_format="json_object", output_schema=schema))


def test_text_ignores_schema():
    result = asyncio.run(
        call(
            lambda _: httpx.Response(200, json=response()),
            config(output_schema="unfinished schema", native_schema=True),
        )
    )

    assert result["text"] == "Hello"
    assert result["json"] is None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "ai_request_rejected"),
        (401, "ai_auth_failed"),
        (403, "ai_auth_failed"),
        (404, "ai_request_rejected"),
        (429, "ai_rate_limited"),
        (500, "ai_request_failed"),
        (302, "ai_request_failed"),
    ],
)
def test_upstream_errors(status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            text="private-key private-prompt",
            headers={"Location": "https://other.example"},
        )

    with pytest.raises(KaloscopeException, match=code) as error:
        asyncio.run(call(handler))

    assert len(requests) == 1
    assert "private-key" not in "".join(traceback.format_exception(error.value))
    assert "private-prompt" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "ai_response_invalid"),
        ({"choices": []}, "ai_response_invalid"),
        (response(""), "ai_response_invalid"),
        (response(None), "ai_response_invalid"),
        (response("partial", finish_reason="length"), "ai_output_truncated"),
        (response(None, finish_reason="content_filter"), "ai_output_refused"),
        (response(None, message={"refusal": "private reason"}), "ai_output_refused"),
        (response("tool", finish_reason="tool_calls"), "ai_response_invalid"),
    ],
)
def test_invalid_response(payload, code):
    with pytest.raises(KaloscopeException, match=code):
        asyncio.run(call(lambda _: httpx.Response(200, json=payload)))


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ReadTimeout("private-key"), "ai_timeout"),
        (httpx.ConnectError("private-key"), "ai_request_failed"),
    ],
)
def test_connection_failure(exception, code):
    def handler(_):
        raise exception

    with pytest.raises(KaloscopeException, match=code) as error:
        asyncio.run(call(handler))

    assert "private-key" not in "".join(traceback.format_exception(error.value))


def test_response_limit():
    with pytest.raises(KaloscopeException, match="ai_response_invalid"):
        asyncio.run(
            call(lambda _: httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)))
        )


@pytest.mark.parametrize(
    ("preset", "field"),
    [
        (AIProviderPreset.OPENAI, "max_completion_tokens"),
        (AIProviderPreset.DEEPSEEK, "max_tokens"),
        (AIProviderPreset.ANTHROPIC, "max_tokens"),
        (None, "max_tokens"),
    ],
)
def test_token_limit(preset, field):
    def handler(request):
        assert json.loads(request.content)[field] == 2048
        return httpx.Response(200, json=response())

    asyncio.run(call(handler, config(max_tokens=2048), preset))


def test_cancellation():
    def handler(_):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(call(handler))
