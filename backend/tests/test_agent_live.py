"""Exercise the configured DeepSeek assistant and the loopback MCP transport."""

import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from app.core.config import KaloscopeConfig
from app.core.constants import MCP_PATH
from app.core.middleware import on_request, on_response
from app.models.ai import AIModelSelection, AIProvider
from app.models.assistant import AssistantTurnRequest, ConversationCreate
from app.models.download import DownloadPlan, DownloadTask
from app.routes.assistant import decide_plan_review, read_plan_review
from app.routes.download import decide_replacement, replacement_status
from app.routes.mcp import dispatch
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService
from app.utils.json import loads
from tests.test_download_plan_changes import stored_plan
from tests.test_mcp import application

# this suite opens a local listener and makes a metered request only when requested
pytestmark = pytest.mark.skipif(
    os.environ.get("KALOSCOPE_LIVE_AGENT") != "1",
    reason="Opt-in real provider evaluation",
)


async def official_provider(tmp_path, monkeypatch):
    """Load the configured official provider into the isolated evaluation database.

    Args:
        tmp_path: Disposable workspace for this evaluation.
        monkeypatch: Per-test settings override.

    Returns:
        Provider stored only in the test application's database.
    """
    backend = Path(__file__).resolve().parents[1]
    database = backend.parent / "workspace" / "database"
    config = KaloscopeConfig(backend / "app/config.toml", tmp_path)
    config.secret_key_path = database / "secret.key"
    monkeypatch.setattr(KaloscopeConfig, "_config", config)
    assert config.secret_key_path.is_file()
    uri = (database / "db.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        row = connection.execute(
            "SELECT base_url, api_key, model_ids FROM ai_provider WHERE enabled=1 "
            "AND preset='deepseek' AND api_key IS NOT NULL "
            "AND rtrim(base_url,'/') IN (?,?) "
            "ORDER BY id LIMIT 1",
            ("https://api.deepseek.com", "https://api.deepseek.com/v1"),
        ).fetchone()
    if not row:
        pytest.fail("No enabled official DeepSeek provider in the local workspace")
    return await AIProvider.create(
        name="Evaluation",
        preset="deepseek",
        base_url=row[0],
        api_key=row[1],
        model_ids=loads(row[2]),
    )


@pytest.mark.parametrize(
    "scenario", ["read", "rebuild", "choose", "consult", "delete", "cancel"]
)
def test_plan(tmp_path, monkeypatch, scenario):
    async def run():
        async with application() as app:
            provider = await official_provider(tmp_path, monkeypatch)
            graph, _, plan = await stored_plan()
            if scenario == "choose":
                await DownloadPlan.create(
                    **{**(await DownloadPlan.filter(id=plan.id).values())[0], "id": 41}
                )
            admin = app.shared_ctx.sessions["admin"]
            app.config.TOUCHUP = False
            app.add_route(dispatch, MCP_PATH, methods=["GET", "POST", "DELETE"])
            route = "/_api/download/plan/replacement/<operation_id:uuid>"
            app.add_route(replacement_status, route, methods=["GET"])
            app.add_route(decide_replacement, route + "/decision", methods=["POST"])
            regular = "/_api/assistant/plan-reviews/<call_id:str>"
            app.add_route(read_plan_review, regular, methods=["GET"])
            app.add_route(decide_plan_review, regular + "/decision", methods=["POST"])
            app.register_middleware(on_request, "request")
            app.register_middleware(on_response, "response")
            server = await app.create_server(
                host="127.0.0.1", port=0, return_asyncio_server=True, access_log=False
            )
            try:
                await server.startup()
                await server.before_start()
                await server.start_serving()
                await server.after_start()
                port = server.server.sockets[0].getsockname()[1]
                async with (
                    httpx2.AsyncClient(
                        headers={
                            "Authorization": f"Bearer {app.ctx.mcp_tokens['admin']}"
                        },
                        trust_env=False,
                    ) as http,
                    Client(
                        streamable_http_client(
                            f"http://127.0.0.1:{port}{MCP_PATH}", http_client=http
                        )
                    ) as client,
                ):
                    catalog = await client.list_tools()
                    assert "get_download_plan" in {tool.name for tool in catalog.tools}
                    result = await client.call_tool(
                        "get_download_plan", {"plan_id": plan.id}
                    )
                    assert (
                        not result.is_error
                        and result.structured_content["plan_id"] == plan.id
                    )
                    preview = await client.call_tool(
                        "review_plan_replacement", {"plan_id": plan.id}
                    )
                    assert (
                        not preview.is_error
                        and preview.structured_content["status"] == "pending"
                    )
                    receipt = await client.call_tool(
                        "get_plan_replacement",
                        {"operation_id": preview.structured_content["operation_id"]},
                    )
                    assert (
                        not receipt.is_error
                        and receipt.structured_content["plan_id"] == plan.id
                    )
                    print(f"MCP HTTP plan read passed ({len(catalog.tools)} tools)")

                model = AIModelSelection(
                    provider_id=provider.id, model_id=provider.model_ids[0]
                )
                chat = await AssistantSessionService.create(
                    ConversationCreate(model=model), admin
                )
                body = AssistantTurnRequest.model_validate(
                    {
                        "model": model,
                        "request_id": "evaluation",
                        "prompt": {
                            "read": "查一下选中的下载计划，告诉我关键词和执行间隔。",
                            "consult": "这个选中的下载计划能删吗？我先问问。",
                            "delete": "删掉选中的下载计划。",
                            "cancel": "请删除选中的下载计划。",
                            "rebuild": "帮我删除后按原样重建一下选中的下载计划。",
                            "choose": "帮我重建 Lantern 下载计划。",
                        }[scenario],
                        "context": {
                            "page_type": "download_plans",
                            "route": "/downloads/plans",
                            "selected_plan": {
                                "plan_id": plan.id,
                                "workflow_id": graph.id,
                            }
                            if scenario != "choose"
                            else None,
                        },
                    }
                )
                events = [
                    event
                    async for event in AssistantService.persistent_stream(
                        chat["id"], body, admin
                    )
                ]
                successful = [
                    event.data["name"]
                    for event in events
                    if event.event == "tool_result" and event.data["ok"]
                ]
                reply = "".join(
                    event.data["text"]
                    for event in events
                    if event.event == "text_delta"
                )
                assert events[-1].event == "done", events[-1].data.get("code")
                if scenario == "read":
                    assert (
                        "get_download_plan" in successful
                        and plan.keyword in reply
                        and "6" in reply
                    )
                elif scenario == "consult":
                    assert "删" in reply and not any(
                        name.startswith(("apply_", "create_")) for name in successful
                    )
                elif scenario in {"delete", "cancel"}:
                    review = next(
                        (
                            event.data
                            for event in events
                            if event.event == "tool_result"
                            and event.data["name"] == "review_download_plan_change"
                            and event.data["ok"]
                        ),
                        None,
                    )
                    assert review is not None, (successful, reply)
                    assert (
                        review["data"]["action"] == "delete" and review["data"]["valid"]
                    )
                elif scenario == "rebuild":
                    assert "review_plan_replacement" in successful, successful
                    result = next(
                        event.data["data"]
                        for event in events
                        if event.event == "tool_result"
                        and event.data["name"] == "review_plan_replacement"
                    )
                    assert result["status"] == "pending" and result["keep_history"]
                else:
                    assert "ask_user" in successful, successful
                    result = next(
                        event.data["data"]
                        for event in events
                        if event.event == "tool_result"
                        and event.data["name"] == "ask_user"
                    )
                    assert {opt["id"] for opt in result["questions"][0]["options"]} == {
                        "40",
                        "41",
                    }
                assert await DownloadPlan.all().count() == (
                    2 if scenario == "choose" else 1
                )
                assert not await DownloadTask.all().exists()
                print(f"DeepSeek {model.model_id}: {successful}; plan verified")
                if scenario in {"delete", "cancel"}:
                    approve = scenario == "delete"
                    async with httpx2.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}",
                        headers={"Authorization": "Token admin"},
                        trust_env=False,
                    ) as browser:
                        path = f"/_api/assistant/plan-reviews/{review['id']}"
                        view = await browser.get(path)
                        view.raise_for_status()
                        shown = view.json()["data"]
                        assert shown["status"] == "reviewed"
                        decision = {
                            "request_id": shown["request_id"],
                            "approve": approve,
                        }
                        for _ in range(2):
                            response = await browser.post(
                                path + "/decision", json=decision
                            )
                            assert response.json()["data"]["status"] == (
                                "deleted" if approve else "cancelled"
                            )
                        assert await DownloadPlan.exists(id=plan.id) == (not approve)
                if scenario == "rebuild":
                    # click the exact decision rendered from the real tool result
                    async with httpx2.AsyncClient(
                        base_url=f"http://127.0.0.1:{port}",
                        headers={"Authorization": "Token admin"},
                        trust_env=False,
                    ) as browser:
                        path = (
                            f"/_api/download/plan/replacement/{result['operation_id']}"
                        )
                        view = await browser.get(path)
                        view.raise_for_status()
                        shown = view.json()["data"]
                        assert (
                            shown["digest"] == result["digest"]
                            and shown["status"] == "pending"
                        )
                        decision = {"digest": shown["digest"], "approve": True}
                        first = await browser.post(path + "/decision", json=decision)
                        first.raise_for_status()
                        committed = first.json()["data"]
                        assert committed["status"] == "applied"
                        replay = await browser.post(path + "/decision", json=decision)
                        assert (
                            replay.json()["data"]["new_plan_id"]
                            == committed["new_plan_id"]
                        )
                        assert not await DownloadPlan.exists(id=plan.id)
                        assert await DownloadPlan.exists(
                            id=committed["new_plan_id"], keyword=plan.keyword
                        )
                        print("Trusted browser approval and replay verified")
            finally:
                await server.close()

    asyncio.run(run())
