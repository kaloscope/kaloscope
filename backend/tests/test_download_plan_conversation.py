"""Test bounded indexer previews and confirmed conversational plan creation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from app.core.ai import history, tools
from app.core.exceptions import ForbiddenException
from app.core.flow.engine import FlowEngine
from app.models.assistant import AssistantToolCall, AssistantTurn, ConversationCreate
from app.models.assistant_questions import AnswerGroup
from app.models.download import (
    DownloadPlan,
    DownloadPlanCheck,
    DownloadTask,
    IndexerSearch,
)
from app.models.flow import FlowGraph, FlowJob, FlowLog, FlowVariable
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.user import Permissions, PermType, UserPermission, UserRole
from app.services import download as service
from app.services.assistant import AssistantService
from app.services.assistant_questions import answer_card
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowGraphService
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response


def engine_fixture(monkeypatch, output):
    """Supply a synthetic search engine without starting application services.

    Args:
        monkeypatch: Pytest patch helper.
        output: Page returned by the engine.

    Returns:
        Mock execution method for call assertions.
    """
    execute = AsyncMock(return_value=output)
    app = SimpleNamespace(
        ctx=SimpleNamespace(flow_engine=SimpleNamespace(execute=execute))
    )
    monkeypatch.setattr(service.Sanic, "get_app", lambda: app)
    return execute


@pytest.mark.parametrize("search", [{}, {"page_num": 2}, {"sources": True}])
def test_preview_without_options(monkeypatch, search):
    engine_fixture(monkeypatch, {"items": [{"id": "r1", "title": "Lantern"}]})
    options = AsyncMock(side_effect=ValueError("Invalid downloader configuration"))
    monkeypatch.setattr(service.DownloadPlanService, "authoring_options", options)

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            result = await tools.preview_indexer(
                IndexerSearch(workflow_id=graph.id, keyword="Lantern", **search), owner
            )

            assert result["status"] == "ok" and result["items"][0]["id"] == "r1"
            assert "subscription" not in result
            if "page_num" in search:
                options.assert_not_awaited()
            else:
                options.assert_awaited_once()
                assert "subscription_note" in result

    asyncio.run(run())


@pytest.mark.parametrize(
    "counts,expected",
    [
        ({"total": 11}, (11, None, True, 2)),
        ({"totalPages": 3}, (None, 3, True, 2)),
        ({}, (None, None, None, None)),
        ({"total": 1, "totalPages": 1}, (1, 1, False, None)),
    ],
)
def test_search_page(monkeypatch, counts, expected):
    output = {
        "items": [
            {
                "id": "r1",
                "title": "Lantern 2025",
                "uploader": "Group A",
                "link": "private-token",
                "extra": "hidden",
            }
        ],
        **counts,
    }
    execute = engine_fixture(monkeypatch, output)

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            before = await FlowGraph.all().values()
            body = IndexerSearch(
                workflow_id=graph.id, keyword="Lantern", filters={"category": "movie"}
            )
            result = await service.DownloadPlanService.search_indexer(body, owner)

            assert result["status"] == "ok"
            assert (
                tuple(
                    result[key]
                    for key in ("total", "total_pages", "has_more", "next_page")
                )
                == expected
            )
            assert result["received_count"] == result["returned_count"] == 1
            assert result["omitted_count"] == 0 and not result["truncated"]
            assert result["items"][0]["uploader"] == "Group A"
            assert (
                "private-token" not in dumps(result).decode()
                and "hidden" not in dumps(result).decode()
            )
            execute.assert_awaited_once_with(
                graph.id,
                {
                    "$start": "search_start",
                    "keyword": "Lantern",
                    "page_num": 1,
                    "page_size": 5,
                    "category": "movie",
                },
                repeatable=True,
                recoverable=False,
                asynchronous=False,
            )
            assert await FlowGraph.all().values() == before
            assert (
                await DownloadPlan.all().count()
                == await DownloadTask.all().count()
                == await FlowJob.all().count()
                == 0
            )
            service.notify_download_changes.assert_not_called()

    asyncio.run(run())


def test_continuation_and_empty(monkeypatch):
    execute = engine_fixture(
        monkeypatch, {"items": [{"id": "r2", "title": "Lantern"}], "total": 2}
    )

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            body = IndexerSearch(
                workflow_id=graph.id, keyword="Lantern", page_num=2, page_size=1
            )
            result = await service.DownloadPlanService.search_indexer(body, owner)
            assert result["status"] == "ok" and result["has_more"] is False
            assert execute.call_args.args[1]["page_num"] == 2
            execute.return_value = {"items": [], "total": 0}
            body.page_num = 1
            empty = await service.DownloadPlanService.search_indexer(body, owner)
            assert (
                empty["status"] == "ok"
                and empty["returned_count"] == empty["total"] == 0
            )

    asyncio.run(run())


def test_published_search_engine(monkeypatch):
    app = SimpleNamespace(
        ctx=SimpleNamespace(flow_engine=FlowEngine.__new__(FlowEngine))
    )
    monkeypatch.setattr(service.Sanic, "get_app", lambda: app)

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            graph.definition["nodes"].append(
                {
                    "id": "end",
                    "data": {
                        "$schema": "search_end",
                        "response": (
                            '{"items":[{"id":{{ page_num|string|tojson }},'
                            '"title":{{ (keyword ~ " / " ~ category)|tojson }}}],'
                            '"total":2}'
                        ),
                    },
                }
            )
            graph.definition["edges"] = [
                {
                    "source": "search",
                    "target": "end",
                    "sourceHandle": "output",
                    "targetHandle": "input",
                }
            ]
            await graph.save()
            result = await service.DownloadPlanService.search_indexer(
                IndexerSearch(
                    workflow_id=graph.id,
                    keyword="Lantern",
                    filters={"category": "movie"},
                    page_num=2,
                    page_size=1,
                ),
                owner,
            )
            assert result["status"] == "ok", result
            assert result["items"] == [{"id": "2", "title": "Lantern / movie"}]
            assert not result["has_more"]
            assert (
                await DownloadTask.all().count()
                == await DownloadPlan.all().count()
                == 0
            )

    asyncio.run(run())


def test_timeout_cancels_engine(monkeypatch):
    cancelled = []

    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    execute = engine_fixture(monkeypatch, {})
    execute.side_effect = slow
    timeout = asyncio.timeout
    monkeypatch.setattr(service.asyncio, "timeout", lambda seconds: timeout(0.01))

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            result = await service.DownloadPlanService.search_indexer(
                IndexerSearch(workflow_id=graph.id, keyword="Lantern"), owner
            )
            assert result["status"] == "timeout"
            assert cancelled == [True]
            execute.assert_awaited_once()

    asyncio.run(run())


def test_cookie_login(monkeypatch):
    from unittest.mock import Mock

    async def run():
        async with database():
            graph, _, _ = await fixture_plan()
            graph.definition["nodes"].append(
                {
                    "id": "auth",
                    "data": {
                        "$schema": "auth_start",
                        "config": (
                            "cookie: {domain: example.test, path: /, name: session}"
                        ),
                    },
                }
            )
            await graph.save()
            cookies = Mock()
            cookies.get_cookie.return_value = object()
            assert await FlowGraphService.indexer_auth(graph.id, cookies) == {
                "name": ""
            }
            cookies.get_cookie.assert_called_once_with(
                domain="example.test", path="/", name="session"
            )
            cookies.get_cookie.return_value = None
            assert await FlowGraphService.indexer_auth(graph.id, cookies) is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "filters",
    [
        {"$start": "details_start"},
        {"page_num": 50},
        {"year": "2025"},
        {"category": "wrong"},
        {"quality": "hd"},
    ],
)
def test_bad_search_input(monkeypatch, filters):
    execute = engine_fixture(monkeypatch, {})

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            result = await service.DownloadPlanService.search_indexer(
                IndexerSearch(workflow_id=graph.id, keyword="Lantern", filters=filters),
                owner,
            )
            assert result["status"] == "invalid_input" and result["issues"]
            execute.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("pagination", "has_more"),
    [({"total": 160}, True), ({"total": 80}, False), ({"totalPages": 2}, True)],
    ids=["more", "last", "pages"],
)
def test_site_page(monkeypatch, pagination, has_more):
    execute = engine_fixture(
        monkeypatch,
        {"items": [{"id": str(i), "title": f"Lantern {i}"} for i in range(80)]}
        | pagination,
    )

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            body = IndexerSearch(workflow_id=graph.id, keyword="Lantern", page_size=20)

            result = await service.DownloadPlanService.search_indexer(body, owner)

            assert result["status"] == "ok", result
            assert result["received_count"] == 80 and result["returned_count"] == 20
            assert result["omitted_count"] == 60 and result["truncated"]
            assert result["has_more"] is has_more
            assert result["next_page"] == (2 if has_more else None)
            assert "80 items" in result["page_note"]

            execute.return_value = {
                "items": [{"id": "same", "title": f"Lantern {i}"} for i in range(2)]
            }
            duplicated = await service.DownloadPlanService.search_indexer(body, owner)
            assert duplicated["status"] == "invalid_output"
            assert {issue["message"] for issue in duplicated["issues"]} == {
                "Use unique remote IDs."
            }

    asyncio.run(run())


def test_truncation(monkeypatch):
    execute = engine_fixture(
        monkeypatch,
        {
            "items": [
                {
                    "id": str(i),
                    "title": "片" * 1000,
                    "misc": "x" * 1000,
                    "uploader": "y" * 1000,
                }
                for i in range(20)
            ],
            "total": 80,
        },
    )

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            body = IndexerSearch(workflow_id=graph.id, keyword="Lantern", page_size=20)
            result = await service.DownloadPlanService.search_indexer(body, owner)
            assert result["status"] == "ok" and result["truncated"]
            assert 0 < result["returned_count"] < result["received_count"] == 20
            assert result["omitted_count"] == 20 - result["returned_count"]
            assert result["total"] == 80 and result["next_page"] == 2
            assert len(dumps(result)) < 8000

            execute.return_value = {"items": [{"id": "bad"}], "total": 1}
            invalid = await service.DownloadPlanService.search_indexer(body, owner)
            assert invalid["status"] == "invalid_output" and invalid["issues"]
            assert "total" not in invalid

            execute.return_value = {"items": [{} for _ in range(20)]}
            invalid_output = await service.DownloadPlanService.search_indexer(
                body, owner
            )
            assert "issue_count" not in invalid_output
            assert invalid_output["issues_truncated"]
            assert len(invalid_output["issues"]) == 10

            body.filters = {f"undeclared-{i}": "value" for i in range(100)}
            invalid_input = await service.DownloadPlanService.search_indexer(
                body, owner
            )
            assert invalid_input["issue_count"] == 100
            assert len(invalid_input["issues"]) == 10
            assert len(dumps(invalid_input)) < 8000

    asyncio.run(run())


def test_failure_and_cancellation(monkeypatch):
    execute = engine_fixture(monkeypatch, {})

    async def run():
        async with database() as (owner, _):
            graph, _, _ = await fixture_plan()
            body = IndexerSearch(workflow_id=graph.id, keyword="Lantern")
            execute.side_effect = RuntimeError("secret-cookie")
            failed = await service.DownloadPlanService.search_indexer(body, owner)
            assert failed["status"] == "failed" and "secret-cookie" not in str(failed)
            execute.side_effect = TimeoutError()
            assert (await service.DownloadPlanService.search_indexer(body, owner))[
                "status"
            ] == "timeout"
            execute.side_effect = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await service.DownloadPlanService.search_indexer(body, owner)
            assert await DownloadPlan.all().count() == 0

    asyncio.run(run())


def test_login_and_permissions(monkeypatch):
    execute = engine_fixture(monkeypatch, {"items": [], "total": 0})

    async def run():
        async with database() as (owner, other):
            graph, _, _ = await fixture_plan()
            graph.definition["nodes"].append(
                {
                    "id": "auth",
                    "data": {
                        "$schema": "auth_start",
                        "config": "login: {required: true}",
                    },
                }
            )
            await graph.save()
            body = IndexerSearch(workflow_id=graph.id, keyword="Lantern")
            denied = await service.DownloadPlanService.search_indexer(body, owner)
            assert denied["status"] == "login_required"
            execute.assert_not_called()
            other.role, other.perms = UserRole.USER, Permissions(indexer_ids=[graph.id])
            assert (await service.DownloadPlanService.search_indexer(body, other))[
                "status"
            ] == "login_required"
            with pytest.raises(ForbiddenException):
                await service.DownloadPlanService.search_indexer(
                    body.model_copy(update={"sources": True}), other
                )
            auth = await FlowVariable.create(
                graph_id=graph.id,
                key="$auth",
                value={"name": "test", "token": "never-expose"},
            )
            result = await tools.preview_indexer(body, other)
            assert result["status"] == "ok" and "never-expose" not in str(result)
            assert "subscription" not in result and "subscription_note" not in result
            other.perms = Permissions()
            with pytest.raises(ForbiddenException):
                await service.DownloadPlanService.search_indexer(body, other)
            auth.expires = 1
            await auth.save()
            assert (await service.DownloadPlanService.search_indexer(body, owner))[
                "status"
            ] == "login_required"

    asyncio.run(run())


def test_preview_search_permissions(monkeypatch):
    execute = engine_fixture(
        monkeypatch,
        {"items": [{"id": "r1", "title": "Lantern", "link": "test-source"}]},
    )
    options = AsyncMock()
    monkeypatch.setattr(service.DownloadPlanService, "authoring_options", options)

    async def run():
        async with database() as (owner, reader):
            graph, _, _ = await fixture_plan()
            graph.definition["nodes"][0]["data"]["config"] += "\nonly_preview: true\n"
            await graph.save()
            reader.role, reader.perms = (
                UserRole.USER,
                Permissions(indexer_ids=[graph.id]),
            )
            values = {
                "workflow_id": graph.id,
                "keyword": "Lantern",
                "filters": {"category": "movie"},
            }
            result = await tools.execute_tool(
                "search_indexer",
                dumps(values).decode(),
                reader,
                conversation_id=1,
                call_id="search",
            )
            assert result["data"]["items"] == [{"id": "r1", "title": "Lantern"}]
            assert "subscription" not in result["data"]
            assert execute.await_args.args[1] == {
                "$start": "search_start",
                "keyword": "Lantern",
                "category": "movie",
                "page_num": 1,
                "page_size": 5,
            }
            options.assert_not_awaited()
            for account, settings in (
                (reader, {"filters": {"undeclared": 1}}),
                (owner, {"sources": True}),
            ):
                rejected = await service.DownloadPlanService.search_indexer(
                    IndexerSearch(**{**values, **settings}), account
                )
                assert rejected["status"] == "invalid_input"
            execute.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("latest_status", ["complete", "error"])
def test_confirmation_gate(latest_status):
    async def run():
        async with database() as (owner, _):
            graph, _, body = await fixture_plan()
            body.confirmed = False
            result = await service.DownloadPlanService.create_once(body, owner)
            assert result["confirmation_required"] and not result["created"]
            assert result["request_id"] == str(body.request_id)
            assert await DownloadPlan.all().count() == 0
            service.notify_download_changes.assert_not_called()

            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            turn = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="review",
                prompt="Show defaults",
            )
            checked = await service.DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=body.plan), owner
            )
            validation = await AssistantToolCall.create(
                turn_id=turn.id,
                call_id="validate",
                name="validate_download_plan",
                arguments="{}",
                status="complete",
                result={"ok": True, "data": checked},
            )
            args = dumps(
                {"request_id": checked["request_id"], "confirmed": True}
            ).decode()
            action = await AssistantToolCall.create(
                turn_id=turn.id,
                call_id="create",
                name="create_download_plan",
                arguments=args,
            )

            blocked = await tools.execute_tool(
                "create_download_plan",
                args,
                owner,
                conversation_id=conversation["id"],
                call_id=action.call_id,
            )
            assert blocked["ok"] and blocked["data"]["confirmation_required"]
            assert await DownloadPlan.all().count() == 0

            turn.status = "complete"
            await turn.save()
            confirm = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="confirm",
                prompt="Confirm",
            )
            action.turn_id = confirm.id
            await action.save()
            newer = await AssistantToolCall.create(
                turn_id=confirm.id,
                call_id="changed",
                name="validate_download_plan",
                arguments="{}",
                status=latest_status,
                result={"ok": True, "data": {**checked, "valid": False}},
            )
            stale = await tools.execute_tool(
                "create_download_plan",
                args,
                owner,
                conversation_id=conversation["id"],
                call_id=action.call_id,
            )
            assert (
                stale["data"]["confirmation_required"]
                and await DownloadPlan.all().count() == 0
            )
            await newer.delete()

            for confirmation in ({}, {"confirmed": False}):
                unconfirmed = await tools.execute_tool(
                    "create_download_plan",
                    dumps(
                        {"request_id": checked["request_id"], **confirmation}
                    ).decode(),
                    owner,
                    conversation_id=conversation["id"],
                    call_id=action.call_id,
                )
                assert unconfirmed["ok"]
                assert unconfirmed["data"]["confirmation_required"]
                assert await DownloadPlan.all().count() == 0
                service.notify_download_changes.assert_not_called()

            created = await tools.execute_tool(
                "create_download_plan",
                args,
                owner,
                conversation_id=conversation["id"],
                call_id=action.call_id,
            )
            assert created["data"]["created"]
            assert (
                graph.id == created["data"]["workflow_id"]
                and validation.turn_id != action.turn_id
            )

    asyncio.run(run())


@pytest.mark.parametrize(
    "scenario,created",
    [
        ("choices", True),
        ("same_title", True),
        ("renamed", True),
        ("editor", True),
        ("different_title", False),
        ("new_binding", False),
        ("declined", False),
        ("unrelated_turn", False),
        ("interrupted", False),
        ("retry_first_turn", False),
    ],
)
def test_revised_subscription(scenario, created):
    async def run():
        async with database() as (owner, _):
            _, _, body = await fixture_plan()
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            offer = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="offer",
                prompt="Subscribe to Lantern",
                content="Reply with your choices to create this subscription.",
                status="interrupted" if scenario == "interrupted" else "complete",
            )
            original = await service.DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=body.plan), owner
            )
            await AssistantToolCall.create(
                turn_id=offer.id,
                call_id="offer-plan",
                name="validate_download_plan",
                arguments="{}",
                status="complete",
                result={"ok": True, "data": original},
            )
            if scenario == "editor":
                await service.DownloadPlanService.create_once(
                    body.model_copy(
                        update={"request_id": UUID(original["request_id"])}
                    ),
                    owner,
                )
            if scenario == "unrelated_turn":
                await AssistantTurn.create(
                    conversation_id=conversation["id"],
                    request_id="other",
                    prompt="List libraries",
                    content="No libraries.",
                    status="complete",
                )
            reply = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="reply",
                prompt="Use my choices",
                retry_of=offer.id if scenario == "retry_first_turn" else None,
            )
            body.plan.interval_num = 2
            if scenario in {"renamed", "different_title"}:
                body.plan.keyword = "Lantern HD"
            checked = await service.DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=body.plan), owner
            )
            if scenario == "new_binding":
                checked["ingestion"] = {
                    "binding": "add",
                    "library_id": 1,
                    "workflow_id": 2,
                    "revision": "new",
                }
            await AssistantToolCall.create(
                turn_id=reply.id,
                call_id="final",
                name="validate_download_plan",
                arguments="{}",
                status="complete",
                result={"ok": True, "data": checked},
            )
            approval = {
                "request_id": checked["request_id"],
                "confirmed": scenario != "declined",
            }
            if scenario == "renamed":
                approval["proposal_id"] = original["request_id"]
            if scenario == "same_title":
                approval["proposal_id"] = checked["request_id"]
            args = dumps(approval).decode()
            await AssistantToolCall.create(
                turn_id=reply.id,
                call_id="create",
                name="create_download_plan",
                arguments=args,
            )
            result = await tools.execute_tool(
                "create_download_plan",
                args,
                owner,
                conversation_id=conversation["id"],
                call_id="create",
            )

            assert result["ok"] and result["data"]["created"] is created
            assert await DownloadPlan.all().count() == int(created)
            if scenario == "editor":
                assert (
                    result["data"]["replayed"]
                    and (await DownloadPlan.get()).interval_num == 6
                )
            elif created:
                plan = await DownloadPlan.get()
                assert plan.interval_num == 2 and plan.keyword == body.plan.keyword
            else:
                assert result["data"]["confirmation_required"]
                service.notify_download_changes.assert_not_called()

    asyncio.run(run())


def test_mcp_search(monkeypatch):
    async def run():
        async with application() as app:
            monkeypatch.setattr(service.Sanic, "get_app", lambda: app)
            graph, _, body = await fixture_plan()
            engine = AsyncMock(
                return_value={"items": [{"id": "r1", "title": "Lantern"}], "total": 1}
            )
            app.ctx.flow_engine = SimpleNamespace(execute=engine)
            args = {"search": {"workflow_id": graph.id, "keyword": "Lantern"}}
            for account in ("reader", "admin"):
                denied = await call(app, "search_indexer", args, token=account)
                assert denied["isError"]
            engine.assert_not_called()

            tokens = {}
            for account in ("reader", "admin"):
                token = await MCPAccessTokenService.create(
                    app.shared_ctx.sessions[account].id,
                    MCPAccessTokenCreate(name="search", scope=MCPTokenScope.READ_WRITE),
                )
                tokens[account] = token["token"]
            assert (await call(app, "search_indexer", args, token=tokens["reader"]))[
                "isError"
            ]

            await UserPermission.create(
                user_id=app.shared_ctx.sessions["reader"].id,
                rel_type=PermType.INDEXER,
                rel_id=graph.id,
            )
            read_only = await call(app, "search_indexer", args, token="reader")
            assert read_only["isError"] and "read-only" in str(read_only)
            visible = await call(app, "search_indexer", args, token=tokens["reader"])
            assert visible["structuredContent"]["status"] == "ok"
            blocked = await call(
                app,
                "search_indexer",
                {"search": {**args["search"], "sources": True}},
                token=tokens["reader"],
            )
            assert blocked["isError"]
            result = await call(app, "search_indexer", args, token=tokens["admin"])
            assert not result.get("isError"), result
            assert result["structuredContent"]["status"] == "ok"
            assert result["structuredContent"]["items"][0]["title"] == "Lantern"

            body.confirmed = False
            unconfirmed = await call(
                app,
                "create_download_plan",
                {"creation": body.model_dump(mode="json")},
                token=tokens["admin"],
            )
            assert unconfirmed["structuredContent"]["confirmation_required"]
            assert (
                await DownloadPlan.all().count()
                == await DownloadTask.all().count()
                == 0
            )

    asyncio.run(run())


@pytest.mark.parametrize("verbose", [False, True])
def test_conversation(monkeypatch, verbose):
    fixture = {}
    stage = {"number": 0, "step": 0}

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
                                        "goals": [
                                            "Download Lantern with selected indexer"
                                        ],
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

        number, step = stage["number"], stage["step"]
        stage["step"] += 1
        flow = fixture["graph_id"]

        actions = [
            [
                ("list_workflows", {"category": "indexer"}),
                ("get_workflow", {"workflow_id": flow}),
                ("search_indexer", {"workflow_id": flow, "keyword": "Lantern"}),
            ],
            [
                (
                    "search_indexer",
                    {
                        "workflow_id": flow,
                        "keyword": "Lantern",
                        "filters": {"category": "movie"},
                    },
                ),
                ("validate_download_plan", {"plan": fixture["plan"]}),
                (
                    "create_download_plan",
                    {
                        "request_id": fixture.get("request_id", ""),
                        "confirmed": True,
                    },
                ),
                (
                    "create_download_plan",
                    {
                        "request_id": fixture.get("request_id", ""),
                        "confirmed": True,
                    },
                ),
            ],
        ][number]

        if step >= len(actions):
            content = [
                "Found Lantern. Category is declared; year is not a filter. "
                "Defaults: now, no end, hourly, one resource per check/total, "
                "Fixture downloader, no transfer.",
                "Created one plan.",
            ][number]
            return response([chunk({"role": "assistant", "content": content}, "stop")])

        name, args = actions[step]
        names = {item["function"]["name"] for item in body["tools"]}
        assert {
            "search_indexer",
            "create_download_plan",
            "validate_download_plan",
        } <= names
        assert "preview_workflow_draft" not in names

        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "content": "Checking supplied facts. " * 400 if verbose else "",
                        "tool_calls": [
                            call_delta(
                                name, dumps(args).decode(), id=f"{number}-{step}"
                            )
                        ],
                    },
                    "tool_calls",
                )
            ]
        )

    configure(monkeypatch, handler)
    execute = engine_fixture(
        monkeypatch,
        {
            "items": [{"id": "r1", "title": "Lantern 2025", "uploader": "Group A"}],
            "total": 1,
        },
    )

    async def run():
        async with database() as (owner, _):
            graph, downloader, _ = await fixture_plan()
            options = await service.DownloadPlanService.authoring_options(
                graph.id, owner
            )
            assert options["defaults"]["downloader_id"] == downloader.id
            assert options["defaults"]["total_limit"] is None
            assert options["defaults"]["batch_limit"] == 1
            fixture.update(
                graph_id=graph.id,
                plan={
                    **options["defaults"],
                    "graph_id": graph.id,
                    "keyword": "Lantern",
                    "filters": {"category": "movie"},
                },
            )

            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            all_results = []
            for number, prompt in enumerate(
                [
                    "帮我订阅 Lantern",
                    "选择电影分类，其他用所示默认值",
                ]
            ):
                stage.update(number=number, step=0)
                events = []
                async for event in AssistantService.persistent_stream(
                    conversation["id"], request(f"step-{number}", prompt=prompt), owner
                ):
                    events.append(event)
                    if (
                        event.event == "tool_result"
                        and event.data["name"] == "validate_download_plan"
                    ):
                        assert event.data["data"]["valid"]
                        fixture["request_id"] = event.data["data"]["request_id"]
                assert events[-1].event == "done", events[-1]
                results = [
                    event.data for event in events if event.event == "tool_result"
                ]
                assert all(result["ok"] for result in results), results
                all_results.extend(results)
                if number == 0:
                    execute.assert_awaited_once()
                    assert results[0]["data"]["items"][0]["downloadable_search"]
                    assert (
                        results[-1]["data"]["subscription"]["defaults"]
                        == options["defaults"]
                    )
                    assert (
                        await DownloadPlan.all().count()
                        == await DownloadTask.all().count()
                        == 0
                    )
                    service.notify_download_changes.assert_not_called()

            assert execute.await_count == 2
            assert (
                execute.call_args_list[0].args[1]["keyword"]
                == execute.call_args_list[1].args[1]["keyword"]
                == "Lantern"
            )
            assert "category" not in execute.call_args_list[0].args[1]
            assert execute.call_args_list[1].args[1]["category"] == "movie"

            assert not await DownloadPlan.exists()
            question = all_results[-1]
            assert question["name"] == "ask_user"
            card = question["data"]
            answers = [
                {
                    "id": q["id"],
                    "selected": [
                        q["options"][-1 if q["field"] == "release" else 0]["id"]
                    ],
                }
                for q in card["questions"]
            ]
            accepted = await answer_card(
                conversation["id"],
                question["id"],
                AnswerGroup.model_validate(
                    {"version": card["version"], "items": answers}
                ),
                owner,
            )
            assert accepted["card"]["state"] == "answered"
            submission = request("answer-" + question["id"], answer_to=question["id"])
            applied = [
                e
                async for e in AssistantService.persistent_stream(
                    conversation["id"], submission, owner
                )
            ]
            assert any(
                e.event == "tool_result"
                and e.data["name"] == "create_download_plan"
                and e.data["data"]["created"]
                for e in applied
            )
            assert await DownloadPlan.all().count() == 1
            plan = await DownloadPlan.first()
            assert plan.keyword == "Lantern" and plan.filters == {"category": "movie"}
            assert plan.batch_limit == 1 and plan.total_limit is None
            assert plan.creation_receipt["first_execution"]["status"] == "queued"
            service.notify_download_changes.assert_called_once()
            assert await DownloadTask.all().count() == await FlowLog.all().count() == 0
            preview_turn = await AssistantTurn.get(request_id="step-0")
            assert preview_turn.resource_ids == [graph.id]
            owner.role, owner.perms = UserRole.USER, Permissions(indexer_ids=[])
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": preview_turn.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="read",
            )
            assert denied["error"]["code"] == "permission_denied"
            restored = await Sessions.turns(conversation["id"], owner)
            assert all(
                not item["tools"] and not item["content"] for item in restored["items"]
            )

    asyncio.run(run())
