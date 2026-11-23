"""Integration tests for administrator reviews, grants and durable outcomes."""

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from tortoise import timezone

from app.core.ai.tools import execute_tool
from app.core.config import KaloscopeConfig
from app.core.exceptions import (
    BadRequestException,
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.mcp import _read
from app.models.agent_actions import AdminChange, AgentAdminReview, WorkspaceQuery
from app.models.download import DownloadPlan
from app.models.flow import FlowGraph, FlowRepository, FlowTemplate
from app.models.media import MediaFormat, MediaItem, MediaLib
from app.models.user import User, UserRole
from app.services.agent_actions import (
    action_status,
    decide_action,
    inspect_workspace,
    review_action,
)
from app.services.flow import FlowGraphService
from app.services.media import MediaLibService
from tests.test_assistant_persistence import database
from tests.test_download_plan_changes import stored_plan
from tests.test_services_media_reading import _item, _opf


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(
        KaloscopeConfig,
        "_config",
        KaloscopeConfig(
            Path(__file__).resolve().parents[1] / "app/config.toml", tmp_path
        ),
    )


async def decide(card, owner, approve=True):
    return await decide_action(
        UUID(card["operation_id"]), card["digest"], approve, owner
    )


def test_library(tmp_path, monkeypatch):
    async def run():
        async with database() as (owner, other):
            watcher = SimpleNamespace(
                add_observer=AsyncMock(), remove_observer=AsyncMock()
            )
            monkeypatch.setattr(
                MediaLibService, "app_ctx", lambda: SimpleNamespace(lib_watcher=watcher)
            )
            path = tmp_path / "Media"
            path.mkdir()
            source = path / "keep.txt"
            source.write_text("record")
            change = AdminChange(
                action="create_library",
                name="Books",
                directory=str(path),
                library_type="novel",
            )
            card = await review_action(change, owner)
            assert not await MediaLib.all().exists()
            assert (await review_action(change, owner))["operation_id"] == card[
                "operation_id"
            ]
            with pytest.raises(NotFoundException):
                await decide(card, other)
            with pytest.raises(BadRequestException):
                await decide_action(UUID(card["operation_id"]), "x" * 64, True, owner)
            assert (await decide(card, owner, False))["status"] == "cancelled"
            card = await review_action(change, owner)
            first, second = await asyncio.gather(
                decide(card, owner), decide(card, owner)
            )
            assert {first["status"], second["status"]} <= {"applied", "executing"}
            lib = await MediaLib.get(name="Books")
            watcher.add_observer.assert_awaited_once()
            delete = AdminChange(action="delete_library", target_id=lib.id)
            stale = await review_action(delete, owner)
            await MediaLib.filter(id=lib.id).update(name="Renamed")
            assert (await decide(stale, owner))["status"] == "conflict"
            card = await review_action(delete, owner)
            assert not card["preview"]["delete_files"]
            assert (await decide(card, owner))["status"] == "applied"
            assert (await decide(card, owner))["result"]["deleted"]
            assert source.read_text() == "record"
            watcher.remove_observer.assert_awaited_once()

    asyncio.run(run())


def test_workflow():
    async def run():
        async with database() as (user, _):
            graph, _, plan = await stored_plan()
            await FlowGraph.filter(id=graph.id).update(draft=graph.definition)
            card = await review_action(
                AdminChange(action="publish_workflow", target_id=graph.id), user
            )
            assert card["preview"]["download_plans"] == [plan.id]
            assert card["status"] == "pending"
            result = await decide(card, user)
            assert (
                result["status"] == "applied"
                and result["result"]["workflow_id"] == graph.id
            )
            assert (await FlowGraph.get(id=graph.id)).state == "published"
            assert await DownloadPlan.all().count() == 1
            repo = await FlowRepository.create(
                repo_name="fixture", repo_url="https://example.invalid"
            )
            template = await FlowTemplate.create(
                repo=repo,
                path="fixture.json",
                name="Example",
                category="indexer",
                revision=1,
                definition=graph.definition,
            )
            options = await inspect_workspace(
                WorkspaceQuery(topic="workflow_templates"), user
            )
            assert options["items"][0]["id"] == template.id
            copy = await review_action(
                AdminChange(action="copy_template", target_id=template.id, name="Copy"),
                user,
            )
            clicks = await asyncio.gather(*(decide(copy, user) for _ in range(4)))
            assert {result["status"] for result in clicks} == {"applied"}
            draft = await FlowGraph.get(name="Copy")
            assert draft.state == "draft"
            assert {r["result"]["workflow_id"] for r in clicks} == {draft.id}

    asyncio.run(run())


def test_title(tmp_path):
    async def run():
        async with database() as (user, _):
            item = await _item(tmp_path, MediaFormat.TXT)
            path = Path(item.dir) / "metadata.opf"
            path.write_bytes(_opf("Old", "<dc:publisher>Publisher</dc:publisher>"))
            change = AdminChange(
                action="edit_metadata", target_id=item.id, title="New & title"
            )
            card = await review_action(change, user)
            assert card["preview"]["before_title"] == "Old"
            assert (await decide(card, user))["status"] == "applied"
            assert (
                "New &amp; title" in path.read_text()
                and "Publisher" in path.read_text()
            )

    asyncio.run(run())


def test_permission_expiry():
    async def run():
        async with database() as (owner, other):
            graph, _, _ = await stored_plan()
            await FlowGraph.filter(id=graph.id).update(draft=graph.definition)
            change = AdminChange(action="publish_workflow", target_id=graph.id)
            card = await review_action(change, owner)
            key = UUID(card["operation_id"])
            await AgentAdminReview.filter(operation_id=key).update(
                expires_at=timezone.now() - timedelta(seconds=1)
            )
            assert (await decide(card, owner))["status"] == "expired"
            with pytest.raises(NotFoundException):
                await action_status(key, other)
            owner.mcp_scope = "read_write"
            with pytest.raises(ForbiddenException):
                await decide(card, owner)
            await User.filter(id=owner.id).update(role="user")
            with pytest.raises(ForbiddenException) as demoted:
                await review_action(change, owner)
            assert demoted.value.message == ErrorCode.FORBIDDEN
            with pytest.raises(ForbiddenException) as demoted_inspect:
                await inspect_workspace(WorkspaceQuery(topic="actions"), owner)
            assert demoted_inspect.value.message == ErrorCode.FORBIDDEN
            non_admin = SimpleNamespace(id=owner.id, role=UserRole.USER)
            with pytest.raises(ForbiddenException) as regular:
                await review_action(change, non_admin)
            assert regular.value.message == ErrorCode.PERMISSION_DENIED
            with pytest.raises(ForbiddenException) as regular_inspect:
                await inspect_workspace(WorkspaceQuery(topic="actions"), non_admin)
            assert regular_inspect.value.message == ErrorCode.PERMISSION_DENIED

    asyncio.run(run())


@pytest.mark.parametrize("field", ["name", "directory", "title"])
def test_blank_change(field):
    with pytest.raises(ValidationError):
        AdminChange(action="create_library", **{field: " 	 "})


def test_video_title(tmp_path, monkeypatch):
    async def run():
        async with database() as (user, _):
            root = tmp_path / "Movies"
            root.mkdir()
            lib = await MediaLib.create(
                name="Movies", dir=str(root), priority=1, lib_type="movie"
            )
            video = root / "film.mkv"
            video.touch()
            nfo = root / "film.nfo"
            nfo.write_text("<movie><title>Old</title><custom>Preserve</custom></movie>")
            item = await MediaItem.create(
                lib=lib, name="film", dir=str(root), path=str(video), nfo_path=str(nfo)
            )
            change = AdminChange(
                action="edit_metadata", target_id=item.id, title="New & title"
            )
            card = await review_action(change, user)
            nfo.write_text(
                "<movie><title>Other</title><custom>Preserve</custom></movie>"
            )
            assert (await decide(card, user))["status"] == "conflict"
            card = await review_action(change, user)
            assert (await decide(card, user))["status"] == "applied"
            assert "<custom>Preserve</custom>" in nfo.read_text()
            assert (await MediaItem.get(id=item.id)).title == "New & title"
            change.title = "Saved without refresh"
            card = await review_action(change, user)
            monkeypatch.setattr(
                "app.services.agent_actions.update_metadata",
                AsyncMock(side_effect=RuntimeError("offline")),
            )
            partial = await decide(card, user)
            assert partial["status"] == "partial" and partial["result"]["saved"]
            assert "Saved without refresh" in nfo.read_text()
            assert (await decide(card, user))["status"] == "partial"
            card = await review_action(change, user)
            outside = tmp_path / "outside.nfo"
            outside.write_text(nfo.read_text())
            nfo.unlink()
            nfo.symlink_to(outside)
            assert (await decide(card, user))["status"] == "conflict"
            with pytest.raises(KaloscopeException, match="media_source_unavailable"):
                await review_action(change, user)
            assistant_error = await execute_tool(
                "review_workspace_change",
                change.model_dump_json(),
                user,
                conversation_id=1,
                call_id="unavailable-nfo",
            )
            assert assistant_error["error"]["code"] == "media_source_unavailable"
            with pytest.raises(ToolError, match="media_source_unavailable"):
                await _read(review_action(change, user))
            assert str(outside) not in str(assistant_error)
            assert "Preserve" in outside.read_text()

    asyncio.run(run())


@pytest.mark.parametrize("decide_again", ["approve", "cancel", "expire"])
def test_rolled_back_review(monkeypatch, decide_again):
    async def run():
        async with database() as (owner, _):
            graph, _, _ = await stored_plan()
            await FlowGraph.filter(id=graph.id).update(draft=graph.definition)
            card = await review_action(
                AdminChange(action="publish_workflow", target_id=graph.id), owner
            )

            async def interrupted(*args, **kwargs):
                await FlowGraph.filter(id=graph.id).update(name="partial write")
                raise RuntimeError("storage unavailable")

            with monkeypatch.context() as patch:
                patch.setattr(FlowGraphService, "publish_graph", interrupted)
                assert (await decide(card, owner))["status"] == "failed"
            await graph.refresh_from_db()
            assert graph.name != "partial write"
            if decide_again == "expire":
                await AgentAdminReview.filter(operation_id=card["operation_id"]).update(
                    expires_at=timezone.now() - timedelta(seconds=1)
                )
            result = await decide(card, owner, approve=decide_again != "cancel")
            assert (
                result["status"]
                == {"expire": "expired", "approve": "applied", "cancel": "cancelled"}[
                    decide_again
                ]
            )

    asyncio.run(run())


def test_concurrent_expiry_status():
    async def run():
        async with database() as (owner, _):
            graph, _, _ = await stored_plan()
            await FlowGraph.filter(id=graph.id).update(draft=graph.definition)
            change = AdminChange(action="publish_workflow", target_id=graph.id)
            expired_card = await review_action(change, owner)
            expired_key = UUID(expired_card["operation_id"])
            await AgentAdminReview.filter(operation_id=expired_key).update(
                expires_at=timezone.now() - timedelta(seconds=1)
            )
            expired_results = await asyncio.gather(
                *(action_status(expired_key, owner) for _ in range(8))
            )
            assert [r["status"] for r in expired_results] == ["expired"] * 8

            timed_out_card = await review_action(change, owner)
            timed_out_key = UUID(timed_out_card["operation_id"])
            await AgentAdminReview.filter(operation_id=timed_out_key).update(
                status="executing",
                updated_at=timezone.now() - timedelta(minutes=11),
            )
            timed_out_results = await asyncio.gather(
                *(action_status(timed_out_key, owner) for _ in range(8))
            )
            assert [r["status"] for r in timed_out_results] == ["uncertain"] * 8

    asyncio.run(run())
