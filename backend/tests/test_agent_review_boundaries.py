"""Test read-only answer continuations and the final approval checks under a lock."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import KaloscopeConfig
from app.core.dl.config import encrypt_config
from app.models.agent_actions import AdminChange, WorkspaceQuery
from app.models.download import Downloader, DownloadPlanAttempt, DownloadTask
from app.models.media import MediaEvent, MediaFormat, MediaLib
from app.services import agent_actions, media
from tests.test_agent_actions import decide
from tests.test_agent_actions import isolate as isolate
from tests.test_assistant_persistence import database
from tests.test_download_plan_authoring import CONFIG
from tests.test_services_assistant import (
    call_delta,
    chunk,
    collect,
    configure,
    make_turn,
    response,
)
from tests.test_services_assistant import stored_context as stored_context
from tests.test_services_media_reading import _item, _opf


@pytest.mark.parametrize("subscription", [True, False], ids=["subscription", "lookup"])
def test_download_discovery(monkeypatch, subscription):
    calls = (
        [
            ("discover_actions", {"domain": "search"}),
            ("discover_actions", {"domain": "downloads"}),
            ("discover_actions", {"domain": "media"}),
            ("list_media_items", {}),
        ]
        if subscription
        else [
            ("list_download_plans", {}),
            ("discover_actions", {"domain": "downloads"}),
        ]
    )
    catalogs = []

    def handler(request):
        body = json.loads(request.content)
        catalogs.append({schema["function"]["name"] for schema in body["tools"]})
        if len(catalogs) > len(calls):
            return response([chunk({"role": "assistant", "content": "Ready."}, "stop")])
        name, args = calls[len(catalogs) - 1]
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            call_delta(
                                name, json.dumps(args), id=f"lookup-{len(catalogs)}"
                            )
                        ],
                    },
                    "tool_calls",
                )
            ]
        )

    async def run():
        async with database() as (owner, _):
            configure(monkeypatch, handler)
            prompt = "帮我订阅 Lantern" if subscription else "查一下下载计划"
            events = await collect(make_turn(prompt), owner)
            assert events[-1].event == "done"
            results = [event.data for event in events if event.event == "tool_result"]
            assert [r["name"] for r in results if r["ok"]] == [
                name for name, _ in calls
            ]
            if not subscription:
                assert "diagnose_download_ingestion" in catalogs[-1]
                assert "validate_download_plan" not in catalogs[-1]
                return
            required = {
                "get_download_plan_options",
                "search_indexer",
                "validate_download_plan",
                "ask_user",
            }
            assert all(required <= catalogs[i] for i in (0, 1, 2, 4))
            assert "validate_download_plan" not in catalogs[3]
            for step in (0, 1):
                assert set(results[step]["data"]["tools"]) == catalogs[step + 1]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("domain", "tool", "arguments"),
    [
        ("downloads", "diagnose_download_ingestion", {"attempt_id": 1}),
        ("workspace", "inspect_workspace", {"topic": "actions"}),
    ],
)
def test_answer_diagnostics(monkeypatch, domain, tool, arguments):
    schemas = []

    def handler(request):
        schemas.append(
            {
                s["function"]["name"]
                for s in json.loads(request.content).get("tools", [])
            }
        )
        if len(schemas) > 2:
            return response(
                [chunk({"role": "assistant", "content": "Recorded."}, "stop")]
            )
        name, args = (
            ("discover_actions", {"domain": domain})
            if len(schemas) == 1
            else (tool, arguments)
        )
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            call_delta(
                                name, json.dumps(args), id=f"lookup-{len(schemas)}"
                            )
                        ],
                    },
                    "tool_calls",
                )
            ]
        )

    async def run():
        async with database() as (owner, _):
            await DownloadPlanAttempt.create(
                id=1, plan_id=1, workflow_id=1, status="driver_offline"
            )
            configure(monkeypatch, handler)
            turn = make_turn("Check that selection")
            turn.request_id = "answer-" + "1" * 32
            events = await collect(turn, owner)
            assert events[0].data["is_answer_turn"]
            assert tool in schemas[1]
            assert "apply_download_plan_change" not in schemas[1]
            results = [event.data for event in events if event.event == "tool_result"]
            assert set(results[0]["data"]["tools"]) <= schemas[1]
            assert [result["name"] for result in results if result["ok"]] == [
                "discover_actions",
                tool,
            ]
            assert events[-1].event == "done"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("supports", "failed", "status"),
    [(True, False, "up"), (True, True, "unavailable"), (False, False, "unsupported")],
)
def test_health_cleanup(monkeypatch, supports, failed, status):
    async def run():
        async with database() as (owner, _):
            monkeypatch.setattr(KaloscopeConfig.get(), "secret_key_enabled", True)
            encrypted = encrypt_config(CONFIG)
            assert encrypted != CONFIG
            record = await Downloader.create(
                name="Fixture", priority=1, config=encrypted
            )
            driver = SimpleNamespace(
                supports_version=supports,
                version=AsyncMock(
                    return_value="1.2", side_effect=RuntimeError if failed else None
                ),
                close=AsyncMock(),
            )
            factory = Mock(return_value=driver)
            monkeypatch.setattr(agent_actions, "load_driver", factory)
            report = await agent_actions.inspect_workspace(
                WorkspaceQuery(topic="downloaders"), owner
            )
            assert report["items"] == [
                {
                    "id": record.id,
                    "name": "Fixture",
                    "status": status,
                    "version": "1.2" if status == "up" else None,
                }
            ]
            factory.assert_called_once_with(CONFIG)
            driver.close.assert_awaited_once_with()

    asyncio.run(run())


@pytest.mark.parametrize("action", ["delete_library", "edit_metadata"])
def test_change_waiting_for_lock(tmp_path, monkeypatch, action):
    async def run():
        async with database() as (owner, _):
            item = await _item(tmp_path, MediaFormat.TXT)
            lib = await MediaLib.get(id=item.lib_id)
            path = Path(item.dir) / "metadata.opf"
            path.write_bytes(_opf("Reviewed"))
            if action == "edit_metadata":
                change = AdminChange(action=action, target_id=item.id, title="Approved")
            else:
                change = AdminChange(action=action, target_id=lib.id)
            card = await agent_actions.review_action(change, owner)
            locking = asyncio.Event()
            actual_lock = media.library_lock

            @asynccontextmanager
            async def lock(directory):
                locking.set()
                async with actual_lock(directory):
                    yield

            monkeypatch.setattr(media, "library_lock", lock)
            async with actual_lock(lib.dir):
                pending = asyncio.create_task(decide(card, owner))
                await asyncio.wait_for(locking.wait(), 5)
                if action == "edit_metadata":
                    path.write_bytes(
                        _opf("Concurrent edit", "<dc:publisher>Preserve</dc:publisher>")
                    )
                else:
                    driver = await Downloader.create(
                        name="Fixture", priority=1, config=CONFIG
                    )
                    await DownloadTask.create(
                        downloader=driver,
                        dir=str(tmp_path),
                        name="Late transfer",
                        state="paused",
                        transfer_lib=lib,
                    )
            result = await pending
            assert result["status"] == "conflict"
            assert (await decide(card, owner))["status"] == "conflict"
            assert await MediaLib.exists(id=lib.id)
            if action == "edit_metadata":
                assert path.read_bytes() == _opf(
                    "Concurrent edit", "<dc:publisher>Preserve</dc:publisher>"
                )
                assert not await MediaEvent.filter(event_type="metadata").exists()
            else:
                assert await DownloadTask.filter(
                    name="Late transfer", transfer_lib_id=lib.id
                ).exists()
                assert path.read_bytes() == _opf("Reviewed")

    asyncio.run(run())
