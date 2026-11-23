"""Verify read-only and read-write MCP access across tools and browser approvals."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sanic import Sanic

from app.core.exceptions import ForbiddenException
from app.models.flow import FlowGraph, FlowRepository, FlowTemplate
from app.models.mcp import MCPAccessTokenCreate
from app.models.user import User
from app.routes.assistant import decide_operation, get_operation
from app.services import workspace_queries
from app.services.mcp import MCPAccessTokenService
from app.utils.json import loads
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_changes import stored_plan
from tests.test_mcp import application, call, post
from tests.test_media_batches import workspace as workspace
from tests.test_media_changes import store
from tests.test_routes_assistant import request as browser_request
from tests.test_services_media_reading import _client


def test_download_indexers():
    async def run():
        async with application() as app:
            graph, _, _ = await fixture_plan()
            result = await call(app, "list_download_indexers")
            sources = result["structuredContent"]
            assert sources["total"] == 1
            assert sources["items"][0]["workflow_id"] == graph.id
            assert sources["items"][0]["filters"]["quality"]["type"] == "checkbox"

    asyncio.run(run())


@pytest.mark.parametrize("library", ["movies", "shows"])
def test_browser_batch_replay(tmp_path, monkeypatch, library):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            media = await store(tmp_path)
            lib = getattr(media, library)
            admin = app.shared_ctx.sessions["admin"]
            writer = await MCPAccessTokenService.create(
                admin.id, MCPAccessTokenCreate(name="writer", scope="read_write")
            )
            leaf = Path(lib.dir) / "temporary.url"
            leaf.write_text("temporary")
            change = {
                "action": "batch",
                "library_id": lib.id,
                "operation": "delete",
                "extensions": ["url"],
            }
            reviewed = (await call(app, "review_media_batch", {"change": change}))[
                "structuredContent"
            ]
            key = reviewed["batch_id"]
            args = {
                "application": {
                    "change": change,
                    "request_id": reviewed["request_id"],
                    "revision": reviewed["revision"],
                    "confirmed": True,
                }
            }
            assert (await call(app, "apply_media_change", args))["isError"]
            assert leaf.exists()
            async with _client(admin) as browser:
                inspected = await browser.get(f"/_api/media/batch/{key}")
                assert inspected.json()["data"]["state"] == "reviewed"
                applied = await browser.post(f"/_api/media/batch/{key}/apply")
                assert applied.json()["data"]["outcome"] == "completed"
            assert not leaf.exists()
            replay = await call(app, "apply_media_change", args, token=writer["token"])
            assert replay["structuredContent"]["outcome"] == "completed"
            assert replay["structuredContent"]["replayed"]

    asyncio.run(run())


def test_workflow_reviews(monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            admin = app.shared_ctx.sessions["admin"]
            graph, _, plan = await stored_plan()
            await FlowGraph.filter(id=graph.id).update(draft=graph.definition)
            repo = await FlowRepository.create(
                repo_name="fixture", repo_url="https://example.invalid"
            )
            template = await FlowTemplate.create(
                repo=repo,
                path="fixture.json",
                name="Shelf",
                category="indexer",
                revision=1,
                definition=graph.definition,
            )
            listed = await call(
                app, "inspect_workspace", {"query": {"topic": "workflow_templates"}}
            )
            assert [row["id"] for row in listed["structuredContent"]["items"]] == [
                template.id
            ]
            for change in [
                {"action": "copy_template", "target_id": template.id, "name": "Copy"},
                {"action": "publish_workflow", "target_id": graph.id},
            ]:
                reviewed = (
                    await call(app, "review_workspace_change", {"change": change})
                )["structuredContent"]
                key = reviewed["operation_id"]
                assert reviewed["action"] == change["action"]
                assert reviewed["status"] == "pending"
                assert "review_path" not in reviewed
                page = browser_request(app, body={})
                page.ctx.user = admin
                full = loads((await get_operation(page, UUID(key))).body)
                assert reviewed["preview"] == full["preview"]
                if change["action"] == "publish_workflow":
                    assert full["preview"]["download_plans"] == [plan.id]
                else:
                    assert not await FlowGraph.exists(name="Copy")
                approval = browser_request(
                    app, body={"digest": full["digest"], "approve": True}
                )
                approval.ctx.user = admin
                confirmed = loads((await decide_operation(approval, UUID(key))).body)
                assert confirmed["status"] == "applied"
                receipt = (
                    await call(
                        app,
                        "inspect_workspace",
                        {"query": {"topic": "receipt", "operation_id": key}},
                    )
                )["structuredContent"]
                assert receipt["status"] == "applied"
                assert receipt["preview"] == reviewed["preview"]
                assert "review_path" not in receipt

            await User.filter(id=admin.id).update(role="user")
            with pytest.raises(ForbiddenException):
                await post(app, "tools/list")

    asyncio.run(run())


def test_workspace_topics(monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            monkeypatch.setattr(
                workspace_queries, "list_tasks", AsyncMock(return_value=[])
            )
            for topic in (
                "logs",
                "downloaders",
                "transcoding",
                "workflow_templates",
                "actions",
                "batches",
            ):
                facts = await call(
                    app, "inspect_workspace", {"query": {"topic": topic}}
                )
                assert facts["structuredContent"]["topic"] == topic

    asyncio.run(run())
