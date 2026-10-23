"""Unit tests for draft structure, registered fields, and workflow connections."""

import asyncio
from copy import deepcopy
from types import MethodType
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.flow.validation import compress_document, validate_document
from app.models.flow import GraphCategory, GraphDocument, GraphWrite
from app.services.ai import AIProviderService


def node(id="one", kind="http", **data):
    return {"id": id, "data": {"$schema": kind, **data}}


def edge(source, target, source_handle="output", target_handle="input"):
    return {
        "id": f"{source}:{source_handle}-{target}:{target_handle}",
        "source": source,
        "target": target,
        "sourceHandle": source_handle,
        "targetHandle": target_handle,
    }


def check(nodes, edges=None, *, complete=True, category=GraphCategory.SCHEDULE):
    return asyncio.run(
        validate_document(
            GraphDocument(nodes=nodes, edges=edges or []), category, complete=complete
        )
    )


@pytest.mark.parametrize(
    "body",
    [
        {"nodes": {}, "edges": []},
        {"nodes": [{"id": "", "data": {}}], "edges": []},
        {
            "nodes": [{"id": "x", "data": {}, "position": {"x": float("inf"), "y": 0}}],
            "edges": [],
        },
        {"nodes": [node()] * 501, "edges": []},
        {"nodes": [], "edges": [{"id": "x", "source": "one", "target": "two"}]},
        {"nodes": [], "edges": [], "definition": {}},
    ],
)
def test_envelope(body):
    with pytest.raises(ValidationError):
        GraphDocument.model_validate(body)


@pytest.mark.parametrize("version", [None, -1, True, "1"])
def test_version(version):
    with pytest.raises(ValidationError):
        GraphWrite(nodes=[], edges=[], draft_version=version)


def test_schema_authority():
    original = node(url="https://example.test")
    original["data"]["$schema"] = {"node_type": "http", "fields": [], "handles": []}
    saved = deepcopy(original)
    document = GraphDocument.model_validate({"nodes": [original], "edges": []})

    compressed = compress_document(document)
    compressed["nodes"][0]["data"]["url"] = "changed"

    assert original == saved
    assert document.nodes[0].data["url"] == "https://example.test"
    assert compressed["nodes"][0]["data"]["$schema"] == "http"
    assert check([original]).valid
    original["data"]["url"] = ""
    assert check([original]).issues[0].code == "required"


def test_incomplete_draft():
    assert check([node()], complete=False).valid
    result = check([node()])
    assert not result.valid
    assert result.issues[0].model_dump() == {
        "code": "required",
        "severity": "error",
        "node_id": "one",
        "field_id": "url",
        "edge_id": None,
    }


def test_editor_metadata():
    assert check([node(url="https://example.test", divider_7=None)]).valid
    assert not check([node(url="https://example.test", divider_7="value")]).valid


def test_connection_warning():
    result = check(
        [node(id, url="https://example.test") for id in ("one", "two", "three")],
        [edge("one", "three"), edge("two", "three")],
    )
    assert result.valid
    assert len(result.issues) == 1
    assert result.issues[0].code == "connection_limit"
    assert result.issues[0].severity == "warning"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        ({"timeout": "30"}, "field_type"),
        ({"timeout": True}, "field_type"),
        ({"timeout": 0}, "field_range"),
        ({"timeout": 1.5}, "field_range"),
        ({"timeout": float("nan")}, "field_type"),
        ({"method": "INVALID"}, "field_option"),
        ({"url": []}, "field_type"),
        ({"url": "x" * 1025}, "field_length"),
        ({"url": "{{ invalid"}, "template_syntax"),
        ({"headers": [{"key": "x"}]}, "field_type"),
        ({"$output": "output"}, "unknown_field"),
    ],
)
def test_fields(data, code):
    result = check([node(**{"url": "https://example.test", **data})])
    assert code in {issue.code for issue in result.issues}


@pytest.mark.parametrize("value", [10**400, -(10**400)])
@pytest.mark.parametrize("complete", [True, False])
def test_large_number(value, complete):
    result = check([node(url="https://example.test", timeout=value)], complete=complete)

    assert not result.valid
    assert result.issues[0].code == "field_range"
    assert result.issues[0].field_id == "timeout"


@pytest.mark.parametrize(
    ("kind", "data", "code"),
    [
        ("unknown", {}, "unknown_node"),
        ("search_start", {}, "node_category"),
        ("script", {"script": {"language": "shell", "code": ""}}, "field_type"),
        ("script", {"script": {"language": "python", "code": ""}}, "required"),
        ("ai", {"model": {"provider_id": True, "model_id": "x"}}, "field_type"),
        ("ai", {"model": {"provider_id": 1, "model_id": ""}}, "required"),
    ],
)
def test_node_fields(kind, data, code):
    assert code in {issue.code for issue in check([node(kind=kind, **data)]).issues}


def test_ai(monkeypatch):
    resolve = AsyncMock()
    monkeypatch.setattr(AIProviderService, "resolve_model", resolve)
    monkeypatch.setattr(
        AIProviderService, "complete", AsyncMock(side_effect=AssertionError)
    )
    result = check(
        [
            node(
                kind="ai",
                model={"provider_id": 1, "model_id": "test"},
                prompt="Summarize {{ title }}",
            )
        ]
    )
    assert result.valid
    resolve.assert_awaited_once()


@pytest.mark.parametrize(
    ("nodes", "edges", "code"),
    [
        ([node(), node()], [], "duplicate_node"),
        ([node()], [edge("one", "missing")], "missing_endpoint"),
        ([node()], [edge("one", "one")], "self_connection"),
        ([node("one"), node("two")], [edge("one", "two", "input")], "invalid_handle"),
        (
            [node("one"), node("two")],
            [edge("one", "two"), edge("two", "one")],
            "uncontrolled_cycle",
        ),
        (
            [node("one"), node("two")],
            [edge("one", "two"), edge("one", "two")],
            "duplicate_edge",
        ),
        (
            [node("one"), node("two"), node("three")],
            [edge("one", "three"), edge("two", "three")],
            "connection_limit",
        ),
    ],
)
def test_connections(nodes, edges, code):
    assert code in {issue.code for issue in check(nodes, edges, complete=False).issues}


def test_loop():
    nodes = [
        node("loop", "loop", expression="{{ items }}", varname="item"),
        node("body", url="https://example.test"),
    ]
    edges = [
        edge("loop", "body", "loop_start"),
        edge("body", "loop", target_handle="loop_continue"),
    ]
    assert check(nodes, edges).valid
    edges[-1]["targetHandle"] = "input"
    assert "invalid_loop" in {issue.code for issue in check(nodes, edges).issues}


def test_nested_loop():
    nodes = [
        node("outer", "loop", expression="{{ items }}", varname="item"),
        node("inner", "loop", expression="{{ item }}", varname="part"),
        node("body", url="https://example.test"),
    ]
    edges = [
        edge("outer", "inner", "loop_start"),
        edge("inner", "outer", target_handle="loop_continue"),
        edge("inner", "body", "loop_start"),
        edge("body", "inner", target_handle="loop_continue"),
    ]
    assert check(nodes, edges).valid


def test_tags():
    result = check(
        [node("start", "search_start"), node("end", "auth_end")],
        [edge("start", "end")],
        complete=False,
        category=GraphCategory.INDEXER,
    )
    assert "conflicting_tags" in {issue.code for issue in result.issues}


def test_loop_tags():
    result = check(
        [node("start", "search_start"), node("loop", "loop"), node("end", "auth_end")],
        [edge("start", "loop"), edge("loop", "end", "loop_start")],
        complete=False,
        category=GraphCategory.INDEXER,
    )
    assert "conflicting_tags" in {issue.code for issue in result.issues}


def test_crossed_loops():
    result = check(
        [node("one", "loop"), node("two", "loop")],
        [edge("one", "two", "loop_start", "loop_continue")],
        complete=False,
    )
    assert "invalid_loop" in {issue.code for issue in result.issues}


def test_cyclic_nesting():
    result = check(
        [node("one", "loop"), node("two", "loop")],
        [edge("one", "two", "loop_start"), edge("two", "one", "loop_start")],
        complete=False,
    )
    assert "invalid_loop" in {issue.code for issue in result.issues}


def test_empty():
    assert check([], complete=False).valid
    assert check([]).issues[0].code == "empty_graph"


@pytest.mark.parametrize(
    ("nodes", "complete", "valid", "status"),
    [
        ([], True, False, "empty"),
        ([], False, True, "empty"),
        ([node()], True, False, "invalid"),
        ([node()], False, True, "not_checked"),
        ([node(kind="condition", expression="true")], True, True, "incomplete"),
    ],
)
def test_readiness_status(nodes, complete, valid, status):
    result = check(nodes, complete=complete)

    assert result.valid == valid
    assert result.readiness.status == status
    assert result.scope == "structure_and_fields"
    assert result.executed is False
    if status == "incomplete":
        assert result.issues == []
        assert result.readiness.warnings[0].code == "missing_entry"
        assert result.readiness.warnings[0].severity == "warning"


@pytest.mark.parametrize(
    ("category", "start", "end", "fields"),
    [
        (GraphCategory.INDEXER, "search_start", "search_end", {}),
        (GraphCategory.INGEST, "ingest_start", "movie", {}),
        (GraphCategory.SCHEDULE, "schedule_start", "schedule_end", {}),
        (
            GraphCategory.SCHEDULE,
            "schedule_start",
            "http",
            {"url": "https://example.test"},
        ),
    ],
)
def test_category_readiness(category, start, end, fields):
    nodes = [node("start", start), node("end", end, **fields)]
    disconnected = check(nodes, category=category)
    connected = check(nodes, [edge("start", "end")], category=category)

    assert disconnected.valid
    assert disconnected.readiness.status == "incomplete"
    assert disconnected.readiness.warnings[0].node_id == "start"
    assert connected.valid
    assert connected.readiness.status == "ready"
    assert connected.readiness.warning_count == 0


def test_each_entry():
    result = check(
        [
            node("search", "search_start"),
            node("end", "search_end"),
            node("auth", "auth_start"),
        ],
        [edge("search", "end")],
        category=GraphCategory.INDEXER,
    )

    assert result.valid
    assert result.readiness.status == "incomplete"
    assert [(issue.code, issue.node_id) for issue in result.readiness.warnings] == [
        ("no_reachable_output", "auth")
    ]


def test_unreachable_output():
    result = check(
        [
            node("start", "search_start"),
            node("end", "search_end"),
            node("unused", "search_end"),
        ],
        [edge("start", "end")],
        category=GraphCategory.INDEXER,
    )

    assert result.readiness.status == "incomplete"
    assert [(issue.code, issue.node_id) for issue in result.readiness.warnings] == [
        ("unreachable_output", "unused")
    ]


def test_static_paths(monkeypatch):
    from app.core.flow.nodes.base import Node

    execute = AsyncMock(side_effect=AssertionError("Validation must not execute nodes"))
    for executor in Node.executors.values():
        assert isinstance(executor, MethodType)
        monkeypatch.setattr(executor.__self__, "execute", execute)
    result = check(
        [
            node("start", "schedule_start"),
            node("condition", "condition", expression="{{ missing_variable }}"),
            node("loop", "loop", expression="{{ items }}", varname="item"),
            node(
                "script",
                "script",
                script={"language": "python", "code": "raise RuntimeError()"},
            ),
        ],
        [
            edge("start", "condition"),
            edge("condition", "loop", "is_true"),
            edge("loop", "script", "loop_start"),
            edge("script", "loop", target_handle="loop_continue"),
        ],
    )

    assert result.valid
    assert result.readiness.status == "ready"
    assert result.executed is False
    execute.assert_not_awaited()


def test_readiness_bounds():
    result = check(
        [node(str(index), "search_end") for index in range(50)],
        category=GraphCategory.INDEXER,
    )

    assert result.readiness.warning_count == 51
    assert len(result.readiness.warnings) == 30
    assert result.readiness.omitted_warnings == 21
