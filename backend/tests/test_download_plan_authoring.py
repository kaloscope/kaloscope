"""Tests for shared download-plan validation, creation and first-attempt recovery."""

import asyncio
import inspect
from datetime import UTC, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from tortoise import timezone
from tortoise.exceptions import IntegrityError

from app.core.ai import history, tools
from app.core.config import KaloscopeConfig
from app.core.dl import syncer
from app.core.dl.driver import DownloadState
from app.core.exceptions import BadRequestException, ForbiddenException
from app.models.assistant import (
    AssistantConversation,
    AssistantToolCall,
    AssistantTurn,
    ConversationCreate,
)
from app.models.download import (
    DownloadDir,
    Downloader,
    DownloadPlan,
    DownloadPlanCheck,
    DownloadPlanCreate,
    DownloadPlanHistory,
    DownloadPlanQuery,
    DownloadTask,
    TransferMethod,
)
from app.models.flow import FlowGraph, FlowLog, GraphState
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import MediaLib
from app.models.user import Permissions, PermType, UserPermission, UserRole
from app.routes import download as download_routes
from app.services import download as service
from app.services.assistant import SYSTEM_PROMPT, AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, completion_client, database, request
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_services_download import DraftDriver

CONFIG = """driver: rpc
name: Fixture
protocol: http
host: downloader.invalid
port: 9000
auth:
  password: do-not-expose
methods:
  add_link:
    get: /add
"""
SEARCH_CONFIG = """keyword:
  required: true
filters:
  category:
    type: select
    options: {movie: Movies, tv: TV}
  quality:
    type: checkbox
    options: {hd: HD, uhd: UHD}
  date_range:
    type: calendar-range
"""


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(
        KaloscopeConfig,
        "_config",
        SimpleNamespace(secret_key_enabled=False, workspace={"TEMP": str(tmp_path)}),
    )
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport,
        "handle_async_request",
        AsyncMock(side_effect=AssertionError("No network")),
    )
    monkeypatch.setattr(service, "notify_download_changes", Mock())


async def fixture_plan(**overrides):
    """Create synthetic published indexer and downloader inputs.

    Args:
        **overrides: Plan fields that differ from the valid fixture.

    Returns:
        Graph, downloader and a shared creation request.
    """
    graph = await FlowGraph.create(
        name="Published indexer",
        category="indexer",
        state="modified",
        definition={
            "nodes": [
                {
                    "id": "search",
                    "data": {"$schema": "search_start", "config": SEARCH_CONFIG},
                }
            ],
            "edges": [],
        },
        draft={"nodes": [], "edges": []},
    )
    downloader = await Downloader.create(
        name="Fixture downloader", config=CONFIG, priority=1
    )
    body = DownloadPlanCreate.model_validate(
        {
            "request_id": str(uuid4()),
            "plan": {
                "graph_id": graph.id,
                "downloader_id": downloader.id,
                "dir": "/synthetic/downloads",
                "keyword": "Lantern",
                "filters": {"category": "movie"},
                "interval_num": 6,
                "batch_limit": 2,
                "total_limit": 10,
                **overrides,
            },
        }
    )
    return graph, downloader, body


def test_options(monkeypatch):
    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan()
            await Downloader.create(name="Second", config=CONFIG, priority=2)
            await DownloadDir.create(path="/synthetic/recent", last_used=timezone.now())
            before = await FlowGraph.all().values()
            first = await service.DownloadPlanService.authoring_options(
                graph.id, owner, 1, 1
            )
            second = await service.DownloadPlanService.authoring_options(
                graph.id, owner, 2, 1
            )
            empty = await service.DownloadPlanService.authoring_options(
                graph.id, owner, 3, 1
            )
            assert first["published"] and first["search"]["keyword"]["required"]
            assert first["downloaders"]["total"] == 2
            assert first["downloaders"]["has_more"]
            assert not second["downloaders"]["has_more"]
            assert empty["downloaders"]["items"] == []
            assert first["downloaders"]["items"][0]["source_types"] == ["magnet"]
            assert (
                "do-not-expose" not in str(first)
                and "config" not in first["downloaders"]["items"][0]
            )
            checked = await service.DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=body.plan), owner
            )
            assert (
                checked["valid"]
                and checked["first_execution"] == "queued_after_creation"
            )
            assert checked["timezone"] == "UTC" and checked["interval_unit"] == "hours"
            assert await FlowGraph.all().values() == before
            assert (
                await DownloadPlan.all().count()
                == await DownloadTask.all().count()
                == await FlowLog.all().count()
                == 0
            )
            service.notify_download_changes.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "changes,path",
    [
        ({"keyword": " "}, "keyword"),
        ({"dir": " "}, "dir"),
        ({"filters": {"page_num": 2}}, "filters.page_num"),
        ({"filters": {"$start": "details_start"}}, "filters.$start"),
        ({"filters": {"unknown": "x"}}, "filters.unknown"),
        ({"filters": {"category": "unknown"}}, "filters.category"),
        ({"filters": {"category": ["movie"]}}, "filters.category"),
        ({"filters": {"quality": "hd"}}, "filters.quality"),
        ({"filters": {"quality": [{}]}}, "filters.quality"),
        ({"downloader_id": 999}, "downloader_id"),
        (
            {
                "interval_start": "2099-01-01T00:00:00",
                "interval_end": "2100-01-01T00:00:00Z",
            },
            "interval_start",
        ),
        ({"interval_end": "2020-01-01T00:00:00Z"}, "interval_end"),
    ],
)
def test_invalid_settings(changes, path):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan(**changes)
            result = await service.DownloadPlanService.create_once(body, owner)
            assert not result["created"]
            assert path in {item["path"] for item in result["issues"]}
            assert not await DownloadPlan.exists()

    asyncio.run(run())


@pytest.mark.parametrize("change", ["draft", "other_category", "no_search", "preview"])
def test_published_entry(change):
    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan()
            if change == "draft":
                graph.state = GraphState.DRAFT
            elif change == "other_category":
                graph.category = "schedule"
            elif change == "no_search":
                graph.definition = {"nodes": [], "edges": []}
            else:
                graph.definition["nodes"][0]["data"]["config"] += (
                    "\nonly_preview: true\n"
                )
            await graph.save()
            result = await service.DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=body.plan), owner
            )
            assert not result["valid"]
            assert "graph_id" in {issue["path"] for issue in result["issues"]}

    asyncio.run(run())


@pytest.mark.parametrize(
    "link,valid,warning",
    [
        ("a" * 40, True, False),
        ("  " + "a" * 40 + "\n", True, False),
        ("magnet:?xt=urn:btih:" + "a" * 40, True, False),
        ("https://example.invalid/movie.torrent", True, True),
        ("https://[broken", False, False),
        ("https://", False, False),
        ("magnet:?dn=missing-hash", False, False),
        ("ftp://example.invalid/movie.torrent", False, False),
        (None, False, False),
    ],
)
def test_sample(link, valid, warning):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan(
                filters={"quality": ["hd"], "date_range": ["2026-01-01", "2026-01-31"]}
            )
            sample = {
                "items": [{"id": "film-1", "title": "Lantern", "link": link}],
                "total": 1,
            }
            result = await service.DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=body.plan, sample=sample), owner
            )
            assert result["valid"] is valid, result
            assert (
                any("torrent content" in text for text in result["warnings"]) is warning
            )

    asyncio.run(run())


def test_create_recovery(monkeypatch):
    async def run():
        async with database() as (owner, other):
            graph, _, body = await fixture_plan(
                interval_start="2099-01-01T08:00:00+08:00"
            )
            service.notify_download_changes.side_effect = RuntimeError(
                "Response lost after commit"
            )
            with pytest.raises(RuntimeError):
                await service.DownloadPlanService.create_once(body, owner)
            service.notify_download_changes.side_effect = None
            result = await service.DownloadPlanService.create_once(body, owner)
            assert result["replayed"] and result["created"]
            assert result["first_execution"]["status"] == "waiting"
            assert result["plan"]["interval_start"].hour == 0
            assert result["plan"]["interval_start"].utcoffset().total_seconds() == 0
            same = body.model_copy(
                update={
                    "plan": body.plan.model_copy(
                        update={
                            "interval_start": body.plan.interval_start.astimezone(UTC)
                        }
                    )
                }
            )
            assert (await service.DownloadPlanService.create_once(same, owner))[
                "plan_id"
            ] == result["plan_id"]
            changed = body.model_copy(
                update={"plan": body.plan.model_copy(update={"keyword": "Different"})}
            )
            with pytest.raises(BadRequestException):
                await service.DownloadPlanService.create_once(changed, owner)
            recovered = await service.DownloadPlanService.find_creation(
                body.request_id, owner
            )
            assert recovered["plan_id"] == result["plan_id"]
            assert await service.DownloadPlanService.find_creation(
                body.request_id, other
            ) == {"created": False}
            graph.state = GraphState.DRAFT
            await graph.save()
            assert (await service.DownloadPlanService.create_once(body, owner))[
                "replayed"
            ]
            assert await DownloadPlan.all().count() == 1
            assert (
                await DownloadTask.all().count()
                == await DownloadPlanHistory.all().count()
                == 0
            )

    asyncio.run(run())


def test_concurrent_creation():
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            results = await asyncio.gather(
                *(
                    service.DownloadPlanService.create_once(body, owner)
                    for _ in range(4)
                )
            )
            assert len({result["plan_id"] for result in results}) == 1
            assert sum(not result["replayed"] for result in results) == 1
            service.notify_download_changes.assert_called_once()

    asyncio.run(run())


def test_integrity_failure(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            create = AsyncMock(side_effect=IntegrityError("Foreign key changed"))
            monkeypatch.setattr(DownloadPlan, "create", create)
            with pytest.raises(IntegrityError):
                await service.DownloadPlanService.create_once(body, owner)
            create.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize(
    "status", ["completed", "partial", "failed", "unconfirmed", "interrupted"]
)
def test_first_attempt(monkeypatch, status):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            result = await service.DownloadPlanService.create_once(body, owner)
            driver = SimpleNamespace(supports_version=False, close=AsyncMock())
            monkeypatch.setattr(syncer, "load_driver", lambda _: driver)
            outcome = {"status": status, "added": 0, "errors": 1}
            execute = AsyncMock(return_value=outcome)
            if status == "interrupted":
                execute.side_effect = asyncio.CancelledError
            monkeypatch.setattr(syncer, "execute_download_plan", execute)
            if status == "interrupted":
                with pytest.raises(asyncio.CancelledError):
                    await syncer.check_download_plans(first_only=True)
            else:
                await asyncio.gather(
                    syncer.check_download_plans(first_only=True),
                    syncer.check_download_plans(first_only=True),
                )
            await syncer.check_download_plans(first_only=True)
            await syncer.check_download_plans()
            replay = await service.DownloadPlanService.create_once(body, owner)
            assert replay["plan_id"] == result["plan_id"]
            assert replay["first_execution"]["status"] == (
                "unconfirmed" if status == "interrupted" else status
            )
            execute.assert_awaited_once()
            assert driver.close.await_count >= 1

    asyncio.run(run())


def test_future_attempt(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan(
                interval_start=timezone.now() + timedelta(days=1)
            )
            created = await service.DownloadPlanService.create_once(body, owner)
            execute = AsyncMock(
                return_value={"status": "completed", "added": 0, "errors": 0}
            )
            monkeypatch.setattr(syncer, "execute_download_plan", execute)
            await syncer.check_download_plans()
            execute.assert_not_awaited()
            await DownloadPlan.filter(id=created["plan_id"]).update(
                interval_start=timezone.now() - timedelta(seconds=1)
            )
            await syncer.check_download_plans()
            execute.assert_awaited_once()

    asyncio.run(run())


def test_permissions():
    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan()
            created = await service.DownloadPlanService.create_once(body, owner)
            owner.role, owner.perms = UserRole.USER, Permissions(indexer_ids=[graph.id])
            assert (
                await service.DownloadPlanService.get_creation(
                    created["plan_id"], owner
                )
            )["plan_id"] == created["plan_id"]
            for operation in [
                service.DownloadPlanService.authoring_options(graph.id, owner),
                service.DownloadPlanService.validate_creation(
                    DownloadPlanCheck(plan=body.plan), owner
                ),
                service.DownloadPlanService.create_once(body, owner),
            ]:
                with pytest.raises(ForbiddenException):
                    await operation
            owner.perms = Permissions()
            with pytest.raises(ForbiddenException):
                await service.DownloadPlanService.get_creation(
                    created["plan_id"], owner
                )

    asyncio.run(run())


def test_mcp():
    async def run():
        async with application() as app:
            graph, _, body = await fixture_plan()
            args = body.model_dump(mode="json")
            for account in ("reader", "admin"):
                denied = await call(
                    app, "create_download_plan", {"creation": args}, token=account
                )
                assert (
                    denied["isError"]
                    and "only permits read-only" in denied["content"][0]["text"]
                )
            tokens = {}
            for account in ("reader", "admin"):
                tokens[account] = (
                    await MCPAccessTokenService.create(
                        app.shared_ctx.sessions[account].id,
                        MCPAccessTokenCreate(
                            name="plan", scope=MCPTokenScope.READ_WRITE
                        ),
                    )
                )["token"]
            denied = await call(
                app, "create_download_plan", {"creation": args}, token=tokens["reader"]
            )
            assert denied["isError"]
            options = await call(
                app,
                "get_download_plan_options",
                {"workflow_id": graph.id},
                token="admin",
            )
            assert options["structuredContent"]["published"]
            checked = await call(
                app,
                "validate_download_plan",
                {"validation": {"plan": args["plan"]}},
                token="admin",
            )
            assert checked["structuredContent"]["valid"]
            made = await call(
                app, "create_download_plan", {"creation": args}, token=tokens["admin"]
            )
            assert not made.get("isError"), made
            plan_id = made["structuredContent"]["plan_id"]
            replay = await call(
                app, "create_download_plan", {"creation": args}, token=tokens["admin"]
            )
            assert replay["structuredContent"]["replayed"]
            permission = await UserPermission.create(
                user_id=app.shared_ctx.sessions["reader"].id,
                rel_type=PermType.INDEXER,
                rel_id=graph.id,
            )
            allowed = await call(app, "get_download_plan", {"plan_id": plan_id})
            assert allowed["structuredContent"]["workflow_id"] == graph.id
            await permission.delete()
            assert (await call(app, "get_download_plan", {"plan_id": plan_id}))[
                "isError"
            ]
            assert await DownloadPlan.all().count() == 1
            assert await DownloadTask.all().count() == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "state", [DownloadState.REMOTE, DownloadState.SUBMIT_UNKNOWN, "failed"]
)
def test_attempt_outcome(monkeypatch, state):
    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan(batch_limit=1)
            created = await service.DownloadPlanService.create_once(body, owner)
            plan = await DownloadPlan.get(id=created["plan_id"])
            engine = SimpleNamespace(
                execute=AsyncMock(return_value={"items": [{"link": "a" * 40}]})
            )
            monkeypatch.setattr(
                syncer.Sanic,
                "get_app",
                lambda: SimpleNamespace(ctx=SimpleNamespace(flow_engine=engine)),
            )
            driver = DraftDriver(
                fail=state == "failed", state=state if state != "failed" else None
            )
            result = await syncer.execute_download_plan(plan, driver)
            expected = (
                "failed"
                if state == "failed"
                else "unconfirmed"
                if state == DownloadState.SUBMIT_UNKNOWN
                else "completed"
            )
            assert result["status"] == expected
            assert result["added"] == (0 if state == "failed" else 1)
            assert result["errors"] == (1 if state == "failed" else 0)
            engine.execute.assert_awaited_once_with(
                graph_id=graph.id,
                bootparams={
                    "$start": "search_start",
                    "page_num": 1,
                    "page_size": 1,
                    "keyword": "Lantern",
                    "category": "movie",
                },
            )
            assert not await DownloadTask.filter(state=DownloadState.COMPLETED).exists()

    asyncio.run(run())


def test_driver_failure(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            result = await service.DownloadPlanService.create_once(body, owner)
            driver = SimpleNamespace(
                supports_version=True,
                version=AsyncMock(side_effect=RuntimeError("offline")),
                close=AsyncMock(),
            )
            monkeypatch.setattr(syncer, "load_driver", lambda _: driver)
            await syncer.check_download_plans(first_only=True)
            receipt = await service.DownloadPlanService.get_creation(
                result["plan_id"], owner
            )
            assert receipt["first_execution"] == {
                "status": "failed",
                "added": 0,
                "errors": 1,
            }
            driver.close.assert_awaited_once()
            assert (await service.DownloadPlanService.create_once(body, owner))[
                "replayed"
            ]

    asyncio.run(run())


def test_expired_replay(monkeypatch):
    async def run():
        async with database() as (owner, _):
            expires = timezone.now() + timedelta(hours=1)
            _, _, body = await fixture_plan(interval_end=expires)
            made = await service.DownloadPlanService.create_once(body, owner)
            monkeypatch.setattr(
                service.timezone, "now", lambda: expires + timedelta(hours=1)
            )
            assert (await service.DownloadPlanService.create_once(body, owner))[
                "plan_id"
            ] == made["plan_id"]
            another = body.model_copy(update={"request_id": uuid4()})
            assert not (await service.DownloadPlanService.create_once(another, owner))[
                "created"
            ]

    asyncio.run(run())


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
@pytest.mark.parametrize("revoked", ["workflow", "library"])
def test_assistant_generation(monkeypatch, verbose, revoked):
    streams = []
    fixture = {}
    identities = []

    def handler(upstream):
        body = loads(upstream.content)
        assert history.context_size(body["messages"], body.get("tools", [])) <= 27904
        if not body.get("stream"):
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": dumps(
                                    {
                                        "goals": ["Create one plan"],
                                        "source_turn_ids": loads(
                                            body["messages"][-1]["content"]
                                        )["turn_ids"],
                                    }
                                ).decode(),
                            },
                        }
                    ]
                },
            )
        streams.append(body)
        step = len(streams)
        results = []
        for item in body["messages"]:
            if item.get("role") == "tool":
                results.append(loads(item["content"]))
            elif item.get("content", "").startswith("Completed tool exchange"):
                results.extend(loads(item["content"].split("\n", 1)[1])["tools"])
        if step == 1:
            name, args = "list_workflows", {"category": "indexer"}
        elif step == 2:
            name, args = (
                "get_download_plan_options",
                {"workflow_id": fixture["graph_id"]},
            )
        elif step == 3:
            assert "validate_download_plan" in {
                item["function"]["name"] for item in body["tools"]
            }
            name, args = "validate_download_plan", {"plan": fixture["plan"]}
        elif step in (4, 5):
            if step == 4:
                checked = next(
                    item["data"]
                    for item in results
                    if item.get("data", {}).get("request_id")
                )
                identities.append(checked["request_id"])
            name, args = "create_download_plan", {"request_id": identities[0]}
        else:
            return response(
                [chunk({"role": "assistant", "content": "Private plan facts"}, "stop")]
            )
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "content": "Reviewing the plan. " * 400 if verbose else "",
                        "tool_calls": [
                            call_delta(name, dumps(args).decode(), id=f"plan-{step}")
                        ],
                    }
                ),
                chunk(finish="tool_calls"),
            ]
        )

    configure(monkeypatch, handler)

    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan()
            lib = await MediaLib.create(
                name="Library", lib_type="movie", dir="/synthetic/library", priority=1
            )
            fixture.update(
                graph_id=graph.id,
                plan={
                    **body.plan.model_dump(mode="json", exclude={"id"}),
                    "transfer_lib_id": lib.id,
                    "transfer_method": "hardlink",
                },
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request(prompt="Validate and create this download plan."),
                    owner,
                )
            ]
            assert events[-1].event == "done", events[-1]
            outcomes = [e.data for e in events if e.event == "tool_result"]
            assert len(outcomes) == 5 and all(item["ok"] for item in outcomes), outcomes
            assert outcomes[3]["data"]["plan_id"] == outcomes[4]["data"]["plan_id"]
            assert outcomes[4]["data"]["replayed"]
            turn = await AssistantTurn.get(request_id="request")
            assert set(turn.resource_ids) == {graph.id, f"media_lib:{lib.id}"}
            assert await DownloadPlan.all().count() == 1
            assert await DownloadTask.all().count() == await FlowLog.all().count() == 0
            assert not any(
                item["function"]["name"] == "validate_download_plan"
                for item in tools.tool_schemas(owner)
            )
            await AssistantConversation.filter(id=conversation["id"]).update(
                summary={"goals": ["Private plan summary"]},
                summary_resources=turn.resource_ids,
                summary_through=turn.id,
            )
            owner.role, owner.perms = (
                UserRole.USER,
                Permissions(
                    indexer_ids=[] if revoked == "workflow" else [graph.id],
                    media_lib_ids=[] if revoked == "library" else [lib.id],
                ),
            )
            denied = (await Sessions.turns(conversation["id"], owner))["items"][0]
            assert denied["content"] == "" and denied["tools"] == []
            original = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": turn.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="history",
            )
            assert original["error"]["code"] == "permission_denied"
            current = await Sessions.begin(conversation["id"], request("next"), owner)
            messages, _, _ = await history.prepare_context(
                completion_client(AsyncMock()),
                current,
                owner,
                SYSTEM_PROMPT,
                tools.tool_schemas(owner),
                None,
            )
            assert "Private plan" not in dumps(messages).decode()
            assert (await Sessions.owned(conversation["id"], owner)).summary == {}

    asyncio.run(run())


def test_retry_chain():
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            first = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="first",
                prompt="Create",
                status="error",
            )
            checked = await service.DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=body.plan), owner
            )
            await AssistantToolCall.create(
                turn_id=first.id,
                call_id="validate",
                name="validate_download_plan",
                arguments="{}",
                status="complete",
                result={"ok": True, "data": checked},
            )
            args = dumps({"request_id": checked["request_id"]}).decode()
            await AssistantToolCall.create(
                turn_id=first.id,
                call_id="create",
                name="create_download_plan",
                arguments=args,
                status="running",
            )
            created = await tools.execute_tool(
                "create_download_plan",
                args,
                owner,
                conversation_id=conversation["id"],
                call_id="create",
            )
            assert created["ok"]
            retry = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="retry",
                prompt="Create",
                retry_of=first.id,
            )
            unrelated = dumps({"request_id": str(uuid4())}).decode()
            await AssistantToolCall.create(
                turn_id=retry.id,
                call_id="retry-call",
                name="create_download_plan",
                arguments=unrelated,
            )
            recovered = await tools.execute_tool(
                "create_download_plan",
                unrelated,
                owner,
                conversation_id=conversation["id"],
                call_id="retry-call",
            )
            assert (
                recovered["data"]["replayed"]
                and recovered["data"]["plan_id"] == created["data"]["plan_id"]
            )
            assert await DownloadPlan.all().count() == 1
            another = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            absent = await tools.execute_tool(
                "create_download_plan",
                args,
                owner,
                conversation_id=another["id"],
                call_id="create",
            )
            assert not absent["ok"]

    asyncio.run(run())


def test_navigation():
    async def run():
        async with database() as (owner, _):
            result = await tools.execute_tool(
                "navigate",
                '{"page":"download_plans"}',
                owner,
                conversation_id=1,
                call_id="navigate",
            )
            assert result["data"]["path"] == "/downloads/plans"
            with pytest.raises(ValidationError):
                tools.NavigationTarget(page="download_plans", resource_id=1)

    asyncio.run(run())


def test_list_permissions(monkeypatch):
    original_json = download_routes.json
    monkeypatch.setattr(
        download_routes, "json", lambda data: original_json(data, dumps=dumps)
    )

    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan()
            library = await MediaLib.create(
                name="Transfer", lib_type="movie", dir="/synthetic/library", priority=1
            )
            plain = await service.DownloadPlanService.create_once(body, owner)
            transfer = await service.DownloadPlanService.create_once(
                body.model_copy(
                    update={
                        "request_id": uuid4(),
                        "plan": body.plan.model_copy(
                            update={
                                "transfer_lib_id": library.id,
                                "transfer_method": TransferMethod.COPY,
                            }
                        ),
                    }
                ),
                owner,
            )
            private = await FlowGraph.create(
                name="Private indexer",
                category="indexer",
                state="published",
                definition=graph.definition,
            )
            other = body.model_copy(
                update={
                    "request_id": uuid4(),
                    "plan": body.plan.model_copy(update={"graph_id": private.id}),
                }
            )
            await service.DownloadPlanService.create_once(other, owner)
            route = inspect.unwrap(download_routes.list_plans)
            http = SimpleNamespace(ctx=SimpleNamespace(user=owner))
            owner.role = UserRole.USER
            owner.perms = Permissions(indexer_ids=[graph.id])

            result = loads(
                (await route(http, DownloadPlanQuery(page_num=1, page_size=1))).body
            )
            assert result["total"] == 1
            assert [item["id"] for item in result["items"]] == [plain["plan_id"]]
            owner.perms.media_lib_ids = [library.id]
            result = loads(
                (
                    await route(
                        http, DownloadPlanQuery(page_num=2, page_size=1, ordering="id")
                    )
                ).body
            )
            assert result["total"] == 2
            assert [item["id"] for item in result["items"]] == [transfer["plan_id"]]
            owner.perms.indexer_ids = []
            assert loads((await route(http, DownloadPlanQuery())).body)["total"] == 0
            owner.role = UserRole.ADMIN
            assert loads((await route(http, DownloadPlanQuery())).body)["total"] == 3

    asyncio.run(run())


@pytest.mark.parametrize(
    "pages,expected,added",
    [
        ([[]], "completed", 0),
        ([[{"title": "Missing link"}]], "failed", 0),
        ([[{"link": "magnet:?dn=missing-hash"}]], "failed", 0),
        ([[{"link": "https://fixture.invalid/not-a-torrent"}]], "failed", 0),
        ([[{"link": "invalid"}, {"link": "a" * 40}]], "partial", 1),
        ([[{"link": "a" * 40}], [{"title": "Missing link"}]], "partial", 1),
    ],
)
def test_rejected_search_links(monkeypatch, pages, expected, added):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan(batch_limit=2)
            created = await service.DownloadPlanService.create_once(body, owner)
            plan = await DownloadPlan.get(id=created["plan_id"])
            engine = SimpleNamespace(
                execute=AsyncMock(
                    side_effect=[{"items": items} for items in [*pages, []]]
                )
            )
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200, text="Not a torrent", headers={"Content-Type": "text/html"}
                    )
                )
            ) as client:
                monkeypatch.setattr(
                    syncer.Sanic,
                    "get_app",
                    lambda: SimpleNamespace(
                        ctx=SimpleNamespace(flow_engine=engine, httpx=client)
                    ),
                )
                result = await syncer.execute_download_plan(plan, DraftDriver())
            assert result["status"] == expected
            assert result["added"] == added
            assert result["errors"] == (0 if expected == "completed" else 1)

    asyncio.run(run())
