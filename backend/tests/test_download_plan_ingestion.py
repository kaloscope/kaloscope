"""Tests for movie ingestion bindings set up with conversational download plans."""

import asyncio
import mimetypes
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from lxml import etree
from sanic import Sanic
from tortoise.exceptions import IntegrityError

from app.core.ai import history, tools
from app.core.constants import NFO_MIME_TYPE
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.media import watcher
from app.models.assistant import AssistantTurn, ConversationCreate
from app.models.download import (
    DownloadPlan,
    DownloadPlanCheck,
    DownloadPlanCreate,
    DownloadState,
    DownloadTask,
    TransferMethod,
)
from app.models.flow import FlowGraph, FlowLog, FlowTrigger
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import Permissions, UserRole
from app.services import download as service
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.mcp import MCPAccessTokenService
from app.services.media import MediaLibService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_conversation import engine_fixture
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_services_media_index import _ingest_engine
from tests.test_workflow_outputs import HTML_FORMATTER, MOVIE_HTML, MOVIE_TEMPLATE


def scraper(output="movie"):
    """Build an `ingest_start -> http -> metadata` definition.

    Args:
        output: Metadata node type at the end of the path.

    Returns:
        Saved graph definition using the movie recipe's tested templates.
    """
    nodes = [
        ("start", {"$schema": "ingest_start"}),
        (
            "http",
            {
                "$schema": "http",
                "url": "https://movies.example.test/search?q={{ title|quote('') }}",
                "formatter": HTML_FORMATTER,
            },
        ),
        ("end", {"$schema": output, "response": MOVIE_TEMPLATE, "force_end": False}),
    ]
    return {
        "nodes": [{"id": key, "type": "node", "data": data} for key, data in nodes],
        "edges": [
            {
                "id": f"{source}-{target}",
                "source": source,
                "sourceHandle": "output",
                "target": target,
                "targetHandle": "input",
            }
            for source, target in (("start", "http"), ("http", "end"))
        ],
    }


async def libraries(tmp_path, *, published=True):
    """Create libraries, ingestion workflows and existing bindings.

    Args:
        tmp_path: Isolated directory for library roots.
        published: Whether a usable movie scraper is already published.

    Returns:
        Created indexer inputs, libraries, workflows and existing bindings.
    """
    graph, downloader, body = await fixture_plan()
    movies = await MediaLib.create(
        lib_type=LibType.MOVIE, dir=str(tmp_path / "movies"), name="Movies", priority=1
    )
    shows = await MediaLib.create(
        lib_type=LibType.TV_SHOW, dir=str(tmp_path / "shows"), name="Shows", priority=2
    )
    tv = await FlowGraph.create(
        name="TV scraper",
        category="ingest",
        state="published",
        definition=scraper("tv_show"),
    )
    draft = await FlowGraph.create(
        name="Draft scraper", category="ingest", state="draft", draft=scraper()
    )
    movie = (
        await FlowGraph.create(
            name="Movie scraper",
            category="ingest",
            state="published",
            definition=scraper(),
        )
        if published
        else None
    )
    existing = [
        await FlowTrigger.create(
            category="ingest", rel_id=movies.id, graph=tv, priority=1, asynchronous=True
        ),
        await FlowTrigger.create(
            category="ingest", rel_id=movies.id, graph=draft, priority=2
        ),
    ]
    body.plan.transfer_lib_id = movies.id
    body.plan.transfer_method = TransferMethod.COPY
    return SimpleNamespace(
        graph=graph,
        downloader=downloader,
        body=body,
        movies=movies,
        shows=shows,
        tv=tv,
        draft=draft,
        movie=movie,
        existing=existing,
    )


async def bindings(lib_id):
    """Read stored bindings in run order.

    Args:
        lib_id: Library whose bindings are read.

    Returns:
        Workflow, priority and asynchronous flag of each binding.
    """
    return [
        (row.graph_id, row.priority, row.asynchronous)
        for row in await FlowTrigger.filter(rel_id=lib_id).order_by("priority")
    ]


async def reviewed(owner, data, workflow_id=None, **plan):
    """Validate the fixture plan with an ingestion workflow.

    Args:
        owner: Administrator account.
        data: Created fixtures.
        workflow_id: Ingestion workflow; defaults to the published movie scraper.
        **plan: Plan settings that differ from the fixture.

    Returns:
        Shared validation result.
    """
    return await service.DownloadPlanService.validate_creation(
        DownloadPlanCheck(
            plan=data.body.plan.model_copy(update=plan),
            ingest_workflow_id=workflow_id or data.movie.id,
        ),
        owner,
    )


def creation(data, validation, *, request_id=None, confirmed=True):
    """Build the confirmed creation for a reviewed validation.

    Args:
        data: Created fixtures.
        validation: Result containing the ingestion review.
        request_id: Plan request identity; defaults to the fixture request.
        confirmed: Whether the user approved the settings.

    Returns:
        Shared creation request.
    """
    return DownloadPlanCreate(
        request_id=request_id or data.body.request_id,
        plan=data.body.plan,
        ingestion={
            "workflow_id": validation["ingestion"]["workflow_id"],
            "revision": validation["ingestion"]["revision"],
        },
        confirmed=confirmed,
    )


def test_inspect(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            before = await bindings(data.movies.id)

            result = await MediaLibService.ingestion_bindings(data.movies.id, owner)
            shows = await MediaLibService.ingestion_bindings(data.shows.id, owner)

            assert result["supported"] and shows["supported"]
            # the TV fixture reaches tv_show only, so it cannot serve episodes
            assert shows["workflows"] == [] and shows["bindings"] == []
            assert [
                (b["workflow_id"], b["order"], b["asynchronous"], b["executable"])
                for b in result["bindings"]
            ] == [(data.tv.id, 1, True, True), (data.draft.id, 2, False, False)]
            assert not any(b["compatible"] for b in result["bindings"])
            assert result["workflows"] == [
                {"workflow_id": data.movie.id, "workflow_name": "Movie scraper"}
            ]
            assert [w["workflow_id"] for w in result["unpublished_workflows"]] == [
                data.draft.id
            ]
            assert "including future imports" in result["scope"]
            assert "unpublished workflows never run" in result["rules"]
            assert await bindings(data.movies.id) == before
            owner.role = UserRole.USER
            owner.perms = Permissions(media_lib_ids=[data.movies.id])
            with pytest.raises(ForbiddenException):
                await MediaLibService.ingestion_bindings(data.movies.id, owner)
            owner.role = UserRole.ADMIN
            with pytest.raises(NotFoundException):
                await MediaLibService.ingestion_bindings(9999, owner)

    asyncio.run(run())


@pytest.mark.parametrize(
    ("workflow", "plan", "path"),
    [
        ("draft", {}, "ingest_workflow_id"),
        ("tv", {}, "ingest_workflow_id"),
        ("graph", {}, "ingest_workflow_id"),
        ("movie", {"transfer_lib_id": "shows"}, "ingest_workflow_id"),
        (
            "movie",
            {"transfer_lib_id": None, "transfer_method": None},
            "transfer_lib_id",
        ),
    ],
    ids=["unpublished", "tv-output", "indexer", "tv-library", "no-library"],
)
def test_validation_issues(tmp_path, workflow, plan, path):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            if plan.get("transfer_lib_id") == "shows":
                plan["transfer_lib_id"] = data.shows.id
            before = await bindings(data.movies.id)

            result = await reviewed(owner, data, getattr(data, workflow).id, **plan)

            assert not result["valid"]
            assert path in [issue["path"] for issue in result["issues"]]
            assert await bindings(data.movies.id) == before
            assert not await DownloadPlan.exists()

    asyncio.run(run())


def test_missing_workflow(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            with pytest.raises(NotFoundException):
                await reviewed(owner, data, 9999)

    asyncio.run(run())


def test_add_then_reuse(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            before = await bindings(data.movies.id)

            validation = await reviewed(owner, data)
            preview = await service.DownloadPlanService.create_once(
                creation(data, validation, confirmed=False), owner
            )
            unchanged = await bindings(data.movies.id)
            created = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            replayed = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            recovered = await service.DownloadPlanService.get_creation(
                created["plan_id"], owner
            )
            assert recovered["ingestion"]["binding"] == "configured"
            second = await reviewed(owner, data, keyword="Lantern extended")
            other = await service.DownloadPlanService.create_once(
                creation(data, second, request_id=uuid4()).model_copy(
                    update={
                        "plan": data.body.plan.model_copy(
                            update={"keyword": "Lantern extended"}
                        )
                    }
                ),
                owner,
            )

            assert validation["valid"] and validation["ingestion"]["binding"] == "add"
            assert preview["confirmation_required"] and unchanged == before
            assert created["created"] and created["ingestion"]["binding"] == "added"
            assert await bindings(data.movies.id) == [
                *before,
                (data.movie.id, 3, False),
            ]
            # the library is prepared before the plan can queue its first attempt
            added = await FlowTrigger.get(rel_id=data.movies.id, graph=data.movie)
            plan = await DownloadPlan.get(id=created["plan_id"])
            assert added.created_at <= plan.created_at
            assert replayed["replayed"] and replayed["plan_id"] == created["plan_id"]
            assert replayed["ingestion"]["binding"] == "configured"
            assert second["ingestion"]["binding"] == "reuse"
            assert other["created"] and other["ingestion"]["binding"] == "reused"
            assert len(await bindings(data.movies.id)) == 3
            assert await DownloadPlan.all().count() == 2
            assert plan.transfer_lib_id == data.movies.id
            assert plan.creation_receipt["first_execution"]["status"] == "queued"
            owner.role = UserRole.USER
            owner.perms = Permissions(
                indexer_ids=[data.graph.id], media_lib_ids=[data.movies.id]
            )
            ordinary = await service.DownloadPlanService.get_creation(plan.id, owner)
            assert ordinary["created"] and "ingestion" not in ordinary
            assert "creation_receipt" not in ordinary["plan"]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["added", "reordered", "edited", "removed"])
@pytest.mark.parametrize("bound", [False, True], ids=["add", "reuse"])
def test_changed_bindings(tmp_path, change, bound):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            if bound:
                await FlowTrigger.create(
                    category="ingest",
                    rel_id=data.movies.id,
                    graph=data.movie,
                    priority=3,
                )
            validation = await reviewed(owner, data)
            if change == "added":
                other = await FlowGraph.create(
                    name="Other movie scraper",
                    category="ingest",
                    state="published",
                    definition=scraper(),
                )
                await FlowTrigger.create(
                    category="ingest", rel_id=data.movies.id, graph=other, priority=4
                )
            elif change == "reordered":
                await FlowTrigger.filter(id=data.existing[1].id).update(priority=5)
            elif change == "edited":
                await FlowTrigger.filter(id=data.existing[0].id).update(
                    asynchronous=False
                )
            else:
                await FlowTrigger.filter(id=data.existing[1].id).delete()
            current = await bindings(data.movies.id)

            result = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )

            assert not result["created"] and not result["valid"]
            assert result["ingestion"]["binding"] == "conflict"
            assert "Validate again" in result["ingestion"]["message"]
            assert await bindings(data.movies.id) == current
            assert not await DownloadPlan.exists()
            service.notify_download_changes.assert_not_called()

    asyncio.run(run())


def test_rechecked_configuration(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            validation = await reviewed(owner, data)
            before = await bindings(data.movies.id)
            await FlowGraph.filter(id=data.movie.id).update(state="draft")

            unpublished = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            await FlowGraph.filter(id=data.movie.id).update(state="published")
            owner.role = UserRole.USER
            owner.perms = Permissions(
                indexer_ids=[data.graph.id], media_lib_ids=[data.movies.id]
            )
            with pytest.raises(ForbiddenException):
                await service.DownloadPlanService.create_once(
                    creation(data, validation), owner
                )

            assert not unpublished["created"]
            assert "ingest_workflow_id" in [i["path"] for i in unpublished["issues"]]
            assert await bindings(data.movies.id) == before
            assert not await DownloadPlan.exists()

    asyncio.run(run())


def test_binding_failure(tmp_path, monkeypatch):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            validation = await reviewed(owner, data)
            monkeypatch.setattr(
                FlowTrigger, "create", AsyncMock(side_effect=RuntimeError("disk"))
            )

            with pytest.raises(RuntimeError):
                await service.DownloadPlanService.create_once(
                    creation(data, validation), owner
                )

            assert not await DownloadPlan.exists()
            service.notify_download_changes.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [RuntimeError, IntegrityError])
def test_plan_failure_after_binding(tmp_path, monkeypatch, failure):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            validation = await reviewed(owner, data)
            original = DownloadPlan.create
            attempts = []

            async def flaky(**values):
                attempts.append(values["keyword"])
                if len(attempts) == 1:
                    raise failure("plan storage unavailable")
                return await original(**values)

            monkeypatch.setattr(DownloadPlan, "create", flaky)

            failed = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            kept = await bindings(data.movies.id)
            retried = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )

            assert not failed["created"] and failed["valid"]
            assert failed["ingestion"]["binding"] == "added"
            assert "retry with the same request_id" in failed["plan_error"]
            assert kept[-1] == (data.movie.id, 3, False)
            assert retried["created"] and not retried["replayed"]
            assert retried["ingestion"]["binding"] == "reused"
            assert await bindings(data.movies.id) == kept
            assert await DownloadPlan.all().count() == 1
            service.notify_download_changes.assert_called_once()

    asyncio.run(run())


def test_retry_after_binding_edit(tmp_path, monkeypatch):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            validation = await reviewed(owner, data)
            original = DownloadPlan.create
            monkeypatch.setattr(
                DownloadPlan, "create", AsyncMock(side_effect=RuntimeError("disk"))
            )
            failed = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            assert not failed["created"] and failed["ingestion"]["binding"] == "added"
            await FlowTrigger.filter(
                rel_id=data.movies.id, graph_id=data.movie.id
            ).update(asynchronous=True)
            changed = await bindings(data.movies.id)
            monkeypatch.setattr(DownloadPlan, "create", original)

            result = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )

            assert (
                not result["created"] and result["ingestion"]["binding"] == "conflict"
            )
            assert await bindings(data.movies.id) == changed
            assert not await DownloadPlan.exists()

    asyncio.run(run())


def test_concurrent_retries(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            validation = await reviewed(owner, data)

            results = await asyncio.gather(
                *[
                    service.DownloadPlanService.create_once(
                        creation(data, validation), owner
                    )
                    for _ in range(3)
                ]
            )

            assert all(result["created"] for result in results)
            assert len({result["plan_id"] for result in results}) == 1
            assert await DownloadPlan.all().count() == 1
            assert [row[0] for row in await bindings(data.movies.id)].count(
                data.movie.id
            ) == 1

    asyncio.run(run())


def test_mcp_scopes_and_parity(tmp_path):
    async def run():
        async with application() as app:
            data = await libraries(tmp_path)
            admin = app.shared_ctx.sessions["admin"]
            arguments = {"library_id": data.movies.id}
            original = await bindings(data.movies.id)

            mcp = await call(app, "get_library_ingestion", arguments, token="admin")
            assistant = await tools.execute_tool(
                "get_library_ingestion",
                dumps(arguments).decode(),
                admin,
                conversation_id=1,
                call_id="ingestion",
            )
            reader = await call(app, "get_library_ingestion", arguments)
            denied = await tools.execute_tool(
                "get_library_ingestion",
                dumps(arguments).decode(),
                app.shared_ctx.sessions["reader"],
                conversation_id=1,
                call_id="reader",
            )
            check = {
                "plan": data.body.plan.model_dump(mode="json"),
                "ingest_workflow_id": data.movie.id,
            }
            validated = await call(
                app, "validate_download_plan", {"validation": check}, token="admin"
            )
            review = validated["structuredContent"]["ingestion"]
            body = {
                "request_id": str(data.body.request_id),
                "plan": check["plan"],
                "ingestion": {
                    "workflow_id": review["workflow_id"],
                    "revision": review["revision"],
                },
                "confirmed": True,
            }
            read_only = await call(
                app, "create_download_plan", {"creation": body}, token="admin"
            )
            unchanged = await bindings(data.movies.id)
            planned = await DownloadPlan.exists()
            write = await MCPAccessTokenService.create(
                admin.id,
                MCPAccessTokenCreate(name="plans", scope=MCPTokenScope.READ_WRITE),
            )
            created = await call(
                app, "create_download_plan", {"creation": body}, token=write["token"]
            )

            assert not mcp.get("isError"), mcp
            assert mcp["structuredContent"] == loads(dumps(assistant["data"]))
            # ordinary accounts are never offered the administrator-only tool
            assert reader["isError"] and denied["error"]["code"] == "forbidden"
            assert review["binding"] == "add"
            assert read_only["isError"] and "read-only" in str(read_only)
            assert unchanged == original and not planned
            assert not created.get("isError"), created
            assert created["structuredContent"]["ingestion"]["binding"] == "added"
            assert await bindings(data.movies.id) == [
                *original,
                (data.movie.id, 3, False),
            ]

    asyncio.run(run())


def test_missing_scraper_draft(tmp_path, monkeypatch):
    fixture = {}
    step = 0

    def handler(upstream):
        nonlocal step
        body = loads(upstream.content)
        assert history.context_size(body["messages"], body.get("tools", [])) <= 27904
        actions = [
            ("get_workflow", {"workflow_id": fixture["graph_id"]}),
            ("get_download_plan_options", {"workflow_id": fixture["graph_id"]}),
            ("get_library_ingestion", {"library_id": fixture["library_id"]}),
            ("get_workflow_reference", {"topic": "movie_scraper"}),
            (
                "create_workflow_draft",
                {
                    "name": "Requested movie scraper",
                    "category": "ingest",
                    "description": "Read movie metadata from the supplied HTML source.",
                },
            ),
        ]
        if step == len(actions):
            return response(
                [chunk({"role": "assistant", "content": "Draft ready."}, "stop")]
            )
        name, arguments = actions[step]
        step += 1
        assert name in {item["function"]["name"] for item in body["tools"]}
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            call_delta(name, dumps(arguments).decode(), id=name)
                        ],
                    },
                    "tool_calls",
                )
            ]
        )

    configure(monkeypatch, handler)

    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path, published=False)
            fixture.update(graph_id=data.graph.id, library_id=data.movies.id)
            before = await bindings(data.movies.id)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )

            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request(
                        "scraper",
                        prompt="Create the missing scraper from my HTML sample, "
                        "then resume the Lantern plan.",
                    ),
                    owner,
                )
            ]

            assert events[-1].event == "done", events[-1]
            created = [
                e.data
                for e in events
                if e.event == "tool_result"
                and e.data["name"] == "create_workflow_draft"
            ][0]
            assert created["ok"]
            graph = await FlowGraph.get(id=created["data"]["workflow_id"])
            assert graph.category == "ingest" and graph.state == "draft"
            assert graph.definition is None and graph.draft == {
                "nodes": [],
                "edges": [],
            }
            assert await bindings(data.movies.id) == before
            assert not await DownloadPlan.exists()
            service.notify_download_changes.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_ingestion_conversation(tmp_path, monkeypatch, verbose):
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
                                        "goals": ["Download Lantern into Movies"],
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
        flow, library = fixture["graph_id"], fixture["library_id"]
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
                ("list_media_libraries", {}),
                ("get_library_ingestion", {"library_id": library}),
            ],
            [
                *unlock,
                ("get_library_ingestion", {"library_id": library}),
                (
                    "validate_download_plan",
                    {
                        "plan": fixture["plan"],
                        "ingest_workflow_id": fixture.get("workflow_id", 1),
                    },
                ),
            ],
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
            if number == 1:
                names = {item["function"]["name"] for item in body["tools"]}
                assert {"create_workflow_draft", "get_workflow_reference"} <= names
            content = [
                "Choose the published indexer for Lantern.",
                "Found Lantern; no overlaps. Movies has no published movie scraper. "
                "Which site should the scraper read?",
                "Final settings: Movies via copy, Movie scraper added after the "
                "existing bindings for all new items in Movies. Confirm?",
                "Created the plan and binding; downloads and ingestion continue "
                "in the background.",
            ][number]
            return response([chunk({"role": "assistant", "content": content}, "stop")])
        name, args = actions[step]
        if step >= 2 and number:
            names = {item["function"]["name"] for item in body["tools"]}
            assert "get_library_ingestion" in names
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
        {"items": [{"id": "r1", "title": "Lantern 2025"}], "total": 1},
    )
    service.Sanic.get_app().ctx.lib_watcher = SimpleNamespace(
        is_scanning=lambda _: False
    )

    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path, published=False)
            options = await service.DownloadPlanService.authoring_options(
                data.graph.id, owner
            )
            assert "get_library_ingestion" in options["creation_rules"]
            fixture.update(
                graph_id=data.graph.id,
                library_id=data.movies.id,
                plan={
                    **options["defaults"],
                    "graph_id": data.graph.id,
                    "keyword": "Lantern",
                    "transfer_lib_id": data.movies.id,
                    "transfer_method": "copy",
                },
            )
            before = await bindings(data.movies.id)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            results = []
            for number, prompt in enumerate(
                [
                    "下载 Lantern，完成后加入电影库",
                    "选择 Published indexer",
                    "我已保存并发布电影刮削工作流",
                    "确认",
                ]
            ):
                if number == 2:
                    # the user built, saved and published the scraper in the designer
                    published = await FlowGraph.create(
                        name="Movie scraper",
                        category="ingest",
                        state="published",
                        definition=scraper(),
                    )
                    fixture["workflow_id"] = published.id
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
                    assert await bindings(data.movies.id) == before
                    assert not await DownloadPlan.exists()

            by_name = {}
            for result in results:
                by_name.setdefault(result["name"], []).append(result["data"])
            first, second = by_name["get_library_ingestion"]
            assert first["workflows"] == [] and first["unpublished_count"] == 1
            assert [w["workflow_id"] for w in second["workflows"]] == [
                fixture["workflow_id"]
            ]
            review = by_name["validate_download_plan"][0]["ingestion"]
            assert review["binding"] == "add" and "not only" in review["scope"]
            created = by_name["create_download_plan"][0]
            assert created["created"] and created["ingestion"]["binding"] == "added"
            replayed = by_name["create_download_plan"][1]
            assert replayed["replayed"] and replayed["plan_id"] == created["plan_id"]
            assert replayed["ingestion"]["binding"] == "configured"
            assert await bindings(data.movies.id) == [
                *before,
                (fixture["workflow_id"], 3, False),
            ]
            # creation returns once queued; no search, download or ingestion ran
            assert execute.await_count == 1
            assert created["first_execution"]["status"] == "queued"
            assert await DownloadTask.all().count() == await FlowLog.all().count() == 0
            service.notify_download_changes.assert_called_once()

            checked = await AssistantTurn.get(request_id="step-2")
            assert f"media_lib:{data.movies.id}" in checked.resource_ids
            assert fixture["workflow_id"] in checked.resource_ids
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


def test_completion_to_ingestion(tmp_path, monkeypatch):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "Lantern.2024.mkv").write_bytes(b"video")
    remote = []

    def site(request):
        remote.append(request.url.params["q"])
        return httpx.Response(200, text=MOVIE_HTML)

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(site)) as client,
        ):
            app = SimpleNamespace(ctx=SimpleNamespace(httpx=client))
            monkeypatch.setattr(Sanic, "get_app", lambda *_, **__: app)
            # the real engine runs synchronous bindings; asynchronous ones queue
            _ingest_engine(monkeypatch)
            data = await libraries(tmp_path)
            (tmp_path / "movies").mkdir()
            validation = await reviewed(owner, data)
            created = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            # the plan's first attempt submitted this task; the downloader finished it
            task = await DownloadTask.create(
                downloader=data.downloader,
                dir=str(downloads),
                name="Lantern.2024.mkv",
                files=["Lantern.2024.mkv"],
                state=DownloadState.COMPLETED,
                transfer_lib_id=data.movies.id,
                transfer_method=TransferMethod.COPY,
            )
            assert created["ingestion"]["binding"] == "added"

            from app.core.dl import syncer

            assert await syncer.transfer_files(task, task.files)
            video = tmp_path / "movies" / "Lantern.2024.mkv"
            assert video.read_bytes() == b"video"
            event = await MediaEvent.create(
                lib=data.movies, src_path=str(video), event_type="created"
            )
            await asyncio.wait_for(watcher.consume_event(event), timeout=5)

            nfo = video.with_suffix(".nfo")
            assert etree.parse(nfo).getroot().findtext("title") == "Lantern & Rain"
            assert etree.parse(nfo).getroot().findtext("year") == "2024"
            # the watcher reads the written NFO like any other library change
            written = await MediaEvent.create(
                lib=data.movies, src_path=str(nfo), event_type="created"
            )
            await asyncio.wait_for(watcher.consume_event(written), timeout=5)
            item = await MediaItem.get(path=str(video))
            assert remote and remote[0].startswith("Lantern")
            assert item.title == "Lantern & Rain"
            logs = await FlowLog.filter(graph_id=data.movie.id).values()
            assert logs and all(log["exc_info"] is None for log in logs)

    asyncio.run(run())
