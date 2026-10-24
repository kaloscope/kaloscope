"""Tests for shared node knowledge, examples and runtime authoring contracts."""

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from jinja2.defaults import DEFAULT_FILTERS
from jinja2.exceptions import TemplateSyntaxError
from tortoise import Tortoise

from app.core.ai import history, tools
from app.core.config import KaloscopeConfig
from app.core.flow.context import Context
from app.core.flow.engine import FlowEngine
from app.core.flow.nodes.base import Node
from app.core.flow.nodes.end.auth import AuthEndNode
from app.core.flow.nodes.end.search import SearchEndNode
from app.core.flow.nodes.general.http import HTTPNode
from app.core.flow.nodes.general.script import ScriptNode
from app.core.flow.nodes.general.variables import VariablesNode
from app.core.flow.nodes.metadata.common import parse_candidates
from app.core.flow.reference import get_workflow_reference
from app.core.renderer import ENV, render
from app.models.flow import FlowGraph, FlowVariable
from app.models.user import UserRole
from app.services.assistant import SYSTEM_PROMPT
from app.services.flow import FlowGraphService
from app.utils.dict import TrackableDict
from app.utils.json import dumps, loads, try_loads

ROOT = Path(__file__).parents[1] / "static" / "flow"
USER = SimpleNamespace(id=1, role=UserRole.ADMIN)
SCHEMAS = sorted(Node.schemas, key=lambda node: node.node_type)
EXAMPLES = [
    (node.node_type, field)
    for node in SCHEMAS
    for field in node.fields
    if getattr(field, "template", None)
]


def context(params):
    """Build a runtime context without a database.

    Args:
        params: Caller-supplied boot parameters.

    Returns:
        Mutable context with no global or cached values.
    """
    value = Context.__new__(Context)
    value.globalvars = {}
    value.localvars = {}
    value.bootparams = params
    value.storage = TrackableDict()
    value.union()
    return value


async def query(name, args):
    """Read authoring metadata using the actual assistant dispatcher.

    Args:
        name: Registered tool name.
        args: Tool arguments.

    Returns:
        Successful tool envelope.
    """
    result = await tools.execute_tool(
        name, dumps(args).decode(), USER, conversation_id=1, call_id="knowledge"
    )
    assert result["ok"], result
    return {"name": name, **result}


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda schema: schema.node_type)
def test_node_contract(schema):
    result = asyncio.run(query("list_node_types", {"node_type": schema.node_type}))
    node = result["data"][schema.group][0]

    assert node["description"] == schema.description
    assert schema.description.isascii()
    assert schema.description != "The base class for all nodes."
    assert node["categories"] == list(schema.categories)
    assert [handle["id"] for handle in node["handles"]] == [
        h.id for h in schema.handles
    ]
    fields = {field["id"]: field for field in node["fields"]}
    for original in schema.fields:
        assert fields[original.id]["default"] == loads(dumps(original.default))
        assert fields[original.id]["required"] == original.required
        for key in ("min", "max", "maxlength", "options", "readonly", "visible_when"):
            if (value := getattr(original, key, None)) is not None:
                assert fields[original.id][key] == loads(dumps(value))


@pytest.mark.parametrize(("node_type", "field"), EXAMPLES, ids=[n for n, _ in EXAMPLES])
def test_examples(node_type, field):
    assert field.readonly
    assert field.default == (ROOT / field.template).read_text()
    assert field.default.isascii()
    example = try_loads(field.default, with_comments=True)
    if node_type in {"novel", "comic"}:
        parsed = parse_candidates(
            field.default, "opf" if node_type == "novel" else "comicinfo"
        )
        assert parsed[0]["title"] == example[0]["title"]
        assert parsed[0]["rating"] == str(example[0]["rating"])
        assert "originaltitle" not in parsed[0]
    elif node_type in {"board_end", "movie", "tv_show", "episode"}:
        assert isinstance(example, list) and example
    else:
        assert isinstance(example, dict) and example


def test_example_inventory():
    assert {f.template for _, f in EXAMPLES} == {
        str(path.relative_to(ROOT)) for path in ROOT.rglob("*.jsonc")
    }
    schema = FlowGraphService.get_node_schemas(node_type="http")["general"][0]
    assert [f.id for f in schema.fields if f.field_type == "divider"] == ["divider_9"]


def test_indexer_examples():
    examples = {
        node_type: try_loads(field.default, with_comments=True)
        for node_type, field in EXAMPLES
    }
    page = examples["search_end"]
    resource = page["items"][0]
    details = examples["details_end"]

    assert len(page["items"]) == examples["search_start"]["page_size"]
    assert page["total"] > len(page["items"])
    assert examples["details_start"]["id"] == resource["id"] == details["id"]
    assert examples["board_end"][0]["items"][0] == resource
    assert details["title"] == resource["title"]
    assert details["url"] != resource["link"]


def test_template_reference():
    content = get_workflow_reference("templates").content
    custom = {
        name
        for name, function in ENV.filters.items()
        if function is not DEFAULT_FILTERS.get(name)
    }
    assert custom
    for name in custom:
        assert re.search(rf"\b{re.escape(name)}\b", content), name
    examples = re.findall(r"`(\{\{.+?\}\})` -> `([^`]+)`", content)
    assert len(examples) >= 5
    for expression, expected in examples:
        assert render(expression, {}) == expected
    assert (
        render(
            "{{ payload|jsonpath_all('$.items[*].id')|tojson }}",
            {"payload": {"items": [{"id": "a"}, {"id": "b"}]}},
        )
        == '["a", "b"]'
    )
    assert render("{{ 'ID=42'|regex_first('ID=(\\\\d+)') }}", {}) == "42"


def test_context_example(monkeypatch):
    from app.core.flow.nodes.general import http

    guide = get_workflow_reference("context").content
    variables = '{"query": {{ keyword|tojson }}}'
    formatter = "{\"items\": {{ response.text|jsonpath_all('$.items[*]')|tojson }}}"
    response = '{"total": {{ items|length }}, "items": {{ items|tojson }}}'
    for expression in (variables, formatter, response):
        assert expression in guide
    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(400, json={"items": [{"id": "a", "title": "Example"}]})

    async def run():
        ctx = context({"keyword": "cats"})
        ctx.globalvars = {"keyword": "global"}
        ctx.localvars = {"keyword": "cached"}
        ctx.union()
        await VariablesNode.execute(node_data={"variables": variables}, context=ctx)
        async with httpx.AsyncClient(transport=httpx.MockTransport(receive)) as client:
            monkeypatch.setattr(
                http.Sanic,
                "get_app",
                lambda: SimpleNamespace(ctx=SimpleNamespace(httpx=client)),
            )
            await HTTPNode.execute(
                node_data={
                    "url": "https://example.test/search?q={{ query|quote }}",
                    "formatter": formatter,
                },
                context=ctx,
            )
        await SearchEndNode.execute(node_data={"response": response}, context=ctx)
        assert ctx["$retval"] == {
            "total": 1,
            "items": [{"id": "a", "title": "Example"}],
        }
        assert "response" not in ctx.storage
        assert str(calls[0].url) == "https://example.test/search?q=cats"
        ctx.update({"nested": {"a": 1}, "items": [1]})
        ctx.update({"nested": {"b": 2}, "items": [1, 2]})
        assert ctx["nested"] == {"a": 1, "b": 2}
        assert ctx["items"] == [{"id": "a", "title": "Example"}, 1, 2]
        replacement = ctx.copy()
        replacement.replace("items", [])
        ctx.update(replacement)
        assert ctx["items"] == []

    asyncio.run(run())


@pytest.mark.parametrize("language", ["python", "javascript"])
def test_script_examples(monkeypatch, language):
    monkeypatch.setattr(
        KaloscopeConfig, "get", lambda: SimpleNamespace(script_strict_mode=True)
    )
    description = FlowGraphService.get_node_schemas(node_type="script")["general"][
        0
    ].description
    marker = "Python" if language == "python" else "JavaScript"
    code = re.search(rf"{marker}: `(.+?)`", description, re.S)[1]
    ctx = context({"items": [1, 2]})
    asyncio.run(
        ScriptNode.execute(
            node_id="test",
            node_data={"script": {"language": language, "code": code}},
            context=ctx,
        )
    )
    assert ctx["count"] == 2


@pytest.mark.parametrize(
    "topic", ["context", "templates", "json_indexer", "movie_scraper"]
)
def test_reference_budget(monkeypatch, topic):
    execute = AsyncMock(
        side_effect=AssertionError("Knowledge reads must not execute workflows")
    )
    monkeypatch.setattr(FlowEngine, "execute", execute)

    async def run():
        reference = await query("get_workflow_reference", {"topic": topic})
        schema = await query("list_node_types", {"node_type": "http"})
        assert reference["data"] == get_workflow_reference(topic).model_dump()
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for index, result in enumerate((reference, schema)):
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "Reading node knowledge. " * 200,
                        "tool_calls": [
                            {
                                "id": str(index),
                                "type": "function",
                                "function": {"name": result["name"], "arguments": "{}"},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": str(index),
                        "content": dumps(result).decode(),
                    },
                ]
            )
        compact = history.compact_exchanges(messages, 1, 0)
        retained = [
            loads(m["content"].split("\n", 1)[1])["tools"][0] for m in compact[1:]
        ]
        assert [r["data"] for r in retained] == [reference["data"], schema["data"]]
        assert history.context_size(compact, tools.tool_schemas(USER)) <= 32000 - 4096
        execute.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("topic", ["../config", "missing", None])
def test_invalid_topic(topic):
    result = asyncio.run(
        tools.execute_tool(
            "get_workflow_reference",
            dumps({"topic": topic}).decode(),
            USER,
            conversation_id=1,
            call_id="bad",
        )
    )
    assert not result["ok"]


@pytest.mark.parametrize("name", ["Demo Reader", ""])
def test_auth_output(monkeypatch, name):
    from app.core.flow.nodes.end import auth

    monkeypatch.setattr(auth, "start_config", AsyncMock(return_value={}))
    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    example = try_loads((ROOT / "resp/auth.jsonc").read_text(), with_comments=True)
    example["name"] = name

    async def run():
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        try:
            await Tortoise.generate_schemas()
            await FlowGraph.create(
                id=1, name="Authentication fixture", category="indexer", state="draft"
            )
            await FlowVariable.create(
                graph_id=1, key="$auth", value={"name": "Previous"}
            )
            ctx = context({})
            await AuthEndNode.execute(
                graph_id=1, node_data={"response": dumps(example).decode()}, context=ctx
            )
            cached = await FlowVariable.get(graph_id=1, key="$auth")
            if name:
                assert (
                    ctx["$retval"]
                    == cached.value
                    == {"name": name, "token": example["token"]}
                )
                assert cached.expires == 4600
            else:
                assert ctx["$retval"] == example
                assert cached.value == {"name": "Previous"}
                assert cached.expires is None
        finally:
            await Tortoise.close_connections()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("language", "code", "expected"),
    [
        (
            "python",
            'def execute(node_id, node_data, context): context["items"] = []',
            [1, 2],
        ),
        (
            "python",
            'def execute(node_id, node_data, context): context.replace("items", [])',
            [],
        ),
        (
            "javascript",
            "function execute(node_id, node_data, context) { context.items = []; }",
            [1, 2],
        ),
    ],
)
def test_script_list_merge(monkeypatch, language, code, expected):
    monkeypatch.setattr(
        KaloscopeConfig, "get", lambda: SimpleNamespace(script_strict_mode=True)
    )
    original = context({})
    original.update({"items": [1, 2]})
    branch = original.copy()

    asyncio.run(
        ScriptNode.execute(
            node_id="test",
            node_data={"script": {"language": language, "code": code}},
            context=branch,
        )
    )
    original.update(branch)

    assert original["items"] == expected


def test_reserved_key_lookup():
    values = {"$auth": {"token": "fixture"}}
    assert render("{{ $auth.token }}", values, raw=True) == "fixture"
    with pytest.raises(TemplateSyntaxError):
        render("{{ $auth.token }}", values)
    assert render("{{ auth.token }}", {"auth": values["$auth"]}) == "fixture"


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("{{ '2024'|year(tz=0) }}", "1970"),
        ("{{ '2024-03-15'|year }}", "2024"),
        ("{{ 0|strftime(tz=0) }}", "1970-01-01 00:00:00"),
        ("{{ '2024-03-15T00:00:00+08:00'|strftime(tz=0) }}", "2024-03-14 16:00:00"),
        ("{{ '2024-03-15'|strftime(tz=8) }}", "2024-03-15 00:00:00"),
    ],
)
def test_date_examples(expression, expected):
    assert render(expression, {}) == expected
