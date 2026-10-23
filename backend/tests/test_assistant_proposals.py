"""Unit tests for saving and recovering assistant canvas edits."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from tortoise import Tortoise

from app.core.ai import tools
from app.core.exceptions import (
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.models.assistant import (
    AssistantProposal,
    AssistantSnapshot,
    AssistantToolCall,
    AssistantTurnRequest,
    ConversationCreate,
    ProposalConfirm,
    ProposalDecision,
)
from app.models.flow import (
    FlowDraftRevision,
    FlowGraph,
    GraphCategory,
    GraphDocument,
    GraphState,
    GraphWrite,
)
from app.models.user import User
from app.services.assistant_proposals import AssistantProposalService as Proposals
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowGraphService
from app.utils.json import dumps

MODEL = {"provider_id": 1, "model_id": "fixture"}
SCRIPT = "# keep this script\n" * 200 + "def execute(node_id, node_data, context):\n"


def document(url="https://old.test", x=10):
    return {
        "nodes": [
            {
                "id": "http",
                "position": {"x": x, "y": 20},
                "data": {"$schema": "http", "url": url},
            },
            *[
                {
                    "id": node_id,
                    "position": {"x": 300, "y": 40 * index},
                    "data": {
                        "$schema": "script",
                        "script": {"language": "python", "code": SCRIPT},
                    },
                }
                for index, node_id in enumerate(("first", "second"))
            ],
        ],
        "edges": [
            {
                "id": "link",
                "source": "first",
                "target": "second",
                "sourceHandle": "output",
                "targetHandle": "input",
            }
        ],
    }


@asynccontextmanager
async def database(db_url="sqlite://:memory:"):
    await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        owner, other = [
            await User.create(username=name, password="unused", role="admin")
            for name in ("owner", "other")
        ]
        graph = await FlowGraph.create(
            name="Repair fixture",
            category=GraphCategory.SCHEDULE,
            state=GraphState.PUBLISHED,
            draft=document(),
            definition=document(),
        )
        conversation = await Sessions.create(
            ConversationCreate(model=MODEL), SimpleNamespace(id=owner.id, role="admin")
        )
        yield SimpleNamespace(
            owner=SimpleNamespace(id=owner.id, role="admin"),
            other=SimpleNamespace(id=other.id, role="admin"),
            reader=SimpleNamespace(
                id=owner.id,
                role="user",
                perms=SimpleNamespace(indexer_ids=[graph.id]),
            ),
            graph=graph,
            conversation=conversation["id"],
        )
    finally:
        await Tortoise.close_connections()


async def preview(env, changes, *, snapshot_id=None, version=0, key="call"):
    """Run the preview tool against an owned canvas and return its result data."""
    if snapshot_id is None:
        graph = await FlowGraph.get(id=env.graph.id)
        capture = await Sessions.snapshot(
            env.conversation,
            graph.id,
            version,
            GraphDocument.model_validate(graph.draft),
            "canvas",
            env.owner,
        )
        snapshot_id = capture.id
    arguments = {
        "workflow_id": env.graph.id,
        "draft_version": version,
        "snapshot_id": snapshot_id,
        "changes": changes,
    }
    _, turn = await Sessions.begin(
        env.conversation,
        AssistantTurnRequest(model=MODEL, request_id=key, prompt="Repair it"),
        env.owner,
    )
    await AssistantToolCall.create(
        turn_id=turn.id,
        call_id=key,
        name="preview_workflow_draft",
        arguments=dumps(arguments).decode(),
    )
    result = await tools.execute_tool(
        "preview_workflow_draft",
        dumps(arguments).decode(),
        env.owner,
        conversation_id=env.conversation,
        call_id=key,
        canvas={
            "resource_id": env.graph.id,
            "draft_version": version,
            "snapshot_id": snapshot_id,
        },
        canvas_snapshot_id=snapshot_id,
    )
    await AssistantToolCall.filter(call_id=key).update(
        result={"id": key, "name": "preview_workflow_draft", **result},
        status="complete",
    )
    await Sessions.finish(turn, status="complete", content="Canvas ready")
    assert result["ok"], result
    return result["data"]


def set_url(url="https://new.test"):
    return {"op": "set_field", "node_id": "http", "field_id": "url", "value": url}


async def decision(env, proposal_id, model=ProposalDecision, **values):
    record = await AssistantProposal.get(id=proposal_id)
    if model is ProposalConfirm and "canvas" not in values:
        base = await Sessions.read_snapshot(
            env.conversation, record.base_snapshot_id, env.owner
        )
        values["canvas"] = base.document
    return model(
        workflow_id=record.workflow_id,
        base_version=record.base_version,
        digest=record.digest,
        **values,
    )


def test_canvas_display():
    async def run():
        async with database() as env:
            original = document(x=87)
            capture = await Sessions.snapshot(
                env.conversation,
                env.graph.id,
                0,
                GraphDocument.model_validate(original),
                "canvas",
                env.owner,
            )
            result = await preview(env, [set_url()], snapshot_id=capture.id)

            view = await Proposals.canvas(
                env.conversation, result["proposal_id"], env.owner
            )

            assert view["base_snapshot_id"] == capture.id
            assert view["change_count"] == 1
            assert view["current_version"] == 0
            assert view["canvas"]["nodes"][0]["position"]["x"] == 87
            assert view["canvas"]["nodes"][0]["data"]["url"] == "https://new.test"
            assert view["canvas"]["nodes"][0]["data"]["$schema"].node_type == "http"
            assert (await FlowGraph.get(id=env.graph.id)).draft == document()
            assert (
                await Sessions.read_snapshot(env.conversation, capture.id, env.owner)
            ).document == GraphDocument.model_validate(original).model_dump(mode="json")
            with pytest.raises(NotFoundException):
                await Proposals.canvas(
                    env.conversation, result["proposal_id"], env.other
                )
            with pytest.raises(ForbiddenException):
                await Proposals.canvas(
                    env.conversation, result["proposal_id"], env.reader
                )

            saved = await Proposals.confirm(
                env.conversation,
                result["proposal_id"],
                await decision(
                    env, result["proposal_id"], ProposalConfirm, canvas=original
                ),
                env.owner,
            )
            restored = await Proposals.canvas(
                env.conversation, result["proposal_id"], env.owner
            )
            assert restored["proposal"] == saved["proposal"]
            assert restored["workflow"] == saved["workflow"]
            assert restored["workflow"]["draft_version"] == 1
            assert restored["workflow"]["state"] == "modified"
            assert (await FlowGraph.get(id=env.graph.id)).definition == document()

    asyncio.run(run())


def test_canvas_invalid():
    async def run():
        async with database() as env:
            result = await preview(
                env,
                [
                    {
                        "op": "add_node",
                        "node": {
                            "id": "invalid",
                            "position": {"x": 0, "y": 0},
                            "data": {"$schema": "missing_node"},
                        },
                    }
                ],
            )

            view = await Proposals.canvas(
                env.conversation, result["proposal_id"], env.owner
            )

            assert view["canvas"] is None
            assert not view["validation"]["valid"]
            assert (await FlowGraph.get(id=env.graph.id)).draft_version == 0

    asyncio.run(run())


def test_reference_preview():
    async def run():
        async with database() as env:
            result = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps(
                    {
                        "workflow_id": env.graph.id,
                        "draft_version": 0,
                        "changes": [set_url()],
                    }
                ),
                env.owner,
                conversation_id=env.conversation,
                call_id="reference-only",
            )

            assert result["ok"]
            assert "proposal_snapshot_id" in result["data"]
            assert "proposal_id" not in result["data"]
            assert not await AssistantProposal.exists()
            assert (await FlowGraph.get(id=env.graph.id)).draft == document()

    asyncio.run(run())


def test_missing_canvas_base():
    async def run():
        async with database() as env:
            data = await preview(env, [set_url()])
            body = await decision(env, data["proposal_id"], ProposalConfirm)
            await AssistantProposal.filter(id=data["proposal_id"]).update(
                base_snapshot_id=None
            )

            view = await Proposals.canvas(
                env.conversation, data["proposal_id"], env.owner
            )
            with pytest.raises(KaloscopeException, match="assistant_canvas_changed"):
                await Proposals.confirm(
                    env.conversation, data["proposal_id"], body, env.owner
                )
            history = await Sessions.turns(env.conversation, env.owner)

            assert view["canvas"] is None
            assert (
                history["items"][-1]["tools"][0]["proposal"]["id"]
                == data["proposal_id"]
            )
            assert (await FlowGraph.get(id=env.graph.id)).draft_version == 0
            assert await revisions(env) == 0

    asyncio.run(run())


def test_canvas_chained_edits():
    async def run():
        async with database() as env:
            capture = await Sessions.snapshot(
                env.conversation,
                env.graph.id,
                0,
                GraphDocument.model_validate(document(x=87)),
                "canvas",
                env.owner,
            )
            context = {
                "resource_id": env.graph.id,
                "draft_version": 0,
                "snapshot_id": capture.id,
            }
            reference = {
                "workflow_id": env.graph.id,
                "draft_version": 0,
                "snapshot_id": capture.id,
            }
            first = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps({**reference, "changes": [set_url()]}),
                env.owner,
                conversation_id=env.conversation,
                call_id="first-edit",
                canvas=context,
                canvas_snapshot_id=capture.id,
            )
            assert first["ok"], first
            latest = first["data"]["proposal_snapshot_id"]
            second = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps(
                    {
                        **reference,
                        "snapshot_id": latest,
                        "changes": [
                            {
                                "op": "set_field",
                                "node_id": "http",
                                "field_id": "timeout",
                                "value": 40,
                            }
                        ],
                    }
                ),
                env.owner,
                conversation_id=env.conversation,
                call_id="second-edit",
                canvas=context,
                canvas_snapshot_id=latest,
            )
            assert second["ok"], second

            view = await Proposals.canvas(
                env.conversation, second["data"]["proposal_id"], env.owner
            )
            node = view["canvas"]["nodes"][0]
            assert view["base_snapshot_id"] == capture.id
            assert node["data"]["url"] == "https://new.test"
            assert node["data"]["timeout"] == 40
            assert node["position"]["x"] == 87

            stale = await tools.execute_tool(
                "preview_workflow_draft",
                json.dumps({**reference, "changes": [set_url()]}),
                env.owner,
                conversation_id=env.conversation,
                call_id="stale-edit",
                canvas=context,
                canvas_snapshot_id=latest,
            )
            assert not stale["ok"]
            assert stale["error"]["code"] == "assistant_canvas_changed"
            assert await AssistantProposal.filter(call_id="stale-edit").count() == 0

    asyncio.run(run())


async def save_edit(env, proposal_id, user=None, **values):
    body = await decision(env, proposal_id, ProposalConfirm, **values)
    result = await Proposals.confirm(
        env.conversation, proposal_id, body, user or env.owner
    )
    return result["proposal"]


async def revisions(env):
    return await FlowDraftRevision.filter(graph_id=env.graph.id).count()


def test_save():
    async def run():
        async with database() as env:
            data = await preview(env, [set_url()])
            view = await Proposals.canvas(
                env.conversation, data["proposal_id"], env.owner
            )

            assert view["proposal"]["status"] == "pending"
            assert view["change_count"] == 1
            assert view["canvas"]["nodes"][0]["data"]["url"] == "https://new.test"

            outcome = await save_edit(env, data["proposal_id"])
            repeated = await save_edit(env, data["proposal_id"])
            await env.graph.refresh_from_db()

            assert outcome["status"] == repeated["status"] == "saved"
            assert outcome["saved_version"] == repeated["saved_version"] == 1
            assert env.graph.draft_version == 1
            assert await revisions(env) == 1
            assert env.graph.state == GraphState.MODIFIED
            assert env.graph.definition == document()
            saved = env.graph.draft
            assert not FlowGraphService.compare_documents(
                document(url="https://new.test"), saved
            )
            assert saved["nodes"][1]["data"]["script"]["code"] == SCRIPT
            assert saved["edges"][0]["sourceHandle"] == "output"
            turns = await Sessions.turns(env.conversation, env.owner)
            tool = turns["items"][-1]["tools"][0]
            assert tool["proposal"]["status"] == "saved"
            assert tool["proposal"]["saved_version"] == 1
            recovered = await Proposals.outcome(
                env.conversation, data["proposal_id"], env.reader
            )
            assert recovered["proposal"] == outcome
            assert recovered["workflow"]["draft_version"] == 1
            assert recovered["workflow"]["state"] == GraphState.MODIFIED
            assert recovered["workflow"]["updated_at"] == env.graph.updated_at

    asyncio.run(run())


@pytest.mark.parametrize("stored", [False, True])
def test_concurrent_save(tmp_path, stored):
    url = f"sqlite://{tmp_path / 'edits.sqlite3'}" if stored else "sqlite://:memory:"

    async def run():
        async with database(url) as env:
            data = await preview(env, [set_url()])

            results = await asyncio.gather(
                *(save_edit(env, data["proposal_id"]) for _ in range(4))
            )
            await env.graph.refresh_from_db()

            assert {result["status"] for result in results} == {"saved"}
            assert {result["saved_version"] for result in results} == {1}
            assert env.graph.draft_version == 1
            assert await revisions(env) == 1
            record = await AssistantProposal.get(id=data["proposal_id"])
            assert record.attempts == 1

    asyncio.run(run())


@pytest.mark.parametrize("captured", [False, True])
def test_zero_change(captured):
    async def run():
        async with database() as env:
            snapshot_id = None
            if captured:
                canvas = document()
                canvas["nodes"][0] |= {"selected": True, "measured": {"width": 1}}
                snapshot_id = (
                    await Sessions.snapshot(
                        env.conversation,
                        env.graph.id,
                        0,
                        GraphDocument.model_validate(canvas),
                        "canvas",
                        env.owner,
                    )
                ).id
            data = await preview(env, [], snapshot_id=snapshot_id)
            record = await AssistantProposal.get(id=data["proposal_id"])
            proposed = (await AssistantSnapshot.get(id=record.snapshot_id)).document
            before = await FlowGraph.get(id=env.graph.id)

            result = await Proposals.confirm(
                env.conversation,
                data["proposal_id"],
                await decision(env, data["proposal_id"], ProposalConfirm),
                env.owner,
            )
            recovered = await Proposals.outcome(
                env.conversation, data["proposal_id"], env.owner
            )
            await env.graph.refresh_from_db()

            assert proposed != before.draft
            assert not FlowGraphService.compare_documents(before.draft, proposed)
            assert result["proposal"]["status"] == "unchanged"
            assert result["proposal"]["saved_version"] == 0
            assert result["workflow"] == recovered["workflow"]
            assert result["workflow"] == {
                "id": env.graph.id,
                "state": GraphState.PUBLISHED,
                "draft_version": 0,
                "updated_at": before.updated_at,
            }
            assert env.graph.updated_at == before.updated_at
            assert env.graph.state == GraphState.PUBLISHED
            assert env.graph.draft_version == 0
            assert await revisions(env) == 0

    asyncio.run(run())


def test_advanced_outcome():
    async def run():
        async with database() as env:
            data = await preview(env, [set_url()])
            saved = await Proposals.confirm(
                env.conversation,
                data["proposal_id"],
                await decision(env, data["proposal_id"], ProposalConfirm),
                env.owner,
            )
            await FlowGraphService.save_graph(
                env.graph.id, GraphWrite(**document(x=99), draft_version=1), env.owner
            )

            recovered = await Proposals.outcome(
                env.conversation, data["proposal_id"], env.owner
            )
            cancelled = await Proposals.cancel(
                env.conversation,
                data["proposal_id"],
                await decision(env, data["proposal_id"]),
                env.owner,
            )
            view = await Proposals.canvas(
                env.conversation, data["proposal_id"], env.owner
            )

            assert saved["workflow"]["draft_version"] == 1
            assert recovered["proposal"]["status"] == "saved"
            assert recovered["proposal"]["saved_version"] == 1
            assert recovered["workflow"] is None
            assert cancelled["proposal"]["status"] == "saved"
            assert cancelled["workflow"] is None
            assert view["current_version"] == 2
            assert view["workflow"] is None

    asyncio.run(run())


@pytest.mark.parametrize("unsaved", [False, True])
def test_canvas_no_op(unsaved):
    async def run():
        async with database() as env:
            canvas = document(
                url="https://manual.test" if unsaved else "https://old.test"
            )
            capture = await Sessions.snapshot(
                env.conversation,
                env.graph.id,
                0,
                GraphDocument.model_validate(canvas),
                "canvas",
                env.owner,
            )
            no_op = await preview(env, [], snapshot_id=capture.id, key="no-op")
            edited = await preview(
                env, [set_url("https://new.test")], snapshot_id=capture.id, key="edit"
            )

            view = await Proposals.canvas(
                env.conversation, no_op["proposal_id"], env.owner
            )
            history = await Sessions.turns(env.conversation, env.owner)
            tools_by_id = {
                tool["id"]: tool for turn in history["items"] for tool in turn["tools"]
            }

            assert no_op["canvas_change_count"] == view["change_count"] == 0
            assert no_op["change_count"] == int(unsaved)
            assert edited["canvas_change_count"] == 1
            assert tools_by_id["no-op"]["result"]["data"]["canvas_change_count"] == 0
            assert tools_by_id["no-op"]["proposal"]["status"] == "pending"
            assert (await FlowGraph.get(id=env.graph.id)).draft_version == 0

    asyncio.run(run())


def test_conflict():
    async def run():
        async with database() as env:
            data = await preview(env, [set_url()])
            external = document(x=99)
            await FlowGraphService.save_graph(
                env.graph.id, GraphWrite(**external, draft_version=0), env.owner
            )

            conflict = await save_edit(env, data["proposal_id"])
            repeated = await save_edit(env, data["proposal_id"])
            view = await Proposals.canvas(
                env.conversation, data["proposal_id"], env.owner
            )
            await env.graph.refresh_from_db()

            assert conflict == repeated
            assert conflict["status"] == "conflict"
            assert conflict["error"] == "flow_draft_conflict"
            assert view["current_version"] == 1
            assert view["change_count"] == 1
            assert view["canvas"]["nodes"][0]["position"]["x"] == 10
            assert env.graph.draft_version == 1
            assert not FlowGraphService.compare_documents(external, env.graph.draft)
            assert await revisions(env) == 1
            assert await AssistantProposal.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("case", ["other", "reader", "changed", "conversation"])
def test_authorization(case):
    async def run():
        async with database() as env:
            data = await preview(env, [set_url()])
            proposal_id = data["proposal_id"]
            body = await decision(env, proposal_id, ProposalConfirm)
            conversation = env.conversation
            user = env.owner
            expected = NotFoundException
            if case == "other":
                user = env.other
            elif case == "reader":
                user, expected = env.reader, ForbiddenException
            elif case == "changed":
                body = body.model_copy(update={"digest": "0" * 64})
                expected = KaloscopeException
            else:
                conversation = (
                    await Sessions.create(ConversationCreate(model=MODEL), env.owner)
                )["id"]

            with pytest.raises(expected) as error:
                await Proposals.confirm(conversation, proposal_id, body, user)

            if case == "changed":
                assert error.value.message == "assistant_proposal_changed"
            if case in ("other", "conversation"):
                with pytest.raises(NotFoundException):
                    await Proposals.canvas(conversation, proposal_id, user)
            record = await AssistantProposal.get(id=proposal_id)
            await env.graph.refresh_from_db()
            assert record.status == "pending"
            assert record.attempts == 0
            assert env.graph.draft_version == 0
            assert not [
                name
                for name in tools.available_tools(env.owner)
                if any(word in name for word in ("confirm", "save", "cancel"))
            ]

    asyncio.run(run())


def test_not_editable_and_invalid():
    async def run():
        async with database() as env:
            invalid = await preview(
                env,
                [
                    {
                        "op": "add_edge",
                        "edge": {
                            "id": "bad",
                            "source": "http",
                            "target": "missing",
                            "sourceHandle": "output",
                            "targetHandle": "input",
                        },
                    }
                ],
                key="invalid",
            )
            view = await Proposals.canvas(
                env.conversation, invalid["proposal_id"], env.owner
            )
            assert view["canvas"] is None
            assert (await save_edit(env, invalid["proposal_id"]))[
                "error"
            ] == "flow_graph_invalid"

            data = await preview(env, [set_url()], key="locked")
            env.graph.editable = False
            await env.graph.save()

            outcome = await save_edit(env, data["proposal_id"])
            await env.graph.refresh_from_db()

            assert outcome["status"] == "failed"
            assert outcome["error"] == "flow_not_editable"
            assert env.graph.draft_version == 0

    asyncio.run(run())


def test_incomplete_draft():
    async def run():
        async with database() as env:
            data = await preview(env, [set_url("")])
            view = await Proposals.canvas(
                env.conversation, data["proposal_id"], env.owner
            )

            assert view["validation"]["valid"] is False
            assert view["canvas"] is not None
            assert (await save_edit(env, data["proposal_id"]))["status"] == "saved"

    asyncio.run(run())


def test_revert():
    async def run():
        async with database() as env:
            data = await preview(env, [set_url()])
            body = await decision(env, data["proposal_id"])

            cancelled = await Proposals.cancel(
                env.conversation, data["proposal_id"], body, env.owner
            )
            after = await save_edit(env, data["proposal_id"])
            await env.graph.refresh_from_db()

            assert cancelled["proposal"]["status"] == after["status"] == "cancelled"
            assert cancelled["workflow"] is None
            assert env.graph.draft_version == 0

            raced = await preview(env, [set_url()], key="raced")
            body = await decision(env, raced["proposal_id"])
            results = await asyncio.gather(
                save_edit(env, raced["proposal_id"]),
                Proposals.cancel(
                    env.conversation, raced["proposal_id"], body, env.owner
                ),
            )
            await env.graph.refresh_from_db()
            assert results[0]["status"] == results[1]["proposal"]["status"]
            assert env.graph.draft_version == (
                1 if results[0]["status"] == "saved" else 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("case", ["deleted", "revoked"])
def test_outcome_recovery(case):
    async def run():
        async with database() as env:
            pending = await preview(env, [set_url()], key="pending")
            saved = await preview(env, [set_url("https://saved.test")], key="saved")
            await save_edit(env, saved["proposal_id"])
            user = env.owner
            if case == "deleted":
                await FlowGraph.filter(id=env.graph.id).delete()
            else:
                user = SimpleNamespace(
                    id=env.owner.id, role="user", perms=SimpleNamespace(indexer_ids=[])
                )

            recovered = await Proposals.outcome(
                env.conversation, saved["proposal_id"], user
            )
            unresolved = await Proposals.outcome(
                env.conversation, pending["proposal_id"], user
            )
            cancelled = await Proposals.cancel(
                env.conversation,
                pending["proposal_id"],
                await decision(env, pending["proposal_id"]),
                user,
            )

            assert recovered["proposal"]["status"] == "saved"
            assert recovered["proposal"]["saved_version"] == 1
            assert recovered["workflow"] is None
            assert unresolved["proposal"]["status"] == "pending"
            assert cancelled["proposal"]["status"] == "cancelled"
            assert (
                await Proposals.outcome(env.conversation, pending["proposal_id"], user)
            )["proposal"]["status"] == "cancelled"
            with pytest.raises(
                NotFoundException if case == "deleted" else ForbiddenException
            ):
                await Proposals.canvas(env.conversation, saved["proposal_id"], user)
            with pytest.raises(NotFoundException):
                await Proposals.outcome(
                    env.conversation, saved["proposal_id"], env.other
                )
            other = (await Sessions.create(ConversationCreate(model=MODEL), env.owner))[
                "id"
            ]
            with pytest.raises(NotFoundException):
                await Proposals.outcome(other, saved["proposal_id"], env.owner)

    asyncio.run(run())


def test_cancel_during_save(monkeypatch):
    save_graph = FlowGraphService.save_graph

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(cls, id, body, user):
            started.set()
            await release.wait()
            return await save_graph(id, body, user)

        async with database() as env:
            data = await preview(env, [set_url()])
            body = await decision(env, data["proposal_id"])
            monkeypatch.setattr(FlowGraphService, "save_graph", classmethod(delayed))

            saving = asyncio.create_task(save_edit(env, data["proposal_id"]))
            await started.wait()
            cancelling = asyncio.create_task(
                Proposals.cancel(env.conversation, data["proposal_id"], body, env.owner)
            )
            for _ in range(10):
                await asyncio.sleep(0)
            blocked = not cancelling.done()
            release.set()
            saved, cancelled = await asyncio.gather(saving, cancelling)
            await env.graph.refresh_from_db()

            assert blocked
            assert saved["status"] == cancelled["proposal"]["status"] == "saved"
            assert cancelled["workflow"]["draft_version"] == 1
            assert env.graph.draft_version == 1
            assert await revisions(env) == 1

    asyncio.run(run())


def test_captured_canvas():
    async def run():
        async with database() as env:
            canvas = document(x=50)
            snapshot = await Sessions.snapshot(
                env.conversation,
                env.graph.id,
                0,
                GraphDocument.model_validate(canvas),
                "canvas",
                env.owner,
            )
            data = await preview(env, [set_url()], snapshot_id=snapshot.id)
            view = await Proposals.canvas(
                env.conversation, data["proposal_id"], env.owner
            )

            assert view["base_snapshot_id"] == snapshot.id
            assert view["canvas"]["nodes"][0]["position"]["x"] == 50
            assert view["change_count"] == 1
            newer = GraphDocument.model_validate(document(x=70))
            with pytest.raises(KaloscopeException, match="assistant_canvas_changed"):
                await save_edit(env, data["proposal_id"], canvas=newer)
            record = await AssistantProposal.get(id=data["proposal_id"])
            assert record.status == "pending"
            assert record.attempts == 0

            saved = await save_edit(env, data["proposal_id"], canvas=canvas)
            await env.graph.refresh_from_db()

            assert saved["status"] == "saved"
            assert env.graph.draft["nodes"][0]["position"]["x"] == 50
            assert env.graph.draft["nodes"][0]["data"]["url"] == "https://new.test"

    asyncio.run(run())


def test_interrupted_save(monkeypatch):
    save_graph = FlowGraphService.save_graph

    async def interrupted(cls, id, body, user):
        await save_graph(id, body, user)
        raise RuntimeError("connection lost after the write")

    async def run():
        async with database() as env:
            data = await preview(env, [set_url()])
            monkeypatch.setattr(
                FlowGraphService, "save_graph", classmethod(interrupted)
            )

            with pytest.raises(RuntimeError):
                await save_edit(env, data["proposal_id"])

            await env.graph.refresh_from_db()
            record = await AssistantProposal.get(id=data["proposal_id"])
            assert env.graph.draft_version == 0
            assert await revisions(env) == 0
            assert record.status == "pending"
            assert record.error == "internal_server_error"
            monkeypatch.undo()
            outcome = await save_edit(env, data["proposal_id"])
            await env.graph.refresh_from_db()
            assert outcome["status"] == "saved"
            assert env.graph.draft_version == 1
            assert await revisions(env) == 1

    asyncio.run(run())


def test_missing_sources():
    async def run():
        async with database() as env:
            data = await preview(env, [set_url()])
            body = await decision(env, data["proposal_id"], ProposalConfirm)
            await FlowGraph.filter(id=env.graph.id).delete()

            with pytest.raises(NotFoundException):
                await Proposals.canvas(env.conversation, data["proposal_id"], env.owner)
            outcome = await Proposals.confirm(
                env.conversation, data["proposal_id"], body, env.owner
            )

            assert outcome["proposal"]["status"] == "failed"
            assert outcome["proposal"]["error"] == "not_found"
            assert outcome["workflow"] is None
            await Sessions.delete(env.conversation, env.owner)
            assert not await AssistantProposal.exists()

    asyncio.run(run())
