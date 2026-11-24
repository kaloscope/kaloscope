"""Evaluate template discovery and direct draft copies with the configured model."""

import asyncio

from app.models.ai import AIModelSelection
from app.models.assistant import ConversationCreate
from app.models.flow import FlowGraph, FlowRepository, FlowTemplate
from app.services.assistant_sessions import AssistantSessionService
from tests.test_agent_downloads_live import run_turn
from tests.test_agent_live import official_provider
from tests.test_agent_live import pytestmark as pytestmark
from tests.test_download_plan_changes import stored_plan
from tests.test_mcp import application


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

            names = [name for name, _ in tools]
            assert "inspect_workspace" in names
            assert "review_workspace_change" not in names
            created = [data for name, data in tools if name == "create_workflow_draft"]
            assert created, names
            draft = await FlowGraph.get(name="Lantern Copy")
            assert created[-1]["workflow_id"] == draft.id
            assert created[-1]["published"] is False
            assert (draft.state, draft.category) == ("draft", template.category)
            assert draft.draft == template.definition and draft.definition is None
            assert await FlowGraph.filter(name="Lantern Copy").count() == 1

    asyncio.run(run())
