"""Evaluate download diagnostics and personal favorites with an opt-in real model."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.ai import AIModelSelection
from app.models.assistant import AssistantTurnRequest, ConversationCreate
from app.models.download import DownloadPlanAttempt, DownloadTask
from app.models.media import MediaLib
from app.models.user import Permissions, PermType, UserFavorite, UserPermission
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from tests.test_agent_live import official_provider
from tests.test_agent_live import pytestmark as pytestmark
from tests.test_download_plan_changes import stored_plan
from tests.test_mcp import application, call


async def run_turn(chat, model, user, context, key, prompt):
    """Ask the configured provider and return successful tool evidence and text.

    Args:
        chat: Isolated conversation ID.
        model: Enabled official provider selection.
        user: Current test account.
        context: The visible application page.
        key: Unique turn identifier.
        prompt: Natural-language action requested by the test.

    Returns:
        Successful tool names paired with their data, and the final response.
    """
    request = AssistantTurnRequest.model_validate(
        {"model": model, "context": context, "request_id": key, "prompt": prompt}
    )
    events = [
        event async for event in AssistantService.persistent_stream(chat, request, user)
    ]
    assert events[-1].event == "done", events[-1].data.get("code")
    successful = [
        (e.data["name"], e.data["data"])
        for e in events
        if e.event == "tool_result" and e.data["ok"]
    ]
    calls = [
        (name, data.get("domain")) if name == "discover_actions" else name
        for name, data in successful
    ]
    print(f"DeepSeek {model.model_id} {key} tools: {calls}")
    return successful, "".join(
        e.data["text"] for e in events if e.event == "text_delta"
    )


def test_trace(tmp_path, monkeypatch):
    async def run():
        async with application() as app:
            provider = await official_provider(tmp_path, monkeypatch)
            model = AIModelSelection(
                provider_id=provider.id, model_id=provider.model_ids[0]
            )
            graph, downloader, plan = await stored_plan()
            lib = await MediaLib.create(
                name="Shows", dir="/synthetic/library", priority=1, lib_type="movie"
            )
            attempt = await DownloadPlanAttempt.create(
                plan_id=plan.id,
                workflow_id=graph.id,
                status="completed",
                submitted=1,
                results_seen=1,
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir="/synthetic/downloads",
                name="Lantern-E02",
                state="completed",
                plan_attempt_id=attempt.id,
                transfer_lib=lib,
                transfer_method="copy",
                transfer_pending=True,
                last_transfer_failed=True,
            )
            account = app.shared_ctx.sessions["admin"]
            chat = await Sessions.create(ConversationCreate(model=model), account)
            context = {
                "page_type": "completed_downloads",
                "route": "/downloads/completed",
                "selected_ids": [task.id],
            }
            successful, text = await run_turn(
                chat["id"],
                model,
                account,
                context,
                "transfer",
                "查一下选中的下载为什么还没入库，核对实际的转存记录。只告诉我查到的状态和证据。",
            )
            reports = [
                data
                for name, data in successful
                if name == "diagnose_download_ingestion"
                and data.get("task_id") == task.id
            ]
            assert reports and reports[-1]["stage"] == "transfer_failed"
            assert "失败" in text and (
                "转存" in text or "转移" in text or "入库" in text
            )
            external = await call(
                app,
                "diagnose_download_ingestion",
                {"query": {"task_id": task.id}},
                token="admin",
            )
            assert external["structuredContent"]["stage"] == reports[-1]["stage"]
            assert await DownloadTask.all().count() == 1

    asyncio.run(run())


def test_favorites(tmp_path, monkeypatch):
    async def run():
        async with application() as app:
            provider = await official_provider(tmp_path, monkeypatch)
            model = AIModelSelection(
                provider_id=provider.id, model_id=provider.model_ids[0]
            )
            graph, _, _ = await stored_plan()
            account = app.shared_ctx.sessions["reader"]
            await UserPermission.create(
                user_id=account.id, rel_type=PermType.INDEXER, rel_id=graph.id
            )
            account.perms = Permissions(indexer_ids=[graph.id])
            execute = AsyncMock(
                return_value={
                    "items": [
                        {
                            "id": "lantern-2",
                            "title": "Lantern Episode Two",
                            "link": "synthetic-omitted",
                        }
                    ],
                    "total": 1,
                }
            )
            app.ctx.flow_engine = SimpleNamespace(execute=execute)
            chat = await Sessions.create(ConversationCreate(model=model), account)
            context = {
                "page_type": "web_search",
                "route": f"/websearch/{graph.id}",
                "workflow_id": graph.id,
            }
            saved, _ = await run_turn(
                chat["id"],
                model,
                account,
                context,
                "favorite",
                "在当前站点搜 Lantern，"
                "把标题为 Lantern Episode Two 的结果加入我的收藏。",
            )
            assert {"search_indexer", "set_indexer_favorite"}.issubset(
                name for name, _ in saved
            )
            assert execute.await_count >= 1
            assert (
                await UserFavorite.filter(
                    user_id=account.id, indexer_id=graph.id, rsrc_id="lantern-2"
                ).count()
                == 1
            )
            listed, text = await run_turn(
                chat["id"],
                model,
                account,
                context,
                "read-saved",
                "重新查询我的收藏，列出当前站点里已收藏的标题。",
            )
            assert (
                "list_indexer_favorites" in [name for name, _ in listed]
                and "Lantern Episode Two" in text
            )
            external = await call(
                app, "list_indexer_favorites", {"query": {"workflow_id": graph.id}}
            )
            assert (
                external["structuredContent"]["items"][0]["resource_id"] == "lantern-2"
            )
            assert not await DownloadTask.all().exists()

    asyncio.run(run())
