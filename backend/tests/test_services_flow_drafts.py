"""Unit tests for workflow draft permissions, version conflicts, and recovery."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tortoise import Tortoise

from app.core.exceptions import (
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.models.flow import (
    FlowDraftRevision,
    FlowGraph,
    GraphCategory,
    GraphDocument,
    GraphDraftCreate,
    GraphState,
    GraphWrite,
)
from app.services import flow
from app.services.flow import FlowGraphService

ADMIN = SimpleNamespace(role="admin")
READER = SimpleNamespace(role="user")


@asynccontextmanager
async def database(db_url="sqlite://:memory:"):
    await Tortoise.init(db_url=db_url, modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        yield await FlowGraph.create(
            name="Test",
            category=GraphCategory.SCHEDULE,
            state=GraphState.DRAFT,
            draft={"nodes": [], "edges": []},
        )
    finally:
        await Tortoise.close_connections()


def draft(version=0, url="https://example.test"):
    return GraphWrite(
        nodes=[{"id": "http", "data": {"$schema": "http", "url": url}}],
        edges=[],
        draft_version=version,
    )


def test_save_and_publish():
    async def run():
        async with database() as graph:
            saved = await FlowGraphService.save_graph(graph.id, draft(url=""), ADMIN)
            assert saved.draft_version == 1
            assert saved.definition is None
            assert saved.state == GraphState.DRAFT
            with pytest.raises(KaloscopeException, match="flow_graph_invalid"):
                await FlowGraphService.publish_graph(graph.id, draft(1, ""), ADMIN)
            published = await FlowGraphService.publish_graph(graph.id, draft(1), ADMIN)
            saved = await FlowGraphService.save_graph(
                graph.id, draft(2, "https://new.test"), ADMIN
            )
            assert saved.draft_version == 3
            assert saved.state == GraphState.MODIFIED
            assert saved.definition == published.definition
            assert (
                await FlowGraphService.save_graph(
                    graph.id, draft(3, "https://new.test"), ADMIN
                )
            ).draft_version == 3
            restored = await FlowGraphService.restore_draft(graph.id, 2, 3, ADMIN)
            assert restored.draft_version == 4
            assert restored.draft == published.definition
            assert restored.definition == published.definition
            assert restored.state == GraphState.PUBLISHED

    asyncio.run(run())


def test_concurrent_writes():
    async def run():
        async with database() as graph:
            results = await asyncio.gather(
                FlowGraphService.save_graph(
                    graph.id, draft(url="https://one.test"), ADMIN
                ),
                FlowGraphService.save_graph(
                    graph.id, draft(url="https://two.test"), ADMIN
                ),
                return_exceptions=True,
            )
            failures = [result for result in results if isinstance(result, Exception)]
            assert len(failures) == 1
            assert isinstance(failures[0], KaloscopeException)
            assert failures[0].status_code == 409
            assert failures[0].message == "flow_draft_conflict"
            await graph.refresh_from_db()
            assert graph.draft_version == 1
            assert await FlowDraftRevision.filter(graph_id=graph.id).count() == 1
            with pytest.raises(KaloscopeException, match="flow_draft_conflict"):
                await FlowGraphService.publish_graph(graph.id, draft(), ADMIN)

    asyncio.run(run())


@pytest.mark.parametrize("method", ["save_graph", "publish_graph"])
def test_other_connection(tmp_path, monkeypatch, method):
    path = tmp_path / "drafts.sqlite3"
    get_accessible = FlowGraphService.get_accessible

    async def concurrent_write(cls, id, user):
        graph = await get_accessible(id, user)
        with sqlite3.connect(path, timeout=1) as connection:
            connection.execute(
                "UPDATE flow_graph SET draft_version = draft_version + 1 WHERE id = ?",
                (id,),
            )
        return graph

    monkeypatch.setattr(
        FlowGraphService, "get_accessible", classmethod(concurrent_write)
    )

    async def run():
        async with database(f"sqlite://{path}") as graph:
            with pytest.raises(KaloscopeException) as error:
                await getattr(FlowGraphService, method)(graph.id, draft(), ADMIN)

            assert error.value.status_code == 409
            assert error.value.message == "flow_draft_conflict"
            await graph.refresh_from_db()
            assert graph.draft_version == 1
            assert graph.draft == {"nodes": [], "edges": []}
            assert not await FlowDraftRevision.all().exists()

    asyncio.run(run())


def test_preview():
    async def run():
        async with database() as graph:
            preview = await FlowGraphService.preview_draft(
                graph.id, draft(url=""), ADMIN
            )
            assert not preview.validation.valid
            assert preview.changes[0].kind == "node"
            assert preview.changes[0].before is None
            assert preview.changes[0].after["data"]["url"] == ""
            assert preview.editor_path == f"/settings/workflows/graphs/{graph.id}"
            assert set(preview.model_dump()) == {
                "workflow_id",
                "draft_version",
                "changes",
                "validation",
                "editor_path",
            }
            await graph.refresh_from_db()
            assert graph.draft_version == 0
            assert await FlowDraftRevision.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize("concurrent_write", [False, True])
def test_preview_baseline(monkeypatch, concurrent_write):
    validate_document = flow.validate_document

    async def run():
        async with database() as graph:
            graph.draft = draft().model_dump(exclude={"draft_version"})
            await graph.save()
            baseline = GraphDocument(
                **draft(url="https://manual.test").model_dump(exclude={"draft_version"})
            )
            document = baseline.model_dump()
            document["nodes"].append(
                {"id": "added", "data": {"$schema": "http", "url": ""}}
            )
            proposed = GraphWrite(**document, draft_version=0)

            async def validate(document, category):
                result = await validate_document(document, category)
                if concurrent_write:
                    await FlowGraph.filter(id=graph.id).update(
                        draft=baseline.model_dump(), draft_version=1
                    )
                return result

            validation = AsyncMock(side_effect=validate)
            monkeypatch.setattr(flow, "validate_document", validation)

            preview, count = await FlowGraphService.preview_draft_with_baseline(
                graph.id, proposed, ADMIN, baseline
            )

            assert count == 1
            assert preview.draft_version == 0
            assert {change.id for change in preview.changes} == {"http", "added"}
            assert not preview.validation.valid
            validation.assert_awaited_once_with(proposed, graph.category)
            assert await FlowDraftRevision.all().count() == 0
            if concurrent_write:
                with pytest.raises(KaloscopeException, match="flow_draft_conflict"):
                    await FlowGraphService.preview_draft_with_baseline(
                        graph.id, proposed, ADMIN, baseline
                    )

    asyncio.run(run())


def test_preview_paths():
    async def run():
        async with database() as graph:
            original = draft().model_dump(exclude={"draft_version"})
            original["nodes"][0]["selected"] = True
            original["nodes"][0]["position"] = {"x": 1, "y": 0}
            graph.draft = original
            await graph.save()
            proposed = deepcopy(original)
            proposed["nodes"][0].update(
                selected=False, measured={"width": 100}, dragging=True
            )
            proposed["nodes"][0]["position"]["x"] = 1.0
            unchanged = await FlowGraphService.preview_draft(
                graph.id,
                GraphWrite(**proposed, draft_version=0),
                ADMIN,
            )
            assert unchanged.changes == []

            proposed["nodes"][0]["position"]["x"] = 2
            proposed["nodes"][0]["data"]["url"] = "https://new.test"
            proposed["nodes"][0]["width"] = 300
            preview = await FlowGraphService.preview_draft(
                graph.id,
                GraphWrite(**proposed, draft_version=0),
                ADMIN,
            )
            change = preview.changes[0]
            assert change.operation == "modify"
            assert change.changed_paths == ["/data/url", "/position/x", "/width"]
            assert change.changed_path_count == 3
            assert change.omitted_changed_paths == 0
            assert preview.validation.valid
            assert preview.validation.readiness.status == "incomplete"
            assert preview.validation.executed is False

            saved = await FlowGraphService.save_graph(
                graph.id, GraphWrite(**proposed, draft_version=0), ADMIN
            )
            assert saved.draft_version == 1
            assert saved.definition is None

    asyncio.run(run())


def test_edge_paths():
    async def run():
        async with database() as graph:
            original = {
                "nodes": [
                    {
                        "id": "start",
                        "data": {"$schema": "condition", "expression": "true"},
                    },
                    *[
                        {
                            "id": id,
                            "data": {"$schema": "http", "url": "https://example.test"},
                        }
                        for id in ("one", "two")
                    ],
                ],
                "edges": [
                    {
                        "id": "link",
                        "source": "start",
                        "sourceHandle": "is_true",
                        "target": "one",
                        "targetHandle": "input",
                        "selected": True,
                    }
                ],
            }
            graph.draft = original
            await graph.save()
            proposed = deepcopy(original)
            proposed["edges"][0]["selected"] = False
            unchanged = await FlowGraphService.preview_draft(
                graph.id, GraphWrite(**proposed, draft_version=0), ADMIN
            )
            assert unchanged.changes == []

            proposed["edges"][0].update(sourceHandle="is_false", target="two")
            preview = await FlowGraphService.preview_draft(
                graph.id, GraphWrite(**proposed, draft_version=0), ADMIN
            )

            assert preview.validation.valid
            assert len(preview.changes) == 1
            change = preview.changes[0]
            assert change.kind == "edge"
            assert change.changed_paths == ["/sourceHandle", "/target"]
            assert change.changed_path_count == 2
            assert change.omitted_changed_paths == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "method", ["save_graph", "publish_graph", "preview_draft", "validate_graph"]
)
def test_permissions(method):
    async def run():
        async with database() as graph:
            with pytest.raises(ForbiddenException):
                await getattr(FlowGraphService, method)(graph.id, draft(), READER)
            await graph.refresh_from_db()
            assert graph.draft_version == 0
            assert graph.draft == {"nodes": [], "edges": []}

    asyncio.run(run())


def test_readonly():
    async def run():
        async with database() as graph:
            graph.editable = False
            await graph.save()
            for method in (FlowGraphService.save_graph, FlowGraphService.preview_draft):
                with pytest.raises(ForbiddenException, match="flow_not_editable"):
                    await method(graph.id, draft(), ADMIN)

    asyncio.run(run())


def test_invalid_create():
    async def run():
        async with database():
            invalid = GraphDocument(
                nodes=[{"id": "bad", "data": {"$schema": "missing"}}], edges=[]
            )
            with pytest.raises(KaloscopeException, match="flow_graph_invalid"):
                await FlowGraphService.create_draft(
                    GraphDraftCreate(name="New", category="schedule", document=invalid),
                    ADMIN,
                )
            assert not await FlowGraph.filter(name="New").exists()

    asyncio.run(run())


def test_history():
    async def run():
        async with database() as graph:
            for version in range(23):
                await FlowGraphService.save_graph(
                    graph.id, draft(version, f"https://test/{version}"), ADMIN
                )
            history = await FlowGraphService.list_drafts(graph.id, ADMIN)
            assert [item["version"] for item in history] == list(range(22, 2, -1))
            with pytest.raises(NotFoundException):
                await FlowGraphService.get_draft(graph.id, 2, ADMIN)
            with pytest.raises(KaloscopeException, match="flow_draft_conflict"):
                await FlowGraphService.restore_draft(graph.id, 3, 22, ADMIN)
            await graph.delete()
            assert await FlowDraftRevision.all().count() == 0

    asyncio.run(run())
