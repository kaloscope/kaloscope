"""Unit tests for AI nodes, provider lifecycle changes, and graph publication."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import KaloscopeException
from app.core.flow.context import Context
from app.core.flow.engine import NodeWrapper, TransientTask
from app.core.flow.nodes.base import Node
from app.core.flow.nodes.general.ai import AINode
from app.models.ai import AIProvider, AIProviderUpsert
from app.models.flow import FlowGraph, GraphCategory, GraphState
from app.services import ai as ai_service
from app.services.ai import AIProviderService
from app.services.flow import FlowGraphService, FlowLogService
from app.utils.dict import TrackableDict


@asynccontextmanager
async def database():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        yield
    finally:
        await Tortoise.close_connections()


def context():
    ctx = Context.__new__(Context)
    ctx.globalvars = {}
    ctx.localvars = {}
    ctx.bootparams = {}
    ctx.storage = TrackableDict({"title": "Example", "ai_result": {"old": True}})
    ctx.union()
    return ctx


def data(provider_id=1, **overrides):
    return {
        "model": {"provider_id": provider_id, "model_id": "test-model"},
        "prompt": "Summarize {{ title }}",
        **overrides,
    }


def definition(provider_id=1, **overrides):
    return {
        "nodes": [
            {
                "id": "ai-node",
                "data": {
                    "$schema": {"node_type": "ai"},
                    **data(provider_id, **overrides),
                },
            }
        ],
        "edges": [],
    }


@pytest.fixture
def network(monkeypatch, tmp_path):
    monkeypatch.setattr(
        KaloscopeConfig,
        "_config",
        SimpleNamespace(secret_key_path=tmp_path / "secret.key"),
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"result":"summary"}'},
                    }
                ]
            },
        )

    monkeypatch.setattr(
        ai_service, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )
    return requests


@pytest.mark.parametrize("temperature", [None, 0, 0.7, 2])
def test_execution(network, temperature):
    async def run():
        async with database():
            provider = await AIProviderService.upsert(
                AIProviderUpsert(
                    name="Example",
                    base_url="https://example.com/v1",
                    api_key="private-key",
                    model_ids=["test-model"],
                )
            )
            node_data = data(
                provider.id,
                system_prompt="You summarize {{ title }}",
                response_format="json_object",
                temperature=temperature,
            )
            ctx = context()

            task = TransientTask(1, {}, True)
            task._context = ctx
            node = NodeWrapper("ai-node", node_type="ai", node_data=node_data)
            async with task.context(node) as local:
                await AINode.execute(node_data=node_data, context=local)

            assert ctx["ai_result"]["json"] == {"result": "summary"}
            assert "old" not in ctx["ai_result"]
            assert ctx["title"] == "Example"
            assert "private-key" not in json.dumps(node_data)
            assert "private-key" not in json.dumps(dict(ctx.storage))
            assert network[0].headers["Authorization"] == "Bearer private-key"
            body = json.loads(network[0].content)
            if temperature is None:
                assert "temperature" not in body
            else:
                assert body["temperature"] == temperature
            messages = body["messages"]
            assert messages[0]["content"].startswith("You summarize Example")
            assert messages[-1]["content"] == "Summarize Example"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("disable", "ai_provider_disabled"),
        ("delete", "ai_provider_missing"),
        ("remove_model", "ai_model_missing"),
        ("bad_key", "ai_key_unavailable"),
    ],
)
def test_published_dependency(network, change, code):
    async def run():
        async with database():
            provider = await AIProvider.create(
                name="Example",
                base_url="https://example.com/v1",
                model_ids=["test-model"],
            )
            graph = await FlowGraph.create(
                name="Test", category=GraphCategory.SCHEDULE, state=GraphState.DRAFT
            )
            published = await FlowGraphService.publish_graph(
                graph.id, definition(provider.id)
            )
            saved = copy.deepcopy(published.definition)

            if change == "delete":
                await AIProviderService.delete(provider.id)
            else:
                if change == "disable":
                    provider.enabled = False
                elif change == "remove_model":
                    provider.model_ids = []
                else:
                    provider.api_key = "invalid-ciphertext"
                await provider.save()

            ctx = context()
            node_data = {**copy.deepcopy(saved["nodes"][0]["data"]), "max_retries": 3}
            with pytest.raises(KaloscopeException, match=code):
                await AINode.execute(node_data=node_data, context=ctx)
            assert ctx["ai_result"] == {"old": True}
            assert node_data["$error"] == code
            assert network == []
            await graph.refresh_from_db()
            assert graph.state == GraphState.PUBLISHED
            assert graph.definition == saved

            if change != "bad_key":
                with pytest.raises(KaloscopeException, match=code):
                    await FlowGraphService.publish_graph(
                        graph.id, definition(provider.id)
                    )
                await graph.refresh_from_db()
                assert graph.definition == saved

    asyncio.run(run())


def test_reenable_and_reconfigure(network):
    async def run():
        async with database():
            provider = await AIProvider.create(
                name="Example",
                base_url="https://example.com/v1",
                enabled=False,
                model_ids=["test-model"],
            )
            with pytest.raises(KaloscopeException, match="ai_provider_disabled"):
                await AINode.execute(node_data=data(provider.id), context=context())
            provider.enabled = True
            provider.base_url = "https://new.example/v1"
            await provider.save()

            await AINode.execute(node_data=data(provider.id), context=context())

            assert len(network) == 1
            assert network[0].url.host == "new.example"
            assert "authorization" not in network[0].headers

    asyncio.run(run())


@pytest.mark.parametrize(
    "overrides",
    [
        {"timeout": 0},
        {"timeout": True},
        {"max_tokens": -1},
        {"max_retries": -1},
        {"max_retries": 4},
        {"max_retries": True},
        {"max_retries": 1.5},
        {"max_retries": "1"},
        {"max_retries": None},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": True},
        {"temperature": False},
        {"temperature": "0.7"},
        {"temperature": ""},
        {"temperature": float("nan")},
        {"temperature": float("inf")},
        {"temperature": float("-inf")},
        {"native_schema": "true"},
        {"prompt": " "},
        {"result_key": "$retval"},
        {"model": None},
    ],
)
def test_invalid_configuration(overrides):
    with pytest.raises(KaloscopeException, match="ai_config_invalid"):
        AINode.configuration(data(**overrides))


@pytest.mark.parametrize("max_retries", [0, 1, 2, 3])
def test_retry_configuration(max_retries):
    assert AINode.configuration(data()).max_retries == 0
    assert (
        AINode.configuration(data(max_retries=max_retries)).max_retries == max_retries
    )


@pytest.mark.parametrize("temperature", [None, 0, 0.7, 1, 2])
def test_temperature_configuration(temperature):
    assert AINode.configuration(data()).temperature is None
    assert (
        AINode.configuration(data(temperature=temperature)).temperature == temperature
    )


def test_missing_prompt_variable(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(AIProviderService, "complete", call)
    ctx = context()

    with pytest.raises(KaloscopeException, match="ai_prompt_invalid"):
        asyncio.run(AINode.execute(node_data=data(prompt="{{ absent }}"), context=ctx))

    call.assert_not_awaited()
    assert ctx["ai_result"] == {"old": True}


def test_failed_output_does_not_continue(network):
    async def run():
        async with database():
            provider = await AIProvider.create(
                name="Example",
                base_url="https://example.com/v1",
                model_ids=["test-model"],
            )
            graph = await FlowGraph.create(
                name="Test", category=GraphCategory.SCHEDULE, state=GraphState.PUBLISHED
            )
            task = TransientTask(graph.id, {}, True)
            task._context = context()
            schema = '{"type":"object","required":["missing"]}'
            node_data = data(
                provider.id,
                response_format="json_object",
                output_schema=schema,
                max_retries=3,
            )
            node = NodeWrapper("ai-node", node_type="ai", node_data=node_data)

            with pytest.raises(KaloscopeException, match="ai_output_invalid"):
                async with task.context(node) as ctx:
                    await AINode._execute(
                        graph_id=graph.id,
                        node_id="ai-node",
                        node_data=node_data,
                        context=ctx,
                    )
            logs = await FlowLogService.get_logs(graph.id)

            assert task._context["ai_result"] == {"old": True}
            assert "$output" not in node_data
            assert logs[0][-1]["type"] == "ai"
            assert logs[0][-1]["error"] == "ai_output_invalid"
            assert "private-key" not in str(logs)
            assert len(network) == 1

    asyncio.run(run())


@pytest.mark.parametrize("succeeds", [True, False])
def test_retry_context(succeeds, monkeypatch):
    requests = []
    ctx = context()
    sleep = AsyncMock()
    monkeypatch.setattr("app.core.ai.completion.asyncio.sleep", sleep)

    def handler(request):
        requests.append(request)
        assert ctx["ai_result"] == {"old": True}
        if not succeeds or len(requests) < 3:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"result":"summary"}'},
                    }
                ]
            },
        )

    monkeypatch.setattr(
        ai_service, "NetworkTransport", lambda **_: httpx.MockTransport(handler)
    )

    async def run():
        async with database():
            provider = await AIProvider.create(
                name="Example",
                base_url="https://example.com/v1",
                model_ids=["test-model"],
            )
            graph = await FlowGraph.create(
                name="Test", category=GraphCategory.SCHEDULE, state=GraphState.PUBLISHED
            )
            task = TransientTask(graph.id, {}, True)
            task._context = ctx
            node_data = data(provider.id, response_format="json_object", max_retries=2)
            node = NodeWrapper("ai-node", node_type="ai", node_data=node_data)

            output = None
            try:
                async with task.context(node) as local:
                    output = await AINode._execute(
                        graph_id=graph.id,
                        node_id="ai-node",
                        node_data=node_data,
                        context=local,
                    )
            except KaloscopeException as error:
                assert not succeeds
                assert str(error.message) == "ai_request_failed"

            if succeeds:
                assert ctx["ai_result"]["json"] == {"result": "summary"}
                assert "old" not in ctx["ai_result"]
                assert "$error" not in node_data
                assert output is not None and output.id == "output"
            else:
                assert ctx["ai_result"] == {"old": True}
                assert node_data["$error"] == "ai_request_failed"
                assert output is None
                assert "$output" not in node_data
                logs = await FlowLogService.get_logs(graph.id)
                assert logs[0][-1]["error"] == "ai_request_failed"

    asyncio.run(run())

    assert len(requests) == 3
    assert sleep.await_count == 2


def test_schema_registration():
    schema = next(item for item in Node.schemas if item.node_type == "ai")
    assert [handle.id for handle in schema.handles] == ["input", "output"]
    assert schema.fields[0].field_type == "ai_model"
    assert schema.icon == "layerDiagonalSparkle"
    fields = {field.id: field for field in schema.fields}
    assert set(fields["response_format"].options.values()) == {"text", "json_object"}
    assert fields["native_schema"].default is False
    assert fields["native_schema"].visible_when == {"response_format": "json_object"}
    assert fields["output_schema"].visible_when == {"response_format": "json_object"}
