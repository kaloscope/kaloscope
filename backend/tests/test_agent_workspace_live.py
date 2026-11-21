"""Evaluate template discovery, approval and MCP receipts with the configured model."""

import asyncio
from uuid import UUID

from app.models.ai import AIModelSelection
from app.models.assistant import ConversationCreate
from app.models.flow import FlowGraph, FlowRepository, FlowTemplate
from app.services.agent_actions import decide_action
from app.services.assistant_sessions import AssistantSessionService
from tests.test_agent_downloads_live import run_turn
from tests.test_agent_live import official_provider
from tests.test_agent_live import pytestmark as pytestmark
from tests.test_download_plan_changes import stored_plan
from tests.test_mcp import application, call


def test_template_copy(tmp_path, monkeypatch):
    async def run():
        async with application() as app:
            provider = await official_provider(tmp_path, monkeypatch)
            model = AIModelSelection(
                provider_id=provider.id, model_id=provider.model_ids[0]
            )
            owner = app.shared_ctx.sessions["admin"]
            graph, _, _ = await stored_plan()
            repo = await FlowRepository.create(
                repo_name="Demo", repo_url="https://example.invalid"
            )
            template = await FlowTemplate.create(
                repo=repo,
                path="lantern.json",
                name="Lantern Source",
                category="indexer",
                revision=1,
                definition=graph.definition,
            )
            chat = await AssistantSessionService.create(
                ConversationCreate(model=model), owner
            )
            tools, _ = await run_turn(
                chat["id"],
                model,
                owner,
                {"page_type": "dashboard", "route": "/dashboard"},
                "copy-template",
                "请从现有模板 Lantern Source 复制一份工作流草稿，"
                "命名为 Lantern Copy，给我核对。",
            )
            assert "inspect_workspace" in {name for name, _ in tools}
            reviews = [
                data for name, data in tools if name == "review_workspace_change"
            ]
            assert reviews, [name for name, _ in tools]
            reviewed = reviews[-1]
            assert reviewed["status"] == "pending"
            assert reviewed["preview"]["template_id"] == template.id
            assert not await FlowGraph.exists(name="Lantern Copy")
            result = await decide_action(
                UUID(reviewed["operation_id"]), reviewed["digest"], True, owner
            )
            assert result["status"] == "applied"
            external = await call(
                app,
                "inspect_workspace",
                {
                    "query": {
                        "topic": "receipt",
                        "operation_id": reviewed["operation_id"],
                    }
                },
                token="admin",
            )
            assert external["structuredContent"]["status"] == "applied"
            assert (await FlowGraph.get(name="Lantern Copy")).state == "draft"

    asyncio.run(run())
