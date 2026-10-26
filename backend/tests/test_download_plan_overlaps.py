"""Tests for shared plan listing and advisory overlap checks before plan creation."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from tortoise import timezone

from app.core.ai import history, tools
from app.models.assistant import AssistantTurn, ConversationCreate
from app.models.download import DownloadPlan, DownloadTask, TransferMethod
from app.models.flow import FlowGraph, FlowLog
from app.models.media import MediaItem, MediaLib
from app.models.user import Permissions, PermType, UserPermission, UserRole
from app.services import download as service
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_conversation import engine_fixture
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response


async def overlaps():
    """Create indexers, plans and media that may overlap with a new Lantern plan.

    Returns:
        Created indexers, libraries, plans, existing media and a creation request.
    """
    graph, downloader, body = await fixture_plan()
    other = await FlowGraph.create(
        name="Other indexer",
        category="indexer",
        state="published",
        definition=graph.definition,
    )
    draft = await FlowGraph.create(
        name="Draft indexer", category="indexer", state="draft"
    )
    movies = await MediaLib.create(
        lib_type="movie", dir="/synthetic/movies", name="Movies", priority=1
    )
    hidden = await MediaLib.create(
        lib_type="movie", dir="/synthetic/hidden", name="Hidden", priority=2
    )
    now = timezone.now()
    base = {
        "downloader_id": downloader.id,
        "dir": "/synthetic/downloads",
        "interval_num": 1,
        "batch_limit": 1,
    }
    plans = {
        "same": await DownloadPlan.create(
            graph=graph, keyword="Lantern", total_limit=1, **base
        ),
        "other": await DownloadPlan.create(
            graph=other,
            keyword="Lantern 2019",
            interval_end=now - timedelta(days=1),
            transfer_lib=movies,
            transfer_method=TransferMethod.HARDLINK,
            total_count=1,
            last_exec=now - timedelta(days=2),
            creation_receipt={"digest": "x", "first_execution": {"status": "added"}},
            **base,
        ),
        "unrelated": await DownloadPlan.create(
            graph=other, keyword="Harbor", total_limit=1, total_count=1, **base
        ),
        "future": await DownloadPlan.create(
            graph=graph,
            keyword="The Lantern Saga",
            interval_start=now + timedelta(days=1),
            transfer_lib=hidden,
            transfer_method=TransferMethod.COPY,
            **base,
        ),
        "draft": await DownloadPlan.create(graph=draft, keyword="lantern", **base),
    }
    item = await MediaItem.create(
        lib=movies,
        dir=movies.dir,
        path=f"{movies.dir}/Lantern (2019).mkv",
        name="Lantern (2019).mkv",
        title="Lantern",
        year=2019,
    )
    return SimpleNamespace(
        graph=graph,
        other=other,
        draft=draft,
        movies=movies,
        hidden=hidden,
        plans=plans,
        item=item,
        body=body,
    )


async def listed(user, **arguments):
    """List plans through the assistant tool.

    Args:
        user: Account calling the tool.
        **arguments: Tool arguments.

    Returns:
        Tool result.
    """
    return await tools.execute_tool(
        "list_download_plans",
        dumps(arguments).decode(),
        user,
        conversation_id=1,
        call_id="plans",
    )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, {"same", "other", "unrelated", "future", "draft"}),
        ({"keyword": "LANTERN"}, {"same", "other", "future", "draft"}),
        ({"workflow": "other"}, {"other", "unrelated"}),
        ({"keyword": "lantern", "workflow": "graph"}, {"same", "future"}),
        ({"keyword": "Missing"}, set()),
    ],
    ids=["all", "keyword", "indexer", "both", "empty"],
)
def test_filters(arguments, expected):
    async def run():
        async with database() as (owner, _):
            data = await overlaps()
            if workflow := arguments.pop("workflow", None):
                arguments["workflow_id"] = getattr(data, workflow).id

            result = await listed(owner, **arguments, page_size=25)

            assert result["ok"], result
            page = result["data"]
            ids = {plan.id: name for name, plan in data.plans.items()}
            assert {ids[item["plan_id"]] for item in page["items"]} == expected
            assert page["total"] == page["page_counts"]["returned"] == len(expected)
            assert page["next_page_num"] is None
            assert page["filters"] == {
                "workflow_id": arguments.get("workflow_id"),
                "keyword": arguments.get("keyword"),
                "ordering": "",
            }

    asyncio.run(run())


@pytest.mark.parametrize("descending", [False, True])
def test_ordering_and_pages(descending):
    async def run():
        async with database() as (owner, _):
            data = await overlaps()
            ordering = "-keyword" if descending else "keyword"
            keywords = sorted(plan.keyword for plan in data.plans.values())
            if descending:
                keywords.reverse()

            pages = [
                (await listed(owner, ordering=ordering, page_num=number, page_size=2))[
                    "data"
                ]
                for number in (1, 2, 3, 4)
            ]

            assert [
                item["keyword"] for page in pages for item in page["items"]
            ] == keywords
            assert [page["total"] for page in pages] == [5] * 4
            assert [page["page_counts"]["returned"] for page in pages] == [2, 2, 1, 0]
            assert [page["next_page_num"] for page in pages] == [2, 3, None, None]
            assert pages[0]["filters"]["ordering"] == ordering

    asyncio.run(run())


def test_projection():
    async def run():
        async with database() as (owner, _):
            data = await overlaps()
            await DownloadPlan.filter(id=data.plans["unrelated"].id).update(
                keyword="Harbor " * 60, filters={"group": "x" * 400}
            )

            page = (await listed(owner, page_size=25))["data"]
            items = {item["plan_id"]: item for item in page["items"]}
            facts = {name: items[plan.id] for name, plan in data.plans.items()}

            assert page["interval_unit"] == "hours" and page["timezone"] == "UTC"
            assert {name: item["schedule"] for name, item in facts.items()} == {
                "same": "active",
                "other": "ended",
                "unrelated": "limit_reached",
                "future": "not_started",
                "draft": "indexer_unpublished",
            }
            other = facts["other"]
            assert other["plan_id"] == data.plans["other"].id
            assert other["workflow_id"] == data.other.id
            assert other["workflow_name"] == "Other indexer"
            assert other["transfer_lib_id"] == data.movies.id
            assert other["total_count"] == 1 and other["last_exec"] is not None
            assert other["first_execution"] == {"status": "added"}
            assert facts["same"]["first_execution"] is None
            assert facts["unrelated"]["keyword"]["truncated"]
            assert facts["unrelated"]["filters"]["truncated"]
            assert len(dumps(page)) < 8000

    asyncio.run(run())


def test_permissions():
    async def run():
        async with database() as (owner, _):
            data = await overlaps()
            owner.role = UserRole.USER
            owner.perms = Permissions(
                indexer_ids=[data.graph.id, data.other.id],
                media_lib_ids=[data.movies.id],
            )

            visible = (await listed(owner, keyword="lantern"))["data"]
            denied = await listed(owner, workflow_id=data.draft.id)
            missing = await listed(owner, workflow_id=9999)
            owner.perms.media_lib_ids = []
            narrowed = (await listed(owner, keyword="lantern"))["data"]

            ids = {plan.id: name for name, plan in data.plans.items()}
            # draft-indexer and hidden-library plans stay out of the exact total
            assert {ids[item["plan_id"]] for item in visible["items"]} == {
                "same",
                "other",
            }
            assert visible["total"] == 2
            assert denied["error"]["code"] == "permission_denied"
            assert missing["error"]["code"] == "not_found"
            assert narrowed["total"] == 1
            assert [ids[item["plan_id"]] for item in narrowed["items"]] == ["same"]

    asyncio.run(run())


def test_mcp_parity():
    async def run():
        async with application() as app:
            data = await overlaps()
            reader = app.shared_ctx.sessions["reader"]
            for rel_type, rel_id in (
                (PermType.INDEXER, data.graph.id),
                (PermType.INDEXER, data.other.id),
                (PermType.MEDIA_LIB, data.movies.id),
            ):
                await UserPermission.create(
                    user_id=reader.id, rel_type=rel_type, rel_id=rel_id
                )
            before = await DownloadPlan.all().values()
            arguments = {"keyword": "Lantern", "ordering": "-keyword", "page_size": 2}

            for account, permissions in (
                (
                    "reader",
                    Permissions(
                        indexer_ids=[data.graph.id, data.other.id],
                        media_lib_ids=[data.movies.id],
                    ),
                ),
                ("admin", None),
            ):
                session = app.shared_ctx.sessions[account].model_copy(
                    update={"perms": permissions}
                )
                mcp = await call(app, "list_download_plans", arguments, token=account)
                assistant = await listed(session, **arguments)

                assert not mcp.get("isError"), mcp
                assert mcp["structuredContent"] == loads(dumps(assistant["data"]))
            denied = await call(
                app, "list_download_plans", {"workflow_id": data.draft.id}
            )

            assert mcp["structuredContent"]["total"] == 4
            assert denied["isError"]
            assert await DownloadPlan.all().values() == before

    asyncio.run(run())


def test_route_parity():
    async def run():
        async with database() as (owner, _):
            data = await overlaps()
            owner.role = UserRole.USER
            owner.perms = Permissions(indexer_ids=[data.other.id])

            page = await service.DownloadPlanService.list_plans(
                service.DownloadPlanQuery(keyword="2019"), owner
            )
            listed_page = (await listed(owner, keyword="2019"))["data"]

            # the page keeps its existing fields and gains the stored schedule state
            assert page["total"] == listed_page["total"] == 0
            owner.perms.media_lib_ids = [data.movies.id]
            page = await service.DownloadPlanService.list_plans(
                service.DownloadPlanQuery(keyword="2019"), owner
            )
            assert page["total"] == 1
            plan = page["items"][0]
            assert plan["graph_name"] == "Other indexer" and not plan["running"]
            assert plan["schedule"] == "ended"
            assert plan["first_execution"] == {"status": "added"}

    asyncio.run(run())


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_overlap_conversation(monkeypatch, verbose):
    fixture, identities = {}, []
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
                                        "goals": ["Plan Lantern after overlap checks"],
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
        unlock = [
            ("get_workflow", {"workflow_id": flow}),
            ("get_download_plan_options", {"workflow_id": flow}),
        ]
        actions = [
            [("list_workflows", {"category": "indexer"})],
            [
                *unlock,
                ("search_indexer", {"workflow_id": flow, "keyword": "Lantern"}),
                ("list_media_items", {"keyword": "Lantern"}),
                ("list_download_plans", {"keyword": "Lantern"}),
            ],
            [*unlock, ("validate_download_plan", {"plan": fixture["plan"]})],
            [
                *unlock,
                *[
                    (
                        "create_download_plan",
                        {
                            "request_id": identities[0] if identities else "",
                            "confirmed": True,
                        },
                    )
                ]
                * 2,
            ],
        ][number]
        if step >= len(actions):
            content = [
                "Choose the published indexer for Lantern.",
                "Found Lantern 2025. Possible overlaps: your library has Lantern "
                "(2019) and plans exist for Lantern on this and another indexer. "
                "Is this the 2025 film, or do you want to view the existing ones?",
                "Final settings and defaults; the library item and existing plans "
                "are possible overlaps (2019 differs). View them, or confirm this "
                "additional plan?",
                "Created one additional plan.",
            ][number]
            return response([chunk({"role": "assistant", "content": content}, "stop")])
        name, args = actions[step]
        if step >= 2 and number:
            names = {item["function"]["name"] for item in body["tools"]}
            assert {"list_download_plans", "list_media_items"} <= names
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
            data = await overlaps()
            options = await service.DownloadPlanService.authoring_options(
                data.graph.id, owner
            )
            rules = options["creation_rules"]
            assert "list_download_plans without workflow_id" in rules
            assert "Never reject" in rules and "list_media_items" in rules
            fixture.update(
                graph_id=data.graph.id,
                plan={
                    **options["defaults"],
                    "graph_id": data.graph.id,
                    "keyword": "Lantern",
                },
            )
            existing = await DownloadPlan.all().order_by("id").values()
            media = await MediaItem.all().values()
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            results = []
            for number, prompt in enumerate(
                [
                    "我要下载 Lantern",
                    "选择 Published indexer",
                    "是 2025 年的新片，其他用默认值",
                    "仍然确认创建这个计划",
                ]
            ):
                stage.update(number=number, step=0)
                events = [
                    event
                    async for event in AssistantService.persistent_stream(
                        conversation["id"],
                        request(f"step-{number}", prompt=prompt),
                        owner,
                    )
                ]
                assert events[-1].event == "done", events[-1]
                turn = [e.data for e in events if e.event == "tool_result"]
                assert all(result["ok"] for result in turn), turn
                results.extend(turn)
                if number == 2:
                    identities.append(turn[-1]["data"]["request_id"])
                if number < 3:
                    assert await DownloadPlan.all().count() == len(existing)
                    service.notify_download_changes.assert_not_called()

            by_name = {}
            for result in results:
                by_name.setdefault(result["name"], []).append(result["data"])
            plans = by_name["list_download_plans"][0]
            found = by_name["list_media_items"][0]
            # the selected indexer does not limit overlap checks
            assert {item["workflow_id"] for item in plans["items"]} == {
                data.graph.id,
                data.other.id,
                data.draft.id,
            }
            assert [(item["title"], item["year"]) for item in found["items"]] == [
                ("Lantern", 2019)
            ]
            # one immediate search; overlap reads and later turns reused it
            assert execute.await_count == 1
            assert execute.call_args.args[1]["keyword"] == "Lantern"
            assert len(by_name["search_indexer"]) == 1
            assert len(by_name["list_download_plans"]) == 1
            created = by_name["create_download_plan"]
            assert created[0]["created"] and created[1]["replayed"]
            assert created[0]["plan_id"] == created[1]["plan_id"]
            assert await DownloadPlan.all().count() == len(existing) + 1
            assert (
                await DownloadPlan.filter(id__not=created[0]["plan_id"])
                .order_by("id")
                .values()
                == existing
            )
            assert await MediaItem.all().values() == media
            assert await DownloadTask.all().count() == await FlowLog.all().count() == 0
            service.notify_download_changes.assert_called_once()

            checked = await AssistantTurn.get(request_id="step-1")
            assert {data.other.id, data.draft.id} <= set(checked.resource_ids)
            assert {
                f"media_lib:{data.movies.id}",
                f"media_lib:{data.hidden.id}",
            } <= set(checked.resource_ids)
            owner.role = UserRole.USER
            owner.perms = Permissions(
                indexer_ids=[data.graph.id], media_lib_ids=[data.movies.id]
            )
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": checked.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="read",
            )
            assert denied["error"]["code"] == "permission_denied"

    asyncio.run(run())
