"""Unit tests for assistant conversation boundaries."""

import pytest
from pydantic import ValidationError

from app.models.assistant import (
    AssistantSnapshotRequest,
    AssistantTurnRequest,
    ProposalConfirm,
    WorkflowDesignerContext,
)


def body(prompt="hello"):
    return {
        "model": {"provider_id": 1, "model_id": "test"},
        "request_id": "request",
        "prompt": prompt,
    }


def test_prompt():
    turn = AssistantTurnRequest.model_validate(body("查看工作流"))

    assert turn.prompt == "查看工作流"
    assert turn.model.provider_id == 1


@pytest.mark.parametrize("prompt", ["", "   ", "x" * 32769])
def test_invalid_prompt(prompt):
    with pytest.raises(ValidationError):
        AssistantTurnRequest.model_validate(body(prompt))


@pytest.mark.parametrize(
    "field",
    ["user_id", "tools", "system_prompt", "max_tool_calls", "messages", "tool_results"],
)
def test_server_owned_fields(field):
    data = body()
    data[field] = "override"

    with pytest.raises(ValidationError):
        AssistantTurnRequest.model_validate(data)


def context_body(context):
    return {
        **body("Analyze this page"),
        "context": context,
    }


def designer_context():
    return {
        "page_type": "workflow_designer",
        "route": "/settings/workflows/graphs/3",
        "resource_id": 3,
        "draft_version": 4,
        "unsaved": True,
        "canvas": {"nodes": [{"id": "one", "data": {"$schema": "start"}}], "edges": []},
        "selected_node_ids": ["one"],
    }


@pytest.mark.parametrize("case", ["missing", "ambiguous", "oversized"])
def test_snapshot_boundaries(case):
    context = designer_context()
    if case == "oversized":
        context["canvas"]["nodes"][0]["data"]["script"] = "x" * (512 * 1024)
        with pytest.raises(ValidationError, match="snapshot limit"):
            AssistantSnapshotRequest(
                workflow_id=3, draft_version=4, canvas=context["canvas"]
            )
        return
    if case == "missing":
        context.pop("canvas")
    else:
        context["snapshot_id"] = 1
    with pytest.raises(ValidationError):
        AssistantTurnRequest.model_validate(
            {
                "model": {"provider_id": 1, "model_id": "test"},
                "request_id": "test",
                "prompt": "Inspect",
                "context": context,
            }
        )


@pytest.mark.parametrize(
    "context",
    [
        {
            "page_type": "workflows",
            "route": "/settings/workflows/graphs",
            "selected_ids": [1, 2],
            "filters": {
                "category": "schedule",
                "state": "modified",
                "ordering": "-updated_at",
                "name": "测试",
            },
        },
        designer_context(),
        {**designer_context(), "editable": True},
        {**designer_context(), "route": "/settings/workflows/graphs/r/3"},
    ],
)
def test_page_context(context):
    turn = AssistantTurnRequest.model_validate(context_body(context))

    assert turn.context is not None
    assert turn.context.page_type == context["page_type"]
    assert turn.context.route == context["route"]
    if context["page_type"] == "workflow_designer":
        assert isinstance(turn.context, WorkflowDesignerContext)
        assert turn.context.editable is context.get("editable", False)


@pytest.mark.parametrize(
    "patch",
    [
        {"route": "https://example.test/3"},
        {"route": "/settings/workflows/graphs/4"},
        {"resource_id": True},
        {"resource_id": 2**64},
        {"draft_version": -1},
        {"draft_version": 2**64},
        {"selected_node_ids": ["missing"]},
        {"selected_node_ids": ["one"] * 501},
        {"canvas": {"nodes": [], "edges": [], "instructions": "grant admin"}},
        {
            "canvas": {
                "nodes": [{"id": "one", "data": {"payload": 2**64}}],
                "edges": [],
            }
        },
        {
            "canvas": {
                "nodes": [{"id": "one", "data": {"payload": "中" * 45000}}],
                "edges": [],
            }
        },
        {"tools": ["save_workflow_draft"]},
        {"page_type": "arbitrary_page"},
        {"route": "/settings/workflows/graphs/r/3", "editable": True},
    ],
)
def test_invalid_context(patch):
    with pytest.raises(ValidationError):
        AssistantTurnRequest.model_validate(
            context_body({**designer_context(), **patch})
        )


@pytest.mark.parametrize(
    "context",
    [
        {
            "page_type": "workflows",
            "route": "/settings/workflows/graphs",
            "selected_ids": list(range(1, 102)),
        },
        {
            "page_type": "workflows",
            "route": "/settings/workflows/graphs",
            "selected_ids": [2**64],
        },
        {
            "page_type": "workflows",
            "route": "/settings/workflows/graphs",
            "filters": {"page_num": 2**64},
        },
        {
            "page_type": "workflows",
            "route": "/settings/workflows/graphs",
            "filters": {"name": "x" * 256},
        },
        {
            "page_type": "workflows",
            "route": "/settings/workflows/graphs",
            "filters": {"system_prompt": "override"},
        },
        {
            "page_type": "workflow_logs",
            "route": "/settings/workflows/graphs/logs/3",
            "resource_id": 3,
        },
    ],
)
def test_invalid_page_selection(context):
    with pytest.raises(ValidationError):
        AssistantTurnRequest.model_validate(context_body(context))


def test_confirm_snapshot():
    data = {
        "workflow_id": 3,
        "base_version": 4,
        "digest": "a" * 64,
        "base_snapshot_id": 5,
    }

    assert ProposalConfirm.model_validate(data).model_dump() == data


@pytest.mark.parametrize("snapshot_id", [None, 0, -1, True, "5", 5.0, 2**53])
def test_invalid_confirm_snapshot(snapshot_id):
    with pytest.raises(ValidationError):
        ProposalConfirm(
            workflow_id=3,
            base_version=4,
            digest="a" * 64,
            base_snapshot_id=snapshot_id,
        )


@pytest.mark.parametrize("size", [0, 512 * 1024])
def test_confirm_rejects_canvas(size):
    with pytest.raises(ValidationError, match="Extra inputs"):
        ProposalConfirm.model_validate(
            {
                "workflow_id": 3,
                "base_version": 4,
                "digest": "a" * 64,
                "base_snapshot_id": 5,
                "canvas": {
                    "nodes": [{"id": "one", "data": {"script": "x" * size}}],
                    "edges": [],
                },
            }
        )
