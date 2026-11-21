"""Verify domain-scoped credentials and media-library boundaries over MCP."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sanic import Sanic

from app.core.exceptions import ForbiddenException
from app.models.agent_actions import AdminChange
from app.models.flow import FlowGraph, FlowRepository, FlowTemplate
from app.models.mcp import MCPAccessTokenCreate
from app.models.user import PermType, User, UserPermission
from app.routes.assistant import decide_operation, get_operation
from app.services import agent_actions
from app.services.mcp import MCPAccessTokenService
from app.utils.json import loads
from tests.test_download_plan_changes import stored_plan
from tests.test_mcp import application, call, post, response_json
from tests.test_media_batches import workspace as workspace
from tests.test_media_changes import store
from tests.test_routes_assistant import request as browser_request
from tests.test_services_media_reading import _client


async def issue(owner, domain="media", libraries=None, scope="read_write"):
    return await MCPAccessTokenService.create(
        owner.id,
        MCPAccessTokenCreate(
            name="restricted", domain=domain, library_ids=libraries, scope=scope
        ),
    )


async def catalog(app, token):
    listed = response_json(await post(app, "tools/list", token=token))
    return {tool["name"] for tool in listed["result"]["tools"]}


@pytest.mark.parametrize(
    ("domain", "libraries"),
    [
        ("all", [1]),
        ("system", [1]),
        ("media", []),
        ("media", [1, 1]),
        ("media", [True]),
        ("media", [0]),
        ("unknown", None),
    ],
)
def test_invalid_grant(domain, libraries):
    with pytest.raises(ValidationError):
        MCPAccessTokenCreate(name="limited", domain=domain, library_ids=libraries)


def test_catalog():
    async def run():
        async with application() as app:
            admin = app.shared_ctx.sessions["admin"]
            for domain in ("downloads", "workflows", "system", "media"):
                token = (await issue(admin, domain))["token"]
                tools = await catalog(app, token)
                if domain in {"system", "workflows"}:
                    listed = response_json(await post(app, "tools/list", token=token))
                    schemas = {
                        entry["name"]: entry["inputSchema"]["$defs"]
                        for entry in listed["result"]["tools"]
                        if entry["name"]
                        in {"inspect_workspace", "review_workspace_change"}
                    }
                    topics = schemas["inspect_workspace"]["WorkspaceQuery"][
                        "properties"
                    ]["topic"]["enum"]
                    assert set(topics) == (
                        {"logs", "transcoding", "downloaders"}
                        if domain == "system"
                        else {"workflow_templates", "receipt"}
                    )
                    if domain == "workflows":
                        actions = schemas["review_workspace_change"]["AdminChange"][
                            "properties"
                        ]["action"]["enum"]
                        assert set(actions) == {"copy_template", "publish_workflow"}

                if domain == "workflows":
                    assert "validate_workflow_template" in tools
                if domain == "media":
                    listed = response_json(await post(app, "tools/list", token=token))
                    media = next(
                        t
                        for t in listed["result"]["tools"]
                        if t["name"] == "apply_media_change"
                    )
                    change = media["inputSchema"]["$defs"]["MediaChangeApply"][
                        "properties"
                    ]["change"]
                    assert change == {"$ref": "#/$defs/MediaBatch"}
                assert tools
                assert "run_workflow" not in tools
                assert (await call(app, "run_workflow", token=token))["isError"]
                assert ("inspect_workspace" in tools) == (
                    domain in {"system", "workflows"}
                )
                assert ("review_workspace_change" in tools) == (domain == "workflows")
                if domain == "downloads":
                    assert {
                        "search_indexer",
                        "list_download_indexers",
                        "diagnose_download_ingestion",
                        "review_plan_replacement",
                        "get_plan_replacement",
                    } <= tools
                elif domain == "media":
                    assert {
                        "get_media_batch",
                        "apply_media_change",
                        "cancel_media_batch",
                    } <= tools
                    assert "diagnose_download_ingestion" not in tools
                    assert (await call(app, "list_workflows", token=token))["isError"]

            reader = app.shared_ctx.sessions["reader"]
            downloads = (await issue(reader, "downloads"))["token"]
            from tests.test_download_plan_authoring import fixture_plan

            graph, _, _ = await fixture_plan()
            ro = (await issue(reader, "downloads", scope="read_only"))["token"]
            assert (await call(app, "list_download_indexers", token=ro))[
                "structuredContent"
            ]["items"] == []
            grant = await UserPermission.create(
                user_id=reader.id, rel_type=PermType.INDEXER, rel_id=graph.id
            )
            sources = (await call(app, "list_download_indexers", token=ro))[
                "structuredContent"
            ]
            assert sources["total"] == 1
            assert sources["items"][0]["workflow_id"] == graph.id
            assert sources["items"][0]["filters"]["quality"]["type"] == "checkbox"
            await grant.delete()
            assert (await call(app, "list_download_indexers", token=ro))[
                "structuredContent"
            ]["items"] == []
            ro_media = (await issue(admin, "all", scope="read_only"))["token"]
            assert "review_media_rescrape" not in await catalog(app, ro_media)
            assert (await call(app, "review_media_rescrape", token=ro_media))["isError"]
            listed = response_json(await post(app, "tools/list", token=downloads))
            search = next(
                t for t in listed["result"]["tools"] if t["name"] == "search_indexer"
            )
            assert (
                "sources"
                not in search["inputSchema"]["$defs"]["IndexerSearch"]["properties"]
            )
            assert "diagnose_download_ingestion" not in await catalog(app, downloads)
            assert (await call(app, "diagnose_download_ingestion", token=downloads))[
                "isError"
            ]
            assert {"inspect_workspace", "review_workspace_change"} <= await catalog(
                app, "admin"
            )

    asyncio.run(run())


def test_library_grants(tmp_path, monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            media = await store(tmp_path)
            admin = app.shared_ctx.sessions["admin"]
            reader = app.shared_ctx.sessions["reader"]
            with pytest.raises(ForbiddenException):
                await issue(reader, libraries=[media.movies.id])
            with pytest.raises(ForbiddenException):
                await issue(admin, libraries=[999])
            allowed = await UserPermission.create(
                user_id=reader.id, rel_type=PermType.MEDIA_LIB, rel_id=media.movies.id
            )
            await UserPermission.create(
                user_id=reader.id, rel_type=PermType.MEDIA_LIB, rel_id=media.shows.id
            )
            token = (await issue(reader, libraries=[media.movies.id]))["token"]
            tools = await catalog(app, token)
            assert tools == {
                "list_media_libraries",
                "list_media_items",
                "get_media_item",
            }

            libraries = (await call(app, "list_media_libraries", token=token))[
                "structuredContent"
            ]["items"]
            assert [lib["id"] for lib in libraries] == [media.movies.id]
            assert libraries[0]["triggers"] == []
            items = (await call(app, "list_media_items", token=token))[
                "structuredContent"
            ]
            assert items["total"] == 1 and items["items"][0]["id"] == media.movie.id
            assert not (
                await call(
                    app, "get_media_item", {"item_id": media.movie.id}, token=token
                )
            )["isError"]
            assert (
                await call(
                    app, "get_media_item", {"item_id": media.show.id}, token=token
                )
            )["isError"]
            assert (
                await call(
                    app, "list_media_items", {"library_id": media.shows.id}, token=token
                )
            )["structuredContent"]["total"] == 0

            await allowed.delete()
            assert not (await call(app, "list_media_libraries", token=token))[
                "structuredContent"
            ]["items"]
            assert (
                await call(
                    app, "get_media_item", {"item_id": media.movie.id}, token=token
                )
            )["isError"]
            assert (await call(app, "list_media_items", token="admin"))[
                "structuredContent"
            ]["total"] > 1

    asyncio.run(run())


def test_batch_scope(tmp_path, monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            media = await store(tmp_path)
            admin = app.shared_ctx.sessions["admin"]
            token = (await issue(admin, libraries=[media.movies.id]))["token"]
            read = (await issue(admin, libraries=[media.movies.id], scope="read_only"))[
                "token"
            ]
            assert "apply_media_change" not in await catalog(app, read)

            for lib, access in ((media.shows, False), (media.movies, True)):
                probe = Path(lib.dir) / "discard.url"
                probe.write_text("unused")
                change = {
                    "action": "batch",
                    "library_id": lib.id,
                    "operation": "delete",
                    "extensions": ["url"],
                }
                reviewed = await call(
                    app, "review_media_batch", {"change": change}, token="admin"
                )
                review = reviewed["structuredContent"]
                batch = {"batch_id": review["batch_id"]}
                apply = {
                    "application": {
                        "request_id": review["request_id"],
                        "change": change,
                        "revision": review["revision"],
                        "confirmed": True,
                    }
                }
                preview = await call(
                    app, "review_media_batch", {"change": change}, token=token
                )
                detail = await call(app, "get_media_batch", batch, token=token)
                stop = await call(app, "cancel_media_batch", batch, token=token)
                assert (await call(app, "apply_media_change", apply, token=read))[
                    "isError"
                ]
                result = await call(app, "apply_media_change", apply, token=token)
                if access:
                    assert preview["structuredContent"]["valid"]
                    assert detail["structuredContent"]["library_id"] == lib.id
                    assert stop["structuredContent"]["state"] == "reviewed"
                    assert result["structuredContent"]["outcome"] == "completed"
                    assert not probe.exists()
                else:
                    assert all(r["isError"] for r in (preview, detail, stop, result))
                    assert probe.exists()

            denied = await call(
                app,
                "apply_media_change",
                {
                    "application": {
                        "change": {"action": "scan", "library_id": media.movies.id}
                    }
                },
                token=token,
            )
            assert "only media file batch" in denied["content"][0]["text"]

    asyncio.run(run())


def test_browser_batch_replay(tmp_path, monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            media = await store(tmp_path)
            admin = app.shared_ctx.sessions["admin"]
            token = (await issue(admin, libraries=[media.movies.id]))["token"]
            other = (await issue(admin, libraries=[media.shows.id]))["token"]
            leaf = Path(media.movies.dir) / "scoped.url"
            leaf.write_text("temporary")
            change = {
                "action": "batch",
                "library_id": media.movies.id,
                "operation": "delete",
                "extensions": ["url"],
            }
            reviewed = (
                await call(app, "review_media_batch", {"change": change}, token=token)
            )["structuredContent"]
            key = reviewed["batch_id"]
            assert leaf.exists()
            async with _client(admin) as browser:
                inspected = await browser.get(f"/_api/media/batch/{key}")
                assert inspected.json()["data"]["state"] == "reviewed"
                applied = await browser.post(f"/_api/media/batch/{key}/apply")
                assert applied.json()["data"]["outcome"] == "completed"
            assert not leaf.exists()
            args = {
                "application": {
                    "change": change,
                    "request_id": reviewed["request_id"],
                    "revision": reviewed["revision"],
                    "confirmed": True,
                }
            }
            replay = (await call(app, "apply_media_change", args, token=token))[
                "structuredContent"
            ]
            assert replay["outcome"] == "completed" and replay["replayed"]
            assert (await call(app, "apply_media_change", args, token=other))["isError"]

    asyncio.run(run())


def test_workflow_reviews(tmp_path, monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *a, **kw: app)
            admin = app.shared_ctx.sessions["admin"]
            token = (await issue(admin, "workflows", scope="read_only"))["token"]
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
                app,
                "inspect_workspace",
                {"query": {"topic": "workflow_templates"}},
                token=token,
            )
            assert [r["id"] for r in listed["structuredContent"]["items"]] == [
                template.id
            ]
            other = await agent_actions.review_action(
                AdminChange(
                    action="create_library",
                    name="Private library",
                    directory=str(tmp_path),
                    library_type="novel",
                ),
                admin,
            )
            for name, args in [
                ("inspect_workspace", {"query": {"topic": "logs"}}),
                ("inspect_workspace", {"query": {"topic": "actions"}}),
                (
                    "inspect_workspace",
                    {
                        "query": {
                            "topic": "receipt",
                            "operation_id": other["operation_id"],
                        }
                    },
                ),
                (
                    "review_workspace_change",
                    {"change": {"action": "delete_library", "target_id": 1}},
                ),
            ]:
                denied = await call(app, name, args, token=token)
                assert denied["isError"] and "Private library" not in str(denied)
            for change in [
                {"action": "copy_template", "target_id": template.id, "name": "Copy"},
                {"action": "publish_workflow", "target_id": graph.id},
            ]:
                reviewed = (
                    await call(
                        app, "review_workspace_change", {"change": change}, token=token
                    )
                )["structuredContent"]
                key = reviewed["operation_id"]
                assert reviewed["status"] == "pending"
                assert "review_path" not in reviewed
                assert (
                    "bound_resources" not in reviewed["preview"]
                    and "download_plans" not in reviewed["preview"]
                )
                assert reviewed["action"] == change["action"]
                page = browser_request(app, body={})
                page.ctx.user = admin
                full = loads((await get_operation(page, UUID(key))).body)
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
                        token=token,
                    )
                )["structuredContent"]
                assert receipt["status"] == "applied"
                assert "review_path" not in receipt
                assert (
                    "bound_resources" not in receipt["preview"]
                    and "download_plans" not in receipt["preview"]
                )

            await User.filter(id=admin.id).update(role="user")
            assert "review_workspace_change" not in await catalog(app, token)
            assert (
                await call(
                    app,
                    "inspect_workspace",
                    {"query": {"topic": "workflow_templates"}},
                    token=token,
                )
            )["isError"]

    asyncio.run(run())


def test_system_topics(monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(Sanic, "get_app", lambda *a, **kw: app)
            monkeypatch.setattr(agent_actions, "list_tasks", AsyncMock(return_value=[]))
            admin = app.shared_ctx.sessions["admin"]
            system = (await issue(admin, "system", scope="read_only"))["token"]
            for topic in ("logs", "downloaders", "transcoding"):
                facts = await call(
                    app, "inspect_workspace", {"query": {"topic": topic}}, token=system
                )
                assert facts["structuredContent"]["topic"] == topic
            for topic in ("workflow_templates", "actions", "receipt", "batches"):
                assert (
                    await call(
                        app,
                        "inspect_workspace",
                        {"query": {"topic": topic}},
                        token=system,
                    )
                )["isError"]
            args = {"change": {"action": "publish_workflow", "target_id": 1}}
            assert (await call(app, "review_workspace_change", args, token=system))[
                "isError"
            ]
            for domain in ("system", "workflows"):
                token = (
                    await issue(
                        app.shared_ctx.sessions["reader"], domain, scope="read_write"
                    )
                )["token"]
                assert {"inspect_workspace", "review_workspace_change"}.isdisjoint(
                    await catalog(app, token)
                )
                assert (
                    await call(
                        app,
                        "inspect_workspace",
                        {"query": {"topic": "logs"}},
                        token=token,
                    )
                )["isError"]
                assert (await call(app, "review_workspace_change", args, token=token))[
                    "isError"
                ]

    asyncio.run(run())
