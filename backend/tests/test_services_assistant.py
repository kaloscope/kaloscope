"""Unit tests for SDK streaming, bounded tool execution, and cancellation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.exceptions import ErrorCode, KaloscopeException
from app.models.ai import AIProviderPreset
from app.models.assistant import AssistantChat
from app.models.user import UserRole
from app.services import assistant as service

ADMIN = SimpleNamespace(id=1, role=UserRole.ADMIN)


def chat(text="查看 AI 节点"):
    return AssistantChat.model_validate(
        {
            "model": {"provider_id": 1, "model_id": "test-model"},
            "messages": [{"role": "user", "content": text}],
        }
    )


def chunk(delta=None, finish=None, usage=None):
    return {
        "id": "completion-one",
        "created": 1,
        "model": "test-model",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        "usage": usage,
    }


def call_delta(name="list_node_types", arguments="{}", id="call-one", index=0):
    return {
        "index": index,
        "id": id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class EventStream(httpx.AsyncByteStream):
    def __init__(self, events, *, wait=False):
        self.payload = b"".join(
            b"data: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n"
            for event in events
        )
        self.wait = wait
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        for start in range(0, len(self.payload), 17):
            yield self.payload[start : start + 17]
        if self.wait:
            self.waiting.set()
            await asyncio.Future()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


def response(events, *, wait=False):
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream"},
        stream=EventStream(events, wait=wait),
    )


def configure(monkeypatch, handler, *, secret="private-key", preset=None):
    provider = SimpleNamespace(
        base_url="https://example.test/v1", preset=preset, api_key="ciphertext"
    )
    monkeypatch.setattr(
        service.AIProviderService, "resolve_model", AsyncMock(return_value=provider)
    )
    monkeypatch.setattr(
        service.AIProviderService, "decrypt_key", AsyncMock(return_value=secret)
    )
    monkeypatch.setattr(
        service, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    return provider


async def collect(body=None, user=ADMIN):
    return [
        event async for event in service.AssistantService.stream(body or chat(), user)
    ]


def test_text_and_usage(monkeypatch):
    requests = []
    upstream = response(
        [
            chunk({"role": "assistant", "content": "你"}),
            chunk({"content": "好"}),
            chunk(
                finish="stop",
                usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            ),
        ]
    )

    def handler(request):
        requests.append(request)
        return upstream

    configure(monkeypatch, handler, preset=AIProviderPreset.OPENAI)
    events = asyncio.run(collect())

    assert [event.event for event in events] == [
        "start",
        "text_delta",
        "text_delta",
        "done",
    ]
    assert (
        "".join(event.data["text"] for event in events if event.event == "text_delta")
        == "你好"
    )
    assert events[-1].data["usage"]["total_tokens"] == 7
    assert upstream.is_closed
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://example.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer private-key"
    payload = json.loads(request.content)
    assert payload["stream"] is True
    assert payload["max_completion_tokens"] == 4096
    assert payload["messages"][0]["role"] == "system"
    assert "private-key" not in json.dumps([event.model_dump() for event in events])


def test_fragmented_tool_call(monkeypatch):
    requests = []
    streams = [
        response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [call_delta(arguments='{"node_')],
                    }
                ),
                chunk(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": 'type":"ai"}'}}
                        ]
                    }
                ),
                chunk(finish="tool_calls"),
            ]
        ),
        response(
            [
                chunk({"role": "assistant", "content": "节点已读取"}),
                chunk(finish="stop"),
            ]
        ),
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return streams[len(requests) - 1]

    configure(monkeypatch, handler)
    events = asyncio.run(collect())

    assert [event.event for event in events] == [
        "start",
        "tool_start",
        "tool_result",
        "text_delta",
        "done",
    ]
    assert events[2].data["ok"] is True
    assert events[-1].data["tool_calls"] == 1
    second = requests[1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["arguments"] == '{"node_type":"ai"}'
    assert second[-1]["role"] == "tool"
    assert second[-1]["tool_call_id"] == "call-one"
    assert json.loads(second[-1]["content"])["ok"] is True
    assert all(stream.is_closed for stream in streams)


@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [("save_workflow_draft", "{}", "forbidden"), ("get_workflow", "{}", "bad_request")],
)
def test_tool_failure_recovery(monkeypatch, name, arguments, code):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return response(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "tool_calls": [call_delta(name, arguments)],
                        }
                    ),
                    chunk(finish="tool_calls"),
                ]
            )
        return response(
            [
                chunk({"role": "assistant", "content": "无法完成该操作"}),
                chunk(finish="stop"),
            ]
        )

    configure(monkeypatch, handler)
    events = asyncio.run(collect())

    result = next(event.data for event in events if event.event == "tool_result")
    assert result["error"]["code"] == code
    assert events[-1].event == "done"
    assert json.loads(requests[1]["messages"][-1]["content"])["ok"] is False


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "ai_auth_failed"),
        (403, "ai_auth_failed"),
        (429, "ai_rate_limited"),
        (400, "ai_request_rejected"),
        (500, "ai_request_failed"),
        (302, "ai_request_failed"),
    ],
)
def test_http_errors(monkeypatch, status, code):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            json={"error": {"message": "private upstream details"}},
            headers={"location": "https://other.test"},
        )

    configure(monkeypatch, handler)
    events = asyncio.run(collect())

    assert len(requests) == 1
    assert events[-1].event == "error"
    assert events[-1].data == {"code": code}
    assert "private upstream" not in json.dumps(
        [event.model_dump() for event in events]
    )


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
        ([chunk({"role": "assistant", "content": "partial"})], "ai_response_invalid"),
        (
            [chunk({"role": "assistant", "content": "partial"}, "length")],
            "ai_output_truncated",
        ),
        ([chunk({"role": "assistant"}, "content_filter")], "ai_output_refused"),
        ([chunk({"role": "assistant", "refusal": "no"}, "stop")], "ai_output_refused"),
        ([chunk({"role": "assistant"}, "stop")], "ai_response_invalid"),
        ([{}], "ai_response_invalid"),
    ],
)
def test_invalid_stream(monkeypatch, chunks, code):
    upstream = response(chunks)
    configure(monkeypatch, lambda _: upstream)
    events = asyncio.run(collect())

    assert events[-1].event == "error"
    assert events[-1].data["code"] == code
    assert upstream.is_closed


def test_keyless_client(monkeypatch):
    requests = []
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "unrelated-admin-key")
    monkeypatch.setenv("OPENAI_ORG_ID", "unrelated-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "unrelated-project")

    def handler(request):
        requests.append(request)
        return response([chunk({"role": "assistant", "content": "ok"}, "stop")])

    configure(monkeypatch, handler, secret=None, preset=AIProviderPreset.OLLAMA)
    events = asyncio.run(collect())

    assert events[-1].event == "done"
    assert not any("unrelated" in value for value in requests[0].headers.values())
    assert "authorization" not in requests[0].headers
    assert json.loads(requests[0].content)["max_tokens"] == 4096


def test_provider_failure(monkeypatch):
    configure(monkeypatch, lambda _: pytest.fail("unexpected upstream request"))
    monkeypatch.setattr(
        service.AIProviderService,
        "resolve_model",
        AsyncMock(side_effect=KaloscopeException(ErrorCode.AI_PROVIDER_DISABLED)),
    )
    events = asyncio.run(collect())

    assert events[-1].data == {"code": "ai_provider_disabled"}


@pytest.mark.parametrize("limit", ["MAX_MODEL_TURNS", "MAX_TOOL_CALLS"])
def test_call_limits(monkeypatch, limit):
    requests = []
    monkeypatch.setattr(service, limit, 1)

    def handler(request):
        requests.append(request)
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [call_delta(id=f"call-{len(requests)}")],
                    }
                ),
                chunk(finish="tool_calls"),
            ]
        )

    configure(monkeypatch, handler)
    events = asyncio.run(collect())

    assert events[-1].data == {"code": "ai_assistant_limit"}
    assert sum(event.event == "tool_result" for event in events) == 1
    assert len(requests) == (1 if limit == "MAX_MODEL_TURNS" else 2)


@pytest.mark.parametrize("limit", ["MAX_CONTEXT_BYTES", "MAX_RESPONSE_BYTES"])
def test_byte_limits(monkeypatch, limit):
    monkeypatch.setattr(service, limit, 1)
    upstream = response([chunk({"role": "assistant", "content": "large"}, "stop")])
    configure(monkeypatch, lambda _: upstream)
    events = asyncio.run(collect())

    assert events[-1].data == {"code": "ai_assistant_limit"}
    if limit == "MAX_RESPONSE_BYTES":
        assert upstream.is_closed


def test_batch_limit(monkeypatch):
    upstream = response(
        [
            chunk(
                {
                    "role": "assistant",
                    "tool_calls": [call_delta(id=str(i), index=i) for i in range(9)],
                }
            ),
            chunk(finish="tool_calls"),
        ]
    )
    configure(monkeypatch, lambda _: upstream)
    execute = AsyncMock()
    monkeypatch.setattr(service, "execute_tool", execute)
    events = asyncio.run(collect())

    assert events[-1].data == {"code": "ai_assistant_limit"}
    execute.assert_not_awaited()


def test_duplicate_call_ids(monkeypatch):
    upstream = response(
        [
            chunk(
                {
                    "role": "assistant",
                    "tool_calls": [call_delta(index=i) for i in range(2)],
                }
            ),
            chunk(finish="tool_calls"),
        ]
    )
    configure(monkeypatch, lambda _: upstream)
    execute = AsyncMock()
    monkeypatch.setattr(service, "execute_tool", execute)
    events = asyncio.run(collect())

    assert events[-1].data == {"code": "ai_response_invalid"}
    execute.assert_not_awaited()


def test_reused_upstream_id(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) <= 2:
            return response(
                [
                    chunk({"role": "assistant", "tool_calls": [call_delta()]}),
                    chunk(finish="tool_calls"),
                ]
            )
        return response([chunk({"role": "assistant", "content": "done"}, "stop")])

    configure(monkeypatch, handler)
    events = asyncio.run(collect())

    assert events[-1].event == "done"
    assert events[-1].data["tool_calls"] == 2
    starts = [event.data["id"] for event in events if event.event == "tool_start"]
    results = [event.data["id"] for event in events if event.event == "tool_result"]
    assert starts == results
    assert len(set(starts)) == 2
    assert requests[1]["messages"][-1]["tool_call_id"] == "call-one"
    assert requests[2]["messages"][-1]["tool_call_id"] == "call-one"


def test_deadline(monkeypatch):
    monkeypatch.setattr(service, "TURN_TIMEOUT", 1)
    upstream = response([chunk({"role": "assistant", "content": "partial"})], wait=True)
    configure(monkeypatch, lambda _: upstream)
    events = asyncio.run(collect())

    assert events[-1].data == {"code": "ai_timeout"}
    assert upstream.is_closed


def test_cancel_model(monkeypatch):
    async def run():
        upstream = response(
            [chunk({"role": "assistant", "content": "partial"})], wait=True
        )
        configure(monkeypatch, lambda _: upstream)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(upstream.stream.waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert upstream.is_closed

    asyncio.run(run())


def test_close_paused_stream(monkeypatch):
    async def run():
        upstream = response(
            [chunk({"role": "assistant", "content": "partial"})], wait=True
        )
        configure(monkeypatch, lambda _: upstream)
        events = service.AssistantService.stream(chat(), ADMIN)

        assert (await anext(events)).event == "start"
        assert (await anext(events)).event == "text_delta"
        await events.aclose()

        assert upstream.is_closed

    asyncio.run(run())


def test_cancel_tool(monkeypatch):
    async def run():
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def execute(*_):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        upstream = response(
            [
                chunk({"role": "assistant", "tool_calls": [call_delta()]}),
                chunk(finish="tool_calls"),
            ]
        )
        configure(monkeypatch, lambda _: upstream)
        monkeypatch.setattr(service, "execute_tool", execute)
        task = asyncio.create_task(collect())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stopped.is_set()
        assert upstream.is_closed

    asyncio.run(run())


def test_concurrent_accounts(monkeypatch):
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["messages"][-1]["role"] == "user":
            return response(
                [
                    chunk({"role": "assistant", "tool_calls": [call_delta()]}),
                    chunk(finish="tool_calls"),
                ]
            )
        result = json.loads(payload["messages"][-1]["content"])
        return response(
            [
                chunk(
                    {"role": "assistant", "content": str(result["data"]["account"])},
                    "stop",
                )
            ]
        )

    async def execute(name, arguments, user):
        await asyncio.sleep(0)
        return {"ok": True, "data": {"account": user.id}}

    configure(monkeypatch, handler)
    monkeypatch.setattr(service, "execute_tool", execute)

    async def run():
        return await asyncio.gather(
            collect(chat("conversation A"), SimpleNamespace(id=1, role=UserRole.ADMIN)),
            collect(chat("conversation B"), SimpleNamespace(id=2, role=UserRole.ADMIN)),
        )

    first, second = asyncio.run(run())
    assert first[0].data["turn_id"] != second[0].data["turn_id"]
    for events, account in ((first, 1), (second, 2)):
        assert events[-1].event == "done"
        assert (
            next(
                event.data["data"]["account"]
                for event in events
                if event.event == "tool_result"
            )
            == account
        )
        assert next(
            event.data["text"] for event in events if event.event == "text_delta"
        ) == str(account)
    for payload in requests:
        content = json.dumps(payload["messages"])
        assert ("conversation A" in content) != ("conversation B" in content)


def test_network_timeout(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("private upstream details", request=request)

    configure(monkeypatch, handler)
    events = asyncio.run(collect())

    assert events[-1].data == {"code": "ai_timeout"}
