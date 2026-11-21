"""Evaluate real-model subscription reviews through the authenticated web API."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from app.core.constants import SESSION_ID
from app.core.dl.config import encrypt_config
from app.core.dl.driver import DownloaderDriver, DownloadSource
from app.core.exceptions import error_handler
from app.core.middleware import on_request, on_response
from app.models.assistant import AssistantToolCall
from app.models.download import DownloadPlan, DownloadPlanCreation, DownloadTask
from app.models.user import UserRole
from app.routes.assistant import assistant
from app.routes.auth import auth
from app.services import download
from app.services.user import UserService
from app.utils.json import loads
from tests.test_agent_live import official_provider
from tests.test_agent_live import pytestmark as pytestmark
from tests.test_download_plan_authoring import fixture_plan
from tests.test_mcp import application


def test_subscription(tmp_path, monkeypatch):
    async def run():
        async with application() as app:
            provider = await official_provider(tmp_path, monkeypatch)
            graph, downloader, _ = await fixture_plan()
            downloader.config = encrypt_config(downloader.config)
            await downloader.save(update_fields=["config"])
            driver = AsyncMock(spec=DownloaderDriver)
            driver.source_types = frozenset({DownloadSource.MAGNET})
            monkeypatch.setattr(download, "load_driver", Mock(return_value=driver))
            notify = Mock()
            monkeypatch.setattr(download, "notify_download_changes", notify)
            search = AsyncMock(
                return_value={
                    "items": [
                        {
                            "id": "episode-one",
                            "title": "Lantern Episode One",
                            "link": "magnet:?xt=urn:btih:" + "a" * 40,
                        }
                    ],
                    "total": 1,
                }
            )
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
                stream = await browser.post(
                    f"/_api/assistant/conversations/{chat_id}/chat",
                    json={
                        "model": model,
                        "request_id": "subscription-preview",
                        "context": {
                            "page_type": "download_plans",
                            "route": "/downloads/plans",
                        },
                        "prompt": (
                            "帮我订阅 Lantern。用 Published indexer 搜索核对，"
                            "不要设筛选条件。每 6 小时检查一次，每次最多下载 2 个，"
                            "总共最多 10 个。使用 Fixture downloader，"
                            "保存到 /synthetic/downloads，无需入库。"
                            "请把这份下载计划准备好，让我在界面里点确认后创建。"
                        ),
                    },
                )
                stream.raise_for_status()
                assert "event: done\n" in stream.text
                results = [
                    loads(block.split("\ndata: ", 1)[1])
                    for block in stream.text.split("\n\n")
                    if block.startswith("event: tool_result\n")
                ]
                names = [item["name"] for item in results if item["ok"]]
                print(f"DeepSeek {model['model_id']} subscription tools: {names}")
                assert "search_indexer" in names and search.await_count >= 1
                reviews = [
                    item
                    for item in results
                    if item["name"] == "validate_download_plan"
                    and item["ok"]
                    and item["data"]["valid"]
                ]
                assert reviews, names
                review = reviews[-1]
                stored = await AssistantToolCall.get(
                    call_id=review["id"], turn__conversation_id=chat_id
                )
                request_id = review["data"]["request_id"]
                assert stored.result["data"]["request_id"] == request_id
                assert not await DownloadPlan.all().exists()
                notify.assert_not_called()

                path = f"/_api/assistant/reviews/{review['id']}"
                shown = await browser.get(path)
                shown.raise_for_status()
                card = shown.json()["data"]
                assert card["status"] == "reviewed" and card["request_id"] == request_id
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
                assert (await DownloadPlanCreation.all().get()).plan_id == plan.id
                assert plan.graph_id == graph.id and plan.downloader_id == downloader.id
                assert plan.keyword == "Lantern" and plan.dir == "/synthetic/downloads"
                assert not plan.filters and plan.transfer_lib_id is None
                assert (plan.interval_num, plan.batch_limit, plan.total_limit) == (
                    6,
                    2,
                    10,
                )
                assert plan.creation_receipt["first_execution"]["status"] == "queued"
                notify.assert_called_once()
                driver.add.assert_not_awaited()
                assert not await DownloadTask.all().exists()

    asyncio.run(run())
