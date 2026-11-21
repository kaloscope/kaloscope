"""Evaluate real-model subscriptions through authenticated web and MCP calls."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.core.constants import SESSION_ID
from app.core.dl.config import encrypt_config
from app.core.dl.driver import DownloaderDriver, DownloadSource
from app.core.exceptions import error_handler
from app.core.middleware import on_request, on_response
from app.models.assistant import AssistantToolCall
from app.models.download import DownloadPlan, DownloadPlanCreation, DownloadTask
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import MediaLib
from app.models.user import User, UserRole
from app.routes.assistant import assistant
from app.routes.auth import auth
from app.services import download
from app.services.mcp import MCPAccessTokenService
from app.services.user import UserService
from app.utils.json import loads
from tests.test_agent_live import official_provider
from tests.test_agent_live import pytestmark as pytestmark
from tests.test_download_plan_authoring import fixture_plan
from tests.test_mcp import application, call


def tool_results(response):
    """Read the completed tools in an assistant event stream.

    Args:
        response: Stream captured by the authenticated test client.

    Returns:
        Tool results in the order the user received them.
    """
    response.raise_for_status()
    assert "event: done\n" in response.text
    return [
        loads(block.split("\ndata: ", 1)[1])
        for block in response.text.split("\n\n")
        if block.startswith("event: tool_result\n")
    ]


async def check_receipt(app, browser, review, expected_status):
    """Check the same frozen subscription through the browser and MCP.

    Args:
        app: Isolated application with reader and administrator MCP accounts.
        browser: Authenticated session belonging to the subscription owner.
        review: Validated plan returned to the assistant.
        expected_status: Expected receipt status before the browser acts.

    Returns:
        Single subscription after both authorized clients have requested creation.
    """
    request_id = review["data"]["request_id"]
    path = f"/_api/assistant/reviews/{review['id']}"
    shown = await browser.get(path)
    shown.raise_for_status()
    card = shown.json()["data"]
    assert card["request_id"] == request_id
    assert card["status"] == expected_status
    creation = {
        "creation": {
            "request_id": request_id,
            "confirmed": True,
            "plan": review["data"]["plan"],
        }
    }
    prior_count = await DownloadPlan.all().count()
    reader = app.shared_ctx.sessions["reader"]
    reader_token = await MCPAccessTokenService.create(
        reader.id,
        MCPAccessTokenCreate(name="reader", scope=MCPTokenScope.READ_WRITE),
    )
    for token in ("admin", reader_token["token"]):
        denied = await call(app, "create_download_plan", creation, token=token)
        assert denied["isError"]
    assert await DownloadPlan.all().count() == prior_count
    plan_ids = []
    for _ in range(2):
        click = await browser.post(
            path + "/decision",
            json={"request_id": request_id, "approve": True},
        )
        click.raise_for_status()
        receipt = click.json()["data"]
        assert receipt["status"] == "applied"
        plan_ids.append(receipt["outcome"]["plan_id"])

    plan = await DownloadPlan.all().get()
    assert plan_ids == [plan.id, plan.id]
    owner = await User.get(username="subscriber")
    writer = await MCPAccessTokenService.create(
        owner.id,
        MCPAccessTokenCreate(name="replay", scope=MCPTokenScope.READ_WRITE),
    )
    replay = await call(app, "create_download_plan", creation, token=writer["token"])
    assert replay["structuredContent"]["replayed"]
    assert replay["structuredContent"]["plan_id"] == plan.id
    assert await DownloadPlan.all().count() == 1
    assert (await DownloadPlanCreation.all().get()).plan_id == plan.id
    return plan


@pytest.mark.parametrize(
    "scenario", ["simple", "preview", "direct", "question", "nyaa", "edition"]
)
def test_subscription(tmp_path, monkeypatch, scenario):
    async def run():
        async with application() as app:
            provider = await official_provider(tmp_path, monkeypatch)
            graph, downloader, _ = await fixture_plan()
            if scenario in {"simple", "edition"}:
                library = await MediaLib.create(
                    name="TV", lib_type="tv_show", dir="/test/tv", priority=1
                )
                await download.DownloadDirService.upsert("/synthetic/downloads")
                if scenario == "edition":
                    await download.DownloadDirService.upsert("/synthetic/season")
            if scenario in {"nyaa", "edition"}:
                graph.name = "Nyaa"
                await graph.save(update_fields=["name"])
            else:
                node = graph.definition["nodes"][0]
                node["data"]["config"] += (
                    "  uploader:\n"
                    "    type: select\n"
                    "    options: {tea: 喵萌奶茶屋, ani: ANi}\n"
                )
                await graph.save(update_fields=["definition"])
            downloader.config = encrypt_config(downloader.config)
            await downloader.save(update_fields=["config"])
            driver = AsyncMock(spec=DownloaderDriver)
            driver.source_types = frozenset({DownloadSource.MAGNET})
            monkeypatch.setattr(download, "load_driver", Mock(return_value=driver))
            monkeypatch.setattr(
                download,
                "default_download_directory",
                lambda: Path("/synthetic/downloads"),
            )
            notify = Mock()
            monkeypatch.setattr(download, "notify_download_changes", notify)
            catalogue = [
                ("tea", "【喵萌奶茶屋】 Lantern [07][简日双语]"),
                ("ani", "[ANi] Lantern [07][简体内嵌]"),
                ("ani", "[ANi] Lantern [07][简繁内封]"),
            ]

            def search_metadata(_, query, **kwargs):
                words = query.get("keyword", "").casefold().split()
                items = [
                    {
                        "id": str(i),
                        "title": title,
                        "link": "magnet:?xt=urn:btih:" + str(i) * 40,
                    }
                    for i, (group, title) in enumerate(catalogue, 1)
                    if all(word in title.casefold() for word in words)
                    and query.get("uploader", group) == group
                ]
                return {"items": items, "total": len(items)}

            search = AsyncMock(side_effect=search_metadata)
            app.ctx.flow_engine = SimpleNamespace(execute=search)
            app.config.update(
                AUTO_EXTEND=False, TOUCHUP=False, AUTH_EXCLUDE_PATHS=["/auth/login"]
            )
            app.blueprint(auth, url_prefix="/_api/auth")
            app.blueprint(assistant, url_prefix="/_api/assistant")
            app.register_middleware(on_request, "request")
            app.register_middleware(on_response, "response")
            app.error_handler.add(Exception, error_handler)
            app.asgi = True
            await app._startup()
            await UserService.create("subscriber", "fixture-password", UserRole.ADMIN)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://kaloscope.test"
            ) as browser:
                login = await browser.post(
                    "/_api/auth/login",
                    data={"username": "subscriber", "password": "fixture-password"},
                )
                login.raise_for_status()
                assert browser.cookies.get(SESSION_ID)
                model = {"provider_id": provider.id, "model_id": provider.model_ids[0]}
                chat = await browser.post(
                    "/_api/assistant/conversations", json={"model": model}
                )
                chat.raise_for_status()
                chat_id = chat.json()["data"]["id"]
                endpoint = f"/_api/assistant/conversations/{chat_id}"
                context = {"page_type": "download_plans", "route": "/downloads/plans"}
                settings = (
                    f"使用 {graph.name}、Fixture downloader，每 6 小时检查一次，"
                    "每次最多下载 2 个，保存到 /synthetic/downloads，无需入库。"
                )
                instruction = {
                    "simple": "",
                    "edition": "",
                    "preview": (
                        "不要设筛选条件，总共最多 10 个。"
                        "请把下载计划准备好，让我在界面里点确认后创建。"
                    ),
                    "direct": "只要喵萌奶茶屋发布的资源，总共最多 20 个，请直接创建。",
                    "question": (
                        "请直接创建订阅。发布组还没定，请先用一张问题卡问我发布组。"
                        "等我回答后按我的条件继续创建。"
                    ),
                }["question" if scenario == "nyaa" else scenario]
                stream = await browser.post(
                    endpoint + "/chat",
                    json={
                        "model": model,
                        "request_id": f"subscription-{scenario}",
                        "context": context,
                        "prompt": (
                            "帮我订阅《Lantern》"
                            if scenario in {"simple", "edition"}
                            else "帮我订阅 Lantern。" + settings + instruction
                        ),
                    },
                )
                results = tool_results(stream)
                if scenario != "preview":
                    cards = [
                        item
                        for item in results
                        if item["name"] == "ask_user" and item["ok"]
                    ]
                    assert cards, [item["name"] for item in results]
                    card = cards[-1]
                    print(
                        "DeepSeek before answers:", [item["name"] for item in results]
                    )
                    assert not await DownloadPlan.all().exists()
                    notify.assert_not_called()
                    assert not await DownloadTask.all().exists()
                    assert len(cards) == 1 and card["data"]["subscription"]
                    answers = []
                    for q in card["data"]["questions"]:
                        options = q["options"]
                        if q["field"] == "release":
                            if scenario in {"nyaa", "question"}:
                                answers.append(
                                    {
                                        "id": q["id"],
                                        "custom": "只要喵萌奶茶屋，总计最多 20 个。",
                                    }
                                )
                                continue
                            wanted = (
                                "ANi · 简体内嵌"
                                if scenario == "edition"
                                else "喵萌奶茶屋"
                            )
                            option = next(o for o in options if o["label"] == wanted)
                        elif q["field"] == "destination":
                            option = next(
                                o for o in options if o["value"] == library.id
                            )
                        elif (
                            q["field"] in {"library", "directory"}
                            and scenario == "edition"
                        ):
                            answers.append(
                                {
                                    "id": q["id"],
                                    "custom": "TV"
                                    if q["field"] == "library"
                                    else "/custom/season",
                                }
                            )
                            continue
                        else:
                            option = options[0]
                        answers.append({"id": q["id"], "selected": [option["id"]]})
                    answer = await browser.post(
                        endpoint + f"/questions/{card['id']}/answer",
                        json={
                            "version": card["data"]["version"],
                            "items": answers,
                        },
                    )
                    answer.raise_for_status()
                    assert answer.json()["data"]["card"]["state"] == "answered"
                    assert not await DownloadPlan.all().exists()
                    search.reset_mock()
                    continuation = await browser.post(
                        endpoint + "/chat",
                        json={
                            "model": model,
                            "request_id": f"answer-{card['id']}",
                            "answer_to": card["id"],
                            "context": context,
                            "prompt": "继续",
                        },
                    )
                    results = tool_results(continuation)
                names = [item["name"] for item in results if item["ok"]]
                print(f"DeepSeek {model['model_id']} {scenario} tools: {names}")
                assert "search_indexer" in names and search.await_count >= 1
                reviews = [
                    item
                    for item in results
                    if item["name"] == "validate_download_plan"
                    and item["ok"]
                    and item["data"]["valid"]
                ]
                assert reviews, [
                    {
                        "name": item["name"],
                        "plan": (item.get("data") or {}).get("plan"),
                        "issues": (item.get("data") or {}).get("issues"),
                        "status": (item.get("data") or {}).get("status"),
                    }
                    for item in results
                    if item["name"] in {"search_indexer", "validate_download_plan"}
                ]
                review = reviews[-1]
                stored = await AssistantToolCall.get(
                    call_id=review["id"], turn__conversation_id=chat_id
                )
                request_id = review["data"]["request_id"]
                assert stored.result["data"]["request_id"] == request_id
                if scenario == "preview":
                    assert not await DownloadPlan.all().exists()
                    notify.assert_not_called()
                else:
                    created = [
                        item["data"]
                        for item in results
                        if item["ok"]
                        and item["name"] == "create_download_plan"
                        and item["data"].get("created")
                    ]
                    assert created, [
                        (
                            item["name"],
                            item["data"].get("outcome"),
                            item["data"].get("issues"),
                        )
                        for item in results
                        if item["name"]
                        in {"validate_download_plan", "create_download_plan"}
                        and item["ok"]
                    ]
                    assert (await DownloadPlan.all().get()).id == created[-1]["plan_id"]
                    notify.assert_called_once()

                plan = await check_receipt(
                    app,
                    browser,
                    review,
                    "reviewed" if scenario == "preview" else "applied",
                )
                assert plan.graph_id == graph.id and plan.downloader_id == downloader.id
                assert plan.dir == (
                    "/custom/season"
                    if scenario == "edition"
                    else "/synthetic/downloads"
                )
                if scenario == "edition":
                    assert plan.keyword == "Lantern ANi 简体内嵌" and not plan.filters
                elif scenario == "nyaa":
                    assert all(
                        term in plan.keyword for term in ("Lantern", "喵萌奶茶屋")
                    )
                    assert not plan.filters
                else:
                    assert plan.keyword == "Lantern"
                    assert (plan.filters or {}) == (
                        {} if scenario == "preview" else {"uploader": "tea"}
                    )
                assert any(
                    entry.args[1]["keyword"] == plan.keyword
                    and all(
                        entry.args[1].get(k) == v
                        for k, v in (plan.filters or {}).items()
                    )
                    for entry in search.await_args_list
                )
                if scenario in {"simple", "edition"}:
                    assert plan.transfer_lib_id == library.id
                    assert plan.transfer_method == "hardlink"
                    assert plan.batch_limit == 12
                    assert (plan.sub_pattern, plan.sub_repl) == (
                        "^(.+/)?",
                        "{{title}}/",
                    )
                    assert plan.total_limit is None
                else:
                    assert plan.transfer_lib_id is None
                if scenario not in {"simple", "edition"}:
                    assert (plan.interval_num, plan.batch_limit, plan.total_limit) == (
                        6,
                        2,
                        10 if scenario == "preview" else 20,
                    )
                assert plan.creation_receipt["first_execution"]["status"] == "queued"
                notify.assert_called_once()
                driver.add.assert_not_awaited()
                assert not await DownloadTask.all().exists()

    asyncio.run(run())
