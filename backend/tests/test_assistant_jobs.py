"""Unit tests for read-only assistant workflow schedule queries."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from app.core.ai import history, tools
from app.core.ai.documents import tool_facts
from app.core.exceptions import BadRequestException, KaloscopeException
from app.core.flow.engine import FlowEngine, FlowTask
from app.models.assistant import AssistantConversation, AssistantToolCall, AssistantTurn
from app.models.flow import FlowGraph, FlowJob, FlowLog
from app.models.user import Permissions, UserInfo, UserRole
from app.services.assistant import SYSTEM_PROMPT, AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowJobService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import (
    MODEL,
    ConversationCreate,
    completion_client,
    database,
    request,
)
from tests.test_services_assistant import call_delta, chunk, configure, response

ROUTE = "/settings/workflows/schedules"
CONTEXT = {
    "page_type": "workflow_jobs",
    "route": ROUTE,
    "selected_job": None,
    "filters": {
        "name": "",
        "state": None,
        "trigger": None,
        "ordering": "",
        "page_num": 1,
        "page_size": 15,
    },
}


@asynccontextmanager
async def schedules():
    """Create schedules with separate workflow IDs and all timing modes.

    Yields:
        Accounts, workflows, and schedules in an isolated database.
    """
    async with database() as (owner, _):
        graphs = [
            await FlowGraph.create(
                id=41 + index, name=name, category=category, state="published"
            )
            for index, (name, category) in enumerate(
                [
                    ("Alpha scan", "indexer"),
                    ("Beta cleanup", "schedule"),
                    ("Gamma", "schedule"),
                ]
            )
        ]
        start = datetime(2026, 10, 24, 8, tzinfo=UTC)
        settings = [
            (graphs[0], "pending", "date", {"run_date": start}),
            (graphs[0], "running", "cron", {"cron_expr": "0 8 * * *"}),
            (
                graphs[1],
                "paused",
                "interval",
                {
                    "interval_num": 2,
                    "interval_unit": "hours",
                    "interval_start": start,
                    "interval_end": start + timedelta(days=2),
                },
            ),
            (graphs[1], "running", "date", {"run_date": start + timedelta(days=1)}),
            (
                graphs[2],
                "pending",
                "interval",
                {"interval_num": 30, "interval_unit": "seconds"},
            ),
        ]
        jobs = []
        for index, (graph, state, trigger, timing) in enumerate(settings):
            job = await FlowJob.create(
                id=101 + index,
                graph=graph,
                state=state,
                trigger=trigger,
                repeatable=index == 0,
                recoverable=index != 1,
                bootparams={"unrelated": "private parameters " * 2000},
                **timing,
            )
            await FlowJob.filter(id=job.id).update(
                created_at=start + timedelta(seconds=index),
                updated_at=start - timedelta(seconds=index),
            )
            jobs.append(job)
        reader = cast(
            UserInfo,
            SimpleNamespace(
                id=owner.id,
                role=UserRole.USER,
                perms=Permissions(indexer_ids=[graphs[0].id]),
            ),
        )
        yield SimpleNamespace(owner=owner, reader=reader, graphs=graphs, jobs=jobs)


async def call(user, arguments=None, name="list_workflow_jobs", conversation_id=1):
    """Execute a registered tool without a provider request.

    Args:
        user: Current account and workflow permissions.
        arguments: Validated by the tool before execution.
        name: Registered tool name.
        conversation_id: Owning conversation for historical reads.

    Returns:
        The bounded tool result or a sanitized error.
    """
    return await tools.execute_tool(
        name,
        dumps(arguments or {}).decode(),
        user,
        conversation_id=conversation_id,
        call_id="test",
    )


@pytest.mark.parametrize(
    ("filters", "ids"),
    [
        ({"name": "ALPHA"}, [101, 102]),
        ({"name": ""}, [101, 102, 103, 104, 105]),
        ({"state": "pending"}, [101, 105]),
        ({"state": "running"}, [102, 104]),
        ({"state": "paused"}, [103]),
        ({"trigger": "date"}, [101, 104]),
        ({"trigger": "cron"}, [102]),
        ({"trigger": "interval"}, [103, 105]),
        ({"name": "alpha", "state": "running", "trigger": "cron"}, [102]),
        ({"name": "missing"}, []),
        ({"name": "private parameters"}, []),
        ({"state": "paused", "trigger": "date"}, []),
    ],
)
def test_filters(filters, ids):
    async def run():
        async with schedules() as fixture:
            result = await call(fixture.owner, filters)

            assert result["ok"]
            data = result["data"]
            assert sorted(item["job_id"] for item in data["items"]) == ids
            assert data["total"] == len(ids)
            assert data["page_counts"] == {"returned": len(ids)}
            assert data["next_page_num"] is None
            assert data["filters"] == {
                "name": filters.get("name") or None,
                "state": filters.get("state"),
                "trigger": filters.get("trigger"),
                "ordering": "",
            }

    asyncio.run(run())


@pytest.mark.parametrize(
    "ordering",
    [
        "",
        "graph__name",
        "-graph__name",
        "trigger",
        "-trigger",
        "state",
        "-state",
        "updated_at",
        "-updated_at",
    ],
)
def test_ordering(ordering):
    async def run():
        async with schedules() as fixture:
            result = await call(fixture.owner, {"ordering": ordering})
            items = result["data"]["items"]

            assert result["data"]["filters"]["ordering"] == ordering
            if ordering in {"", "updated_at", "-updated_at"}:
                ids = [item["job_id"] for item in items]
                assert ids == sorted(ids, reverse=ordering != "-updated_at")
            else:
                field = (
                    "workflow_name"
                    if "graph__name" in ordering
                    else ordering.lstrip("-")
                )
                values = [item[field] for item in items]
                assert values == sorted(values, reverse=ordering.startswith("-"))

    asyncio.run(run())


def test_pages():
    async def run():
        async with schedules() as fixture:
            page_num, ids = 1, []
            while page_num is not None:
                result = await call(
                    fixture.owner, {"page_num": page_num, "page_size": 2}
                )
                data = result["data"]

                assert data["total"] == 5
                assert data["page_num"] == page_num
                assert data["page_size"] == 2
                assert data["page_counts"]["returned"] == (1 if page_num == 3 else 2)
                assert data["next_page_num"] == (page_num + 1 if page_num < 3 else None)
                ids.extend(item["job_id"] for item in data["items"])
                page_num = data["next_page_num"]

            assert ids == [105, 104, 103, 102, 101]
            result = await call(fixture.owner, {"page_num": 4, "page_size": 2})
            assert result["data"]["total"] == 5
            assert result["data"]["page_counts"] == {"returned": 0}
            assert result["data"]["items"] == []
            assert result["data"]["next_page_num"] is None

    asyncio.run(run())


def test_timing():
    async def run():
        async with schedules() as fixture:
            # stale fields for another trigger must not be presented as active timing
            await FlowJob.filter(id=101).update(cron_expr="0 9 * * *", interval_num=99)
            result = await call(fixture.owner)
            data = result["data"]
            items = {item["job_id"]: item for item in data["items"]}

            assert data["timezone"] == "UTC"
            common = {
                "job_id",
                "workflow_id",
                "workflow_name",
                "state",
                "trigger",
                "repeatable",
                "recoverable",
            }
            assert items[101].keys() == common | {"run_date"}
            assert datetime.fromisoformat(items[101]["run_date"]) == datetime(
                2026, 10, 24, 8, tzinfo=UTC
            )
            assert items[102].keys() == common | {"cron_expr"}
            assert items[102]["cron_expr"] == "0 8 * * *"
            assert items[103].keys() == common | {
                "interval_num",
                "interval_unit",
                "interval_start",
                "interval_end",
            }
            assert (items[103]["interval_num"], items[103]["interval_unit"]) == (
                2,
                "hours",
            )
            assert datetime.fromisoformat(items[103]["interval_end"]) == datetime(
                2026, 10, 26, 8, tzinfo=UTC
            )
            assert items[105]["interval_start"] is None
            assert items[105]["interval_end"] is None
            assert items[101]["repeatable"] is True and items[101]["trigger"] == "date"
            assert (
                items[102]["recoverable"] is False and items[102]["trigger"] == "cron"
            )
            assert items[102]["state"] == "running"
            assert not await FlowLog.exists()
            assert {item["workflow_id"] for item in items.values()} == {41, 42, 43}
            assert items[101]["workflow_name"] == "Alpha scan"
            description = tools.available_tools(fixture.owner)[
                "list_workflow_jobs"
            ].description
            for meaning in (
                "not executing",
                "concurrent identical parameters",
                "recovery",
                "Never infer next run or success",
            ):
                assert meaning in description

    asyncio.run(run())


def test_permissions():
    async def run():
        async with schedules() as fixture:
            result = await call(fixture.reader)
            assert result["data"]["total"] == 2
            assert {item["workflow_id"] for item in result["data"]["items"]} == {41}
            hidden = await call(fixture.reader, {"name": "Beta"})
            assert hidden["data"]["total"] == 0
            fixture.reader.perms = None
            empty = await call(fixture.reader)
            assert empty["data"]["items"] == []
            assert empty["data"]["total"] == 0

    asyncio.run(run())


def test_read_only(monkeypatch):
    trap = AsyncMock(side_effect=AssertionError("Queries must not schedule or execute"))
    for method in (
        "add_job",
        "pause_job",
        "refresh_job",
        "execute",
        "execute_batch",
    ):
        monkeypatch.setattr(FlowEngine, method, trap)
    monkeypatch.setattr(FlowTask, "run", trap)
    monkeypatch.setattr(FlowJobService, "upsert", trap)
    monkeypatch.setattr(
        FlowJobService,
        "app_ctx",
        Mock(side_effect=AssertionError("No scheduler needed")),
    )
    monkeypatch.setattr(httpx.AsyncClient, "send", trap)

    async def run():
        async with schedules() as fixture:
            before = [
                await model.all().order_by("id").values()
                for model in (FlowGraph, FlowJob, FlowLog)
            ]

            result = await call(fixture.owner)

            assert result["ok"]
            assert before == [
                await model.all().order_by("id").values()
                for model in (FlowGraph, FlowJob, FlowLog)
            ]
            assert "private parameters" not in dumps(result).decode()
            trap.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments",
    [
        {"page_num": 0},
        {"page_num": True},
        {"page_size": 26},
        {"page_size": 0},
        {"state": "completed"},
        {"trigger": "manual"},
        {"ordering": "name"},
        {"ordering": "bootparams"},
        {"name": "x" * 256},
        {"job_id": 101},
    ],
)
def test_invalid(arguments):
    result = asyncio.run(call(cast(UserInfo, SimpleNamespace(role="admin")), arguments))
    assert result["error"]["code"] == "bad_request"


def test_navigation():
    async def run():
        async with schedules() as fixture:
            for user in (fixture.owner, fixture.reader):
                result = await call(user, {"page": "workflow_jobs"}, "navigate")
                assert result["data"]["path"] == ROUTE
                assert result["data"]["target"] == {
                    "page": "workflow_jobs",
                    "resource_id": None,
                }
            for arguments in (
                {"page": "workflow_jobs", "resource_id": 101},
                {"page": ROUTE},
                {"page": "workflow_jobs", "url": "https://example.com"},
            ):
                result = await call(fixture.owner, arguments, "navigate")
                assert result["error"]["code"] == "bad_request"
            result = await call(
                fixture.reader,
                {"page": "workflow_designer", "resource_id": 41},
                "navigate",
            )
            assert result["data"]["path"] == "/settings/workflows/graphs/r/41"
            denied = await call(
                fixture.reader,
                {"page": "workflow_designer", "resource_id": 42},
                "navigate",
            )
            assert denied["error"]["code"] == "permission_denied"

    asyncio.run(run())


@pytest.mark.parametrize(
    "patch",
    [
        {"route": "/settings/workflows/graphs"},
        {"selected_job": {"job_id": 101}},
        {"selected_job": {"job_id": "101", "workflow_id": 41}},
        {"selected_job": {"job_id": 101, "workflow_id": 0}},
        {"filters": {"state": "completed"}},
        {"filters": {"trigger": "manual"}},
        {"filters": {"ordering": "graph__draft"}},
        {"filters": {"page_num": 0}},
        {"filters": {"page_size": 101}},
        {"selected_ids": [101]},
    ],
)
def test_context_contract(patch):
    body = request(context=CONTEXT)
    assert body.context.model_dump(mode="json") == CONTEXT
    with pytest.raises(ValidationError):
        request(context={**CONTEXT, **patch})


@pytest.mark.parametrize(
    ("selected", "error"),
    [
        (None, None),
        ({"job_id": 102, "workflow_id": 41}, None),
        ({"job_id": 101, "workflow_id": 42}, KaloscopeException),
        ({"job_id": 103, "workflow_id": 41}, BadRequestException),
        ({"job_id": 999, "workflow_id": 41}, BadRequestException),
    ],
)
def test_selection(selected, error):
    async def run():
        async with schedules() as fixture:
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), fixture.reader
            )
            body = request(context={**CONTEXT, "selected_job": selected})

            if error:
                with pytest.raises(error):
                    await Sessions.begin(conversation["id"], body, fixture.reader)
                assert not await AssistantTurn.exists()
            else:
                turn = await Sessions.begin(conversation["id"], body, fixture.reader)
                assert turn.resource_ids == ([41] if selected else [])
                assert turn.context["selected_job"] == selected

    asyncio.run(run())


@pytest.mark.parametrize("source", ["query", "reader_query", "selection"])
def test_history_permissions(monkeypatch, source):
    streams = []

    def handler(upstream):
        body = loads(upstream.content)
        streams.append(body)
        if source != "selection" and len(streams) <= 3:
            return response(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                call_delta(
                                    "list_workflow_jobs",
                                    dumps(
                                        {"page_num": len(streams), "page_size": 2}
                                    ).decode(),
                                )
                            ],
                        }
                    ),
                    chunk(finish="tool_calls"),
                ]
            )
        return response(
            [chunk({"role": "assistant", "content": "Private schedule facts"}, "stop")]
        )

    configure(monkeypatch, handler)

    async def run():
        async with schedules() as fixture:
            owner = fixture.owner if source == "query" else fixture.reader
            if source == "reader_query":
                owner.perms = Permissions(indexer_ids=[41, 42])
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            context = {
                **CONTEXT,
                "selected_job": {"job_id": 102, "workflow_id": 41}
                if source == "selection"
                else None,
            }
            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"], request(context=context), owner
                )
            ]

            assert events[-1].event == "done", events[-1]
            turn = await AssistantTurn.get(request_id="request")
            expected_resources = {
                "query": [41, 42, 43],
                "reader_query": [41, 42],
                "selection": [41],
            }[source]
            assert turn.resource_ids == expected_resources
            assert turn.context == context
            page = (await Sessions.turns(conversation["id"], owner))["items"][0]
            assert page["content"] == "Private schedule facts"
            original = await call(
                owner,
                {"turn_id": turn.id},
                "read_conversation_history",
                conversation["id"],
            )
            assert original["ok"]
            await AssistantConversation.filter(id=conversation["id"]).update(
                summary={"goals": ["Private schedule summary"]},
                summary_resources=turn.resource_ids,
                summary_through=turn.id,
            )

            owner.role, owner.perms = UserRole.USER, Permissions(indexer_ids=[42])
            current_jobs = await call(owner)
            assert current_jobs["data"]["total"] == 2
            assert {item["workflow_id"] for item in current_jobs["data"]["items"]} == {
                42
            }
            page = (await Sessions.turns(conversation["id"], owner))["items"][0]
            assert page["content"] == ""
            assert page["tools"] == []
            assert page["context"] is None
            assert page["error"] == "permission_denied"
            queries = [{"turn_id": turn.id}] + [
                {"tool_call_id": item.call_id}
                for item in await AssistantToolCall.filter(turn_id=turn.id)
            ]
            for args in queries:
                denied = await call(
                    owner, args, "read_conversation_history", conversation["id"]
                )
                assert denied["error"]["code"] == "permission_denied"
            index = await call(
                owner, {}, "read_conversation_history", conversation["id"]
            )
            assert index["data"]["items"] == []
            current = await Sessions.begin(conversation["id"], request("next"), owner)
            create = AsyncMock()
            messages, _, _ = await history.prepare_context(
                completion_client(create),
                current,
                owner,
                SYSTEM_PROMPT,
                tools.tool_schemas(owner),
                None,
            )
            assert "Private schedule" not in dumps(messages).decode()
            assert not any(
                "Current page at send time" in message["content"]
                for message in messages
            )
            assert (await Sessions.owned(conversation["id"], owner)).summary == {}
            create.assert_not_awaited()
            owner.perms = Permissions(indexer_ids=expected_resources)
            restored = (await Sessions.turns(conversation["id"], owner))["items"][0]
            assert restored["content"] == "Private schedule facts"
            original = await call(
                owner,
                {"turn_id": turn.id},
                "read_conversation_history",
                conversation["id"],
            )
            assert original["ok"]

    asyncio.run(run())


def test_query_budget(monkeypatch):
    streams, sizes = [], []

    def handler(upstream):
        body = loads(upstream.content)
        streams.append(body)
        sizes.append(history.context_size(body["messages"], body.get("tools", [])))
        assert sizes[-1] <= 32000 - 4096
        assert body.get("stream") is True
        if len(streams) == 1:
            context = loads(body["messages"][-2]["content"].split("\n", 1)[1])
            return response(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                call_delta(
                                    "list_workflow_jobs",
                                    dumps(context["filters"]).decode(),
                                )
                            ],
                        }
                    ),
                    chunk(finish="tool_calls"),
                ]
            )
        results = [
            loads(message["content"])
            for message in body["messages"]
            if message["role"] == "tool"
        ]
        assert len(results) == 1
        data = results[0]["data"]
        assert data["total"] == 15
        assert data["page_counts"]["returned"] == 15
        assert data["next_page_num"] is None
        assert data["timezone"] == "UTC"
        assert data["filters"]["name"] == "定时验收"
        assert {item["workflow_name"] for item in data["items"]} == {
            f"定时验收工作流 {index:02}" for index in range(15)
        }
        assert {item["trigger"] for item in data["items"]} == {
            "date",
            "cron",
            "interval",
        }
        facts = tool_facts(results[0])["data"]
        assert facts["total"] == 15 and facts["page_counts"]["returned"] == 15
        assert facts["filters"] == data["filters"]
        assert facts["timezone"] == "UTC"
        assert body.get("tool_choice") != "none"
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "content": "15 个计划已启用调度；执行情况需要读取日志。",
                    },
                    "stop",
                )
            ]
        )

    configure(monkeypatch, handler)

    async def run():
        async with database() as (owner, _):
            for index in range(15):
                graph = await FlowGraph.create(
                    name=f"定时验收工作流 {index:02}",
                    category="schedule",
                    state="published",
                )
                trigger, timing = [
                    ("date", {"run_date": datetime(2026, 10, 24, 8, tzinfo=UTC)}),
                    ("cron", {"cron_expr": "0 8 * * *"}),
                    ("interval", {"interval_num": 1, "interval_unit": "days"}),
                ][index % 3]
                await FlowJob.create(
                    graph=graph, state="running", trigger=trigger, **timing
                )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            context = {
                **CONTEXT,
                "filters": {
                    **CONTEXT["filters"],
                    "name": "定时验收",
                    "state": "running",
                    "ordering": "graph__name",
                },
            }
            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"], request(context=context), owner
                )
            ]

            assert events[-1].event == "done", events[-1]
            assert len(streams) == 2

    asyncio.run(run())
