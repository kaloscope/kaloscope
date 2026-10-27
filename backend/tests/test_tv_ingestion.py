"""Tests for TV scraper authoring, TV-library ingestion and download setup."""

import asyncio
import mimetypes
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from lxml import etree
from sanic import Sanic

from app.core.ai import history, tools
from app.core.constants import NFO_MIME_TYPE
from app.core.exceptions import ForbiddenException
from app.core.flow.context import MANUAL_KEY, RETVAL_KEY
from app.core.flow.nodes.base import CancellationSignal
from app.core.flow.nodes.control.condition import ConditionNode
from app.core.flow.nodes.general.variables import VariablesNode
from app.core.flow.nodes.metadata.episode import EpisodeNode
from app.core.flow.nodes.metadata.tvshow import TVShowNode
from app.core.flow.nodes.start.ingest import IngestStartNode
from app.core.media import shelver, watcher
from app.core.media.shelver import TEMPLATES_PATH, read_nfo, render_nfo
from app.models.assistant import (
    AssistantProposal,
    AssistantTurn,
    ConversationCreate,
    ProposalConfirm,
    ProposalDecision,
)
from app.models.download import (
    DownloadPlan,
    DownloadState,
    DownloadTask,
    DownloadTaskCheck,
    DownloadTaskCreate,
    TransferMethod,
)
from app.models.flow import FlowGraph, FlowLog, FlowTrigger, GraphDocument, GraphWrite
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import Permissions, UserRole
from app.models.workflow_authoring import TemplateValidation
from app.services import download as service
from app.services import workflow_authoring as authoring
from app.services.assistant import AssistantService
from app.services.assistant_proposals import AssistantProposalService as Proposals
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowGraphService
from app.services.mcp import MCPAccessTokenService
from app.services.media import MediaLibService, tv_route_issue
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_conversation import engine_fixture
from tests.test_download_plan_ingestion import (
    bindings,
    creation,
    libraries,
    reviewed,
)
from tests.test_download_tasks_once import (
    RESULTS,
    Rpc,
    searched,
    settings,
    setup,
    site,
)
from tests.test_download_tasks_once import sealing as sealing
from tests.test_flow_reference import context
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_services_media_index import _ingest_engine
from tests.test_workflow_outputs import HTML_FORMATTER, check

CONDITION = "{{ nfo_type == 'episode' }}"
SHOW_URL = "https://tv.example.test/search?q={{ title|quote('') }}"
EPISODE_URL = (
    "https://tv.example.test/episodes?{% if series_id %}id="
    "{{ series_id|quote('') }}{% else %}q={{ title|quote('') }}{% endif %}"
)
SHOW_TEMPLATE = """[
{% for card in html|xpath_all('//article[@class="show"]') %}
{% set year = card|xpath_first('.//time/@data-year') %}
{"title": {{ card|xpath_first('.//h2/text()')|tojson }},
"id": {{ card|xpath_first('./@data-id')|tojson }}, "site": "tv.example.test",
"genres": {{ card|xpath_all('.//span[@class="genre"]/text()')|tojson }}
{% if year %}, "year": {{ year|int }}{% endif %}}
{% if not loop.last %},{% endif %}
{% endfor %}]"""
EPISODE_TEMPLATE = """{% set path = '//li[@data-season="' ~ season ~ '"]'
~ '[@data-episode="' ~ episode ~ '"]' %}
[{% for card in html|xpath_all(path) %}
{% set aired = card|xpath_first('./time/@datetime') %}
{"title": {{ card|xpath_first('./h3/text()')|tojson }},
"id": {{ card|xpath_first('./@data-id')|tojson }}, "site": "tv.example.test",
"season": {{ card|xpath_first('./@data-season')|int }},
"episode": {{ card|xpath_first('./@data-episode')|int }}
{% if aired %}, "aired": {{ aired|tojson }}{% endif %}}
{% if not loop.last %},{% endif %}
{% endfor %}]"""
SHOW_HTML = (
    '<article class="show" data-id="wayfinder"><h2>The Wayfinder Journals</h2>'
    '<time data-year="2024"/><span class="genre">Adventure</span></article>'
)
EPISODE_HTML = (
    '<ol><li data-id="wayfinder-s00e01" data-season="0" data-episode="1">'
    "<h3>Charting the Journals</h3></li>"
    '<li data-id="wayfinder-s01e02" data-season="1" data-episode="2">'
    '<h3>The Island Without a Name</h3><time datetime="2024-04-12"/></li></ol>'
)
SHOW = {"nfo_type": "tvshow", "title": "The Wayfinder Journals", "year": 2024}
EPISODE = {**SHOW, "nfo_type": "episode", "season": 1, "episode": 2}
ROUTES = [
    ("start", "output", "route"),
    ("route", "is_true", "episode_http"),
    ("route", "is_false", "show_http"),
    ("show_http", "output", "show"),
    ("episode_http", "output", "episode"),
]


def tv_scraper(expression=CONDITION, edges=None):
    """Build a TV scraper definition that routes requests by `nfo_type`.

    Args:
        expression: Condition choosing the episode branch.
        edges: Source, handle and target links; defaults to complete routing.

    Returns:
        Saved graph definition using the TV recipe's tested templates.
    """
    nodes = [
        ("start", {"$schema": "ingest_start"}),
        ("route", {"$schema": "condition", "expression": expression}),
        (
            "show_http",
            {"$schema": "http", "url": SHOW_URL, "formatter": HTML_FORMATTER},
        ),
        (
            "episode_http",
            {"$schema": "http", "url": EPISODE_URL, "formatter": HTML_FORMATTER},
        ),
        ("show", {"$schema": "tv_show", "response": SHOW_TEMPLATE, "force_end": False}),
        (
            "episode",
            {"$schema": "episode", "response": EPISODE_TEMPLATE, "force_end": False},
        ),
    ]
    return {
        "nodes": [{"id": key, "type": "node", "data": data} for key, data in nodes],
        "edges": [
            {
                "id": f"{source}-{target}",
                "source": source,
                "sourceHandle": handle,
                "target": target,
                "targetHandle": "input",
            }
            for source, handle, target in (ROUTES if edges is None else edges)
        ],
    }


async def tv_libraries(tmp_path, *, complete=True):
    """Add TV workflows and existing TV-library bindings to the shared fixtures.

    Args:
        tmp_path: Isolated directory for library roots.
        complete: Whether a published, fully routed TV scraper already exists.

    Returns:
        Shared fixtures plus complete, incomplete and draft TV workflows. The TV
        library already runs the movie scraper (asynchronously) and the draft.
    """
    data = await libraries(tmp_path)
    data.series = (
        await FlowGraph.create(
            name="Wayfinder TV",
            category="ingest",
            state="published",
            definition=tv_scraper(),
        )
        if complete
        else None
    )
    data.tv_draft = await FlowGraph.create(
        name="TV draft", category="ingest", state="draft", draft=tv_scraper()
    )
    data.shows_existing = [
        await FlowTrigger.create(
            category="ingest",
            rel_id=data.shows.id,
            graph=data.movie,
            priority=1,
            asynchronous=True,
        ),
        await FlowTrigger.create(
            category="ingest", rel_id=data.shows.id, graph=data.tv_draft, priority=2
        ),
    ]
    data.body.plan.transfer_lib_id = data.shows.id
    return data


@pytest.mark.parametrize(
    ("expression", "edges", "issue"),
    [
        (CONDITION, None, None),
        ("{{ not (nfo_type != 'episode') }}", None, None),
        (
            "{{ nfo_type == 'tvshow' }}",
            [
                ("start", "output", "route"),
                ("route", "is_false", "episode_http"),
                ("route", "is_true", "show_http"),
                ("show_http", "output", "show"),
                ("episode_http", "output", "episode"),
            ],
            None,
        ),
        (
            CONDITION,
            [
                ("start", "output", "show_http"),
                ("show_http", "output", "show"),
                ("show_http", "output", "episode"),
            ],
            "nfo_type=tvshow requests may also reach episode",
        ),
        ("{{ html|length > 0 }}", None, "requests may also reach"),
        ("{{ episode is not none }}", None, "requests may also reach"),
        (CONDITION, ROUTES[:4], "to episode"),
        (CONDITION, [*ROUTES[:2], ROUTES[4]], "to tv_show"),
    ],
    ids=[
        "complete",
        "boolean",
        "reversed",
        "shared-page",
        "page-condition",
        "episode-number",
        "unlinked-episode",
        "episode-only",
    ],
)
def test_routes(expression, edges, issue):
    result = tv_route_issue(tv_scraper(expression, edges))

    if issue is None:
        assert result is None
    else:
        assert issue in result, result


@pytest.mark.parametrize(
    ("expression", "fields"),
    [
        ("{{ nfo_type == 'episode' and season }}", {"season": 0}),
        ("{{ nfo_type == 'episode' and title == 'Show' }}", {"title": "Another"}),
        ("{{ nfo_type == 'episode' and year == 2024 }}", {"year": 2025}),
        ("{{ nfo_type == 'episode' and episode != 0 }}", {"episode": 0}),
        ("{{ nfo_type == 'episode' and var('season') }}", {"season": 0}),
    ],
    ids=["special", "title", "year", "episode-zero", "context-function"],
)
def test_request_dependent_route(expression, fields):
    async def run():
        branch = await ConditionNode.execute(
            node_data={"expression": expression}, context=context({**EPISODE, **fields})
        )

        assert branch is ConditionNode.Handles.is_false
        assert tv_route_issue(tv_scraper(expression)) is not None

    asyncio.run(run())


@pytest.mark.parametrize("shared_route", [False, True])
def test_overwritten_route(shared_route):
    async def run():
        definition = tv_scraper(
            edges=[
                *([ROUTES[0]] if shared_route else []),
                ("start", "output", "variables"),
                ("variables", "output", "route"),
                *ROUTES[1:],
            ]
        )
        values = {"$schema": "variables", "variables": '{"nfo_type": "episode"}'}
        definition["nodes"].append({"id": "variables", "type": "node", "data": values})
        ctx = context(SHOW)
        await VariablesNode.execute(node_data=values, context=ctx)
        branch = await ConditionNode.execute(
            node_data={"expression": CONDITION}, context=ctx
        )

        assert branch is ConditionNode.Handles.is_true
        assert tv_route_issue(definition) is not None

    asyncio.run(run())


@pytest.mark.parametrize(
    ("value", "mode", "path"),
    [
        ([{"season": 1}], "episode", "0.title"),
        ([{"title": "A", "season": "1"}], "episode", "0.season"),
        ([{"title": "A", "episode": -1}], "episode", "0.episode"),
        ([{"title": "A", "episode": 2**31}], "episode", "0.episode"),
        ([{"title": "A", "aired": "2024-02-30"}], "episode", "0.aired"),
        ([{"title": "A", "rating": 10.5}], "episode", "0.rating"),
        ([{"title": "A\u0001"}], "episode", "[0]"),
        ({"title": "A"}, "episode", "$"),
        ([{"title": "A", "genres": "Drama"}], "tv_show", "0.genres"),
        ([{"title": "A", "year": 0}], "tv_show", "0.year"),
        ([{"title": "A", "season": True}], "tv_show", "0.season"),
        ([{"title": "A", "premiered": "2024-4-5"}], "tv_show", "0.premiered"),
    ],
)
def test_output_errors(value, mode, path):
    result = check(value, mode)

    assert not result.valid
    assert path in [issue.path for issue in result.issues], result.issues
    assert result.error and "validate again" in result.error


@pytest.mark.parametrize(
    ("value", "mode", "fields", "note"),
    [
        (
            [{"title": "Charting", "season": 0, "episode": 1}],
            "episode",
            {"season": 0, "episode": 1},
            "0 is written as given",
        ),
        (
            [{"title": "Next", "season": 1, "episode": 3}],
            "episode",
            {"season": 1, "episode": 2},
            "differ from the request",
        ),
        ([{"title": "Pilot"}], "episode", {"season": 1}, "parsed numbers"),
        ([{"title": "Show", "year": 2024}], "tv_show", {}, "series_id"),
        ([], "episode", {}, "Empty candidates"),
        (
            [{"title": "Pilot", "season": 1, "episode": 1, "director": "A"}],
            "episode",
            {},
            "Unknown episode fields",
        ),
    ],
)
def test_output_notes(value, mode, fields, note):
    result = check(value, mode, **fields)

    assert result.valid, result
    assert result.value == value
    assert any(note in warning for warning in result.warnings), result.warnings


@pytest.mark.parametrize(
    ("fields", "html", "expected"),
    [
        (
            SHOW,
            SHOW_HTML,
            [
                {
                    "title": "The Wayfinder Journals",
                    "id": "wayfinder",
                    "site": "tv.example.test",
                    "genres": ["Adventure"],
                    "year": 2024,
                }
            ],
        ),
        (
            {**SHOW, "year": None, "season": 2},
            SHOW_HTML.replace('<time data-year="2024"/>', ""),
            [
                {
                    "title": "The Wayfinder Journals",
                    "id": "wayfinder",
                    "site": "tv.example.test",
                    "genres": ["Adventure"],
                }
            ],
        ),
        (SHOW, "<html/>", []),
        (
            EPISODE,
            EPISODE_HTML,
            [
                {
                    "title": "The Island Without a Name",
                    "id": "wayfinder-s01e02",
                    "site": "tv.example.test",
                    "season": 1,
                    "episode": 2,
                    "aired": "2024-04-12",
                }
            ],
        ),
        (
            {**EPISODE, "season": 0, "episode": 1, "series_id": "wayfinder"},
            EPISODE_HTML,
            [
                {
                    "title": "Charting the Journals",
                    "id": "wayfinder-s00e01",
                    "site": "tv.example.test",
                    "season": 0,
                    "episode": 1,
                }
            ],
        ),
        ({**EPISODE, "episode": None}, EPISODE_HTML, []),
    ],
    ids=[
        "show",
        "show-missing-year",
        "show-no-match",
        "episode",
        "special",
        "unparsed",
    ],
)
def test_recipe(monkeypatch, tmp_path, fields, html, expected):
    write = AsyncMock(side_effect=AssertionError("Fixture checks must not write NFOs"))
    monkeypatch.setattr(shelver, "gen_nfo", write)
    episode = fields["nfo_type"] == "episode"
    mode, node, nfo_type = (
        ("episode", EpisodeNode, "episode")
        if episode
        else ("tv_show", TVShowNode, "tvshow")
    )
    template = EPISODE_TEMPLATE if episode else SHOW_TEMPLATE

    async def run():
        fixture = {"html": html, MANUAL_KEY: True, **fields}
        result = authoring.validate_workflow_template(
            TemplateValidation(template=template, context=fixture, mode=mode)
        )
        ctx = context(fixture)
        await node.execute(node_data={"response": template}, context=ctx)

        assert result.valid, result
        assert ctx[RETVAL_KEY] == result.value == expected
        for candidate in result.value[:1]:
            # the checker accepts exactly what the real reader reads back
            nfo = tmp_path / "item.nfo"
            nfo.write_text(
                render_nfo(
                    nfo_type,
                    (TEMPLATES_PATH / f"{nfo_type}.nfo").read_text(
                        encoding="utf-8-sig"
                    ),
                    candidate,
                )
            )
            meta, error, invalid = read_nfo(LibType.TV_SHOW, nfo)
            assert error is None and not invalid
            assert (meta.title, meta.id, meta.site) == (
                candidate["title"],
                candidate["id"],
                candidate["site"],
            )
            if episode:
                assert (meta.season, meta.episode, meta.aired) == (
                    candidate["season"],
                    candidate["episode"],
                    candidate.get("aired"),
                )
            else:
                assert (meta.year, meta.genres) == (
                    candidate.get("year"),
                    candidate["genres"],
                )
        write.assert_not_awaited()

    asyncio.run(run())


def test_inspect(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await tv_libraries(tmp_path)
            novels = await MediaLib.create(
                lib_type=LibType.NOVEL,
                dir=str(tmp_path / "novels"),
                name="Novels",
                priority=3,
            )
            before = await bindings(data.shows.id)

            result = await MediaLibService.ingestion_bindings(data.shows.id, owner)
            reading = await MediaLibService.ingestion_bindings(novels.id, owner)
            issues, _ = await MediaLibService.ingestion_check(
                novels.id, data.series.id, owner
            )

            assert result["supported"] and result["lib_type"] == LibType.TV_SHOW
            assert [
                (b["workflow_id"], b["order"], b["asynchronous"], b["compatible"])
                for b in result["bindings"]
            ] == [(data.movie.id, 1, True, False), (data.tv_draft.id, 2, False, False)]
            # the shared fixture's TV scraper reaches tv_show only
            assert result["workflows"] == [
                {"workflow_id": data.series.id, "workflow_name": "Wayfinder TV"}
            ]
            assert data.tv_draft.id in [
                w["workflow_id"] for w in result["unpublished_workflows"]
            ]
            assert "including future imports" in result["scope"]
            assert "series/season/episode" in result["scope"]
            assert "library root are not indexed" in result["scope"]
            assert "tv_scraper" in result["rules"]
            assert not reading["supported"]
            assert [issue["path"] for issue in issues] == ["transfer_lib_id"]
            assert await bindings(data.shows.id) == before

    asyncio.run(run())


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        ("tv", "to episode"),
        ("tv_draft", "Publish this workflow first"),
        ("movie", "to tv_show"),
        ("graph", "Choose an ingest workflow"),
        ("series", None),
    ],
    ids=["incomplete", "draft", "movie", "indexer", "complete"],
)
def test_checks(tmp_path, workflow, message):
    async def run():
        async with database() as (owner, _):
            data = await tv_libraries(tmp_path)
            before = await bindings(data.shows.id)

            result = await reviewed(owner, data, getattr(data, workflow).id)

            if message is None:
                assert result["valid"], result["issues"]
                review = result["ingestion"]
                assert review["binding"] == "add"
                assert review["lib_type"] == LibType.TV_SHOW
                assert review["library_name"] == "Shows"
                assert "library root are not indexed" in review["scope"]
            else:
                assert not result["valid"]
                found = [
                    i for i in result["issues"] if i["path"] == "ingest_workflow_id"
                ]
                assert found and message in found[0]["message"], result["issues"]
            assert await bindings(data.shows.id) == before
            assert not await DownloadPlan.exists()

    asyncio.run(run())


def test_plan_binding(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await tv_libraries(tmp_path)
            before = await bindings(data.shows.id)
            outdated = await reviewed(owner, data, data.series.id)
            # another administrator changes the library after that review
            await FlowTrigger.filter(id=data.shows_existing[0].id).update(
                asynchronous=False
            )
            changed = await bindings(data.shows.id)

            stale = await service.DownloadPlanService.create_once(
                creation(data, outdated), owner
            )
            validation = await reviewed(owner, data, data.series.id)
            preview = await service.DownloadPlanService.create_once(
                creation(data, validation, confirmed=False), owner
            )
            unchanged = await bindings(data.shows.id)
            created = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            replayed = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            again = await reviewed(owner, data, data.series.id)

            assert changed != before
            assert not stale["created"] and stale["ingestion"]["binding"] == "conflict"
            assert preview["confirmation_required"] and unchanged == changed
            assert created["created"] and created["ingestion"]["binding"] == "added"
            assert replayed["replayed"] and replayed["plan_id"] == created["plan_id"]
            # existing bindings keep their order and settings; the new one is last
            assert await bindings(data.shows.id) == [
                *changed,
                (data.series.id, 3, False),
            ]
            assert await DownloadPlan.all().count() == 1
            plan = await DownloadPlan.get(id=created["plan_id"])
            assert plan.transfer_lib_id == data.shows.id
            assert again["ingestion"]["binding"] == "reuse"

    asyncio.run(run())


def test_one_time(monkeypatch, tmp_path):
    # a refused connection is a definite failure: nothing reached the downloader
    driver = Rpc(error=httpx.ConnectError("downloader connection refused"))
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            data = await tv_libraries(tmp_path)
            items = await searched(owner, data.graph.id, monkeypatch, driver, client)
            # the single-file torrent needs a series/season folder in TV libraries
            fields = settings(
                data.graph.id,
                items["r2"],
                data.downloader.id,
                transfer_lib_id=data.shows.id,
                transfer_method=TransferMethod.HARDLINK,
                sub_pattern="^",
                sub_repl="Wayfinder (2024)/Season 01/",
            )
            before = await bindings(data.shows.id)
            check = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(
                    {**fields, "ingest_workflow_id": data.series.id}
                ),
                owner,
            )
            review = check["ingestion"]
            body = DownloadTaskCreate(
                request_id=check["request_id"],
                info_hash=check["download"]["info_hash"],
                confirmed=True,
                ingestion={
                    "workflow_id": review["workflow_id"],
                    "revision": review["revision"],
                },
                **fields,
            )

            failed = await service.DownloadTaskService.create_once(body, owner)
            retried = await service.DownloadTaskService.create_once(body, owner)
            replayed = await service.DownloadTaskService.create_once(body, owner)

            assert check["valid"] and review["binding"] == "add"
            assert check["download"]["transfer_method"] == TransferMethod.HARDLINK
            assert not failed["created"] and failed["ingestion"]["binding"] == "added"
            assert "same request_id" in failed["submit_error"]
            assert retried["created"] and retried["ingestion"]["binding"] == "reused"
            assert replayed["replayed"] and replayed["task_id"] == retried["task_id"]
            assert len(driver.added) == 2
            assert await bindings(data.shows.id) == [
                *before,
                (data.series.id, 3, False),
            ]
            task = await DownloadTask.get(id=retried["task_id"])
            assert task.transfer_lib_id == data.shows.id
            # the downloader has the task; transfer and ingestion run later
            assert await FlowLog.all().count() == 0

    asyncio.run(run())


def test_permissions(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await tv_libraries(tmp_path)
            validation = await reviewed(owner, data, data.series.id)
            owner.role = UserRole.USER
            owner.perms = Permissions(
                indexer_ids=[data.graph.id], media_lib_ids=[data.shows.id]
            )

            for request in (
                MediaLibService.ingestion_bindings(data.shows.id, owner),
                service.DownloadPlanService.create_once(
                    creation(data, validation), owner
                ),
            ):
                with pytest.raises(ForbiddenException):
                    await request

            assert validation["valid"]
            assert not await DownloadPlan.exists()
            assert not await FlowTrigger.filter(graph_id=data.series.id).exists()

    asyncio.run(run())


def test_mcp_parity(tmp_path):
    async def run():
        async with application() as app:
            data = await tv_libraries(tmp_path)
            admin = app.shared_ctx.sessions["admin"]
            arguments = {"library_id": data.shows.id}
            original = await bindings(data.shows.id)
            mcp = await call(app, "get_library_ingestion", arguments, token="admin")
            assistant = await tools.execute_tool(
                "get_library_ingestion",
                dumps(arguments).decode(),
                admin,
                conversation_id=1,
                call_id="ingestion",
            )
            fixture = {"template": EPISODE_TEMPLATE, "mode": "episode"}
            fixture["context"] = {"html": EPISODE_HTML, **EPISODE}
            checked = await call(
                app,
                "validate_workflow_template",
                {"validation": fixture},
                token="admin",
            )
            local = await tools.execute_tool(
                "validate_workflow_template",
                dumps(fixture).decode(),
                admin,
                conversation_id=1,
                call_id="check",
            )
            plan = {
                "plan": data.body.plan.model_dump(mode="json"),
                "ingest_workflow_id": data.series.id,
            }
            validated = await call(
                app, "validate_download_plan", {"validation": plan}, token="admin"
            )
            review = validated["structuredContent"]["ingestion"]
            body = {
                "request_id": str(data.body.request_id),
                "plan": plan["plan"],
                "ingestion": {
                    "workflow_id": review["workflow_id"],
                    "revision": review["revision"],
                },
                "confirmed": True,
            }
            read_only = await call(
                app, "create_download_plan", {"creation": body}, token="admin"
            )
            unchanged = await bindings(data.shows.id)
            planned = await DownloadPlan.exists()
            write = await MCPAccessTokenService.create(
                admin.id,
                MCPAccessTokenCreate(name="tv", scope=MCPTokenScope.READ_WRITE),
            )
            created = await call(
                app, "create_download_plan", {"creation": body}, token=write["token"]
            )

            assert not mcp.get("isError"), mcp
            assert mcp["structuredContent"] == loads(dumps(assistant["data"]))
            assert checked["structuredContent"] == loads(dumps(local["data"]))
            assert checked["structuredContent"]["valid"]
            assert review["binding"] == "add" and review["lib_type"] == "tv_show"
            assert read_only["isError"] and "read-only" in str(read_only)
            assert unchanged == original and not planned
            assert not created.get("isError"), created
            assert created["structuredContent"]["ingestion"]["binding"] == "added"
            assert await bindings(data.shows.id) == [
                *original,
                (data.series.id, 3, False),
            ]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("nfo_type", "manual", "exists"),
    [
        ("tvshow", False, True),
        (None, False, True),
        (None, True, True),
        (None, False, False),
    ],
    ids=["typed", "indexed", "manual", "missing"],
)
def test_existing_nfo(tmp_path, nfo_type, manual, exists):
    async def run():
        path = tmp_path / "show.nfo"
        if exists:
            path.write_text("<tvshow/>")
        ctx = context({"nfo_type": nfo_type, "nfo_path": str(path), MANUAL_KEY: manual})

        if exists and not manual:
            with pytest.raises(CancellationSignal):
                await IngestStartNode.execute(context=ctx)
        else:
            await IngestStartNode.execute(context=ctx)

        assert path.exists() == exists
        if exists:
            assert path.read_text() == "<tvshow/>"

    asyncio.run(run())


@pytest.mark.parametrize("layout", ["series", "season-zero", "root"])
def test_completion(tmp_path, monkeypatch, layout):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    downloads = tmp_path / "downloads"
    folder = "The Wayfinder Journals (2024)"
    relative = {
        "series": f"{folder}/The Wayfinder Journals S01E02.mkv",
        "season-zero": f"{folder}/Season 00/The Wayfinder Journals S00E01.mkv",
        "root": "The Wayfinder Journals S01E02.mkv",
    }[layout]
    source = downloads / relative
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    remote = []

    def site(request):
        remote.append((request.url.path, dict(request.url.params)))
        episodes = EPISODE_HTML.replace(
            "</ol>",
            '<li data-id="wayfinder-s01e03" data-season="1" data-episode="3">'
            "<h3>A Second Map</h3></li></ol>",
        )
        return httpx.Response(
            200, text=episodes if request.url.path == "/episodes" else SHOW_HTML
        )

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(site)) as client,
        ):
            app = SimpleNamespace(ctx=SimpleNamespace(httpx=client))
            monkeypatch.setattr(Sanic, "get_app", lambda *_, **__: app)
            # the real engine runs synchronous bindings; asynchronous ones queue
            _ingest_engine(monkeypatch)
            data = await tv_libraries(tmp_path)
            (tmp_path / "shows").mkdir()
            validation = await reviewed(owner, data, data.series.id)
            created = await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            assert created["ingestion"]["binding"] == "added"
            # the plan's first attempt submitted this task; the downloader finished it
            task = await DownloadTask.create(
                downloader=data.downloader,
                dir=str(downloads),
                name=folder,
                files=[relative],
                state=DownloadState.COMPLETED,
                transfer_lib_id=data.shows.id,
                transfer_method=TransferMethod.COPY,
            )

            from app.core.dl import syncer

            transferred = await syncer.transfer_files(task, task.files)
            video = tmp_path / "shows" / relative
            if layout == "root":
                assert not transferred
                assert source.read_bytes() == b"video"
                assert not video.exists()
                assert not task.transfer_targets
                assert (
                    "library root are not indexed" in validation["ingestion"]["scope"]
                )
                assert not await MediaItem.filter(lib=data.shows).exists()
                assert not await FlowLog.filter(graph_id=data.series.id).exists()
                assert not video.with_suffix(".nfo").exists()
                assert remote == []
                return

            assert transferred
            assert video.read_bytes() == b"video"
            event = await MediaEvent.create(
                lib=data.shows, src_path=str(video), event_type="created"
            )
            await asyncio.wait_for(watcher.consume_event(event), timeout=5)

            season, number, title = (
                (0, 1, "Charting the Journals")
                if layout == "season-zero"
                else (1, 2, "The Island Without a Name")
            )
            series_nfo = video.parent / f"{video.parent.name}.nfo"
            episode_nfo = video.with_suffix(".nfo")
            show = etree.parse(series_nfo).getroot()
            episode = etree.parse(episode_nfo).getroot()
            assert show.tag == "tvshow" and episode.tag == "episodedetails"
            assert show.findtext("title") == "The Wayfinder Journals"
            assert show.findtext("uniqueid") == "wayfinder"
            assert episode.findtext("title") == title
            assert (episode.findtext("season"), episode.findtext("episode")) == (
                str(season),
                str(number),
            )
            # each request reached only its own route
            assert [path for path, _ in remote] == [
                "/search",
                "/episodes",
            ], remote
            assert remote[1][1] == {"q": "The Wayfinder Journals"}
            for nfo in (series_nfo, episode_nfo):
                written = await MediaEvent.create(
                    lib=data.shows, src_path=str(nfo), event_type="created"
                )
                await asyncio.wait_for(watcher.consume_event(written), timeout=5)
            item = await MediaItem.get(path=str(video))
            parent = await MediaItem.get(id=item.parent_id)
            assert (item.title, item.season, item.episode) == (
                title,
                season,
                number,
            )
            assert (parent.title, parent.unique_id) == (
                "The Wayfinder Journals",
                "wayfinder",
            )
            logs = await FlowLog.filter(graph_id=data.series.id).values()
            assert len(logs) == 2 and all(log["exc_info"] is None for log in logs)

            if layout == "series":
                # future imports use the parent ID after its NFO has been indexed
                next_video = video.with_name("The Wayfinder Journals S01E03.mkv")
                next_video.write_bytes(b"next episode")
                event = await MediaEvent.create(
                    lib=data.shows, src_path=str(next_video), event_type="created"
                )
                await asyncio.wait_for(watcher.consume_event(event), timeout=5)

                assert remote[-1] == ("/episodes", {"id": "wayfinder"})
                assert len(remote) == 3
                episode = etree.parse(next_video.with_suffix(".nfo")).getroot()
                assert episode.findtext("title") == "A Second Map"
                assert episode.findtext("episode") == "3"

    asyncio.run(run())


def summary(body, goal):
    """Answer the history summarizer with the turns it was asked to cover.

    Args:
        body: Non-streaming summarization request.
        goal: Goal retained by the summary.

    Returns:
        Synthetic provider response.
    """
    turn_ids = loads(body["messages"][-1]["content"])["turn_ids"]
    content = dumps({"goals": [goal], "source_turn_ids": turn_ids}).decode()
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": content},
                }
            ]
        },
    )


def tv_changes():
    """Build the scripted preview: complete TV routing after the user's start node.

    Returns:
        Add-node and add-edge operations using canonical node types and handles.
    """
    positions = {
        "route": (800, 0),
        "show_http": (1600, 0),
        "show": (2400, 0),
        "episode_http": (1600, 600),
        "episode": (2400, 600),
    }
    definition = tv_scraper()
    return [
        {
            "op": "add_node",
            "node": {
                **node,
                "position": dict(zip(("x", "y"), positions[node["id"]], strict=True)),
            },
        }
        for node in definition["nodes"]
        if node["id"] != "start"
    ] + [{"op": "add_edge", "edge": edge} for edge in definition["edges"]]


@pytest.mark.parametrize("decision", ["keep", "revert"])
@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_authoring(monkeypatch, tmp_path, verbose, decision):
    streams, network, base = [], [], {}
    start = {
        "id": "start",
        "type": "node",
        "position": {"x": 0, "y": 0},
        "data": {"$schema": "ingest_start"},
    }

    def validation(template, mode, fields):
        return "validate_workflow_template", {
            "template": template,
            "mode": mode,
            "context": fields,
        }

    def handler(upstream):
        body = loads(upstream.content)
        assert history.context_size(body["messages"], body.get("tools", [])) <= 27904
        if not body.get("stream"):
            return summary(body, "Build the requested TV scraper.")
        streams.append(body)
        step = len(streams)
        if step == 1:
            calls = [
                (
                    "create_workflow_draft",
                    {
                        "name": "Wayfinder TV",
                        "category": "ingest",
                        "description": "Scrape series and episode metadata.",
                    },
                )
            ]
        elif step == 3:
            calls = [("get_workflow_reference", {"topic": "tv_scraper"})] + [
                ("list_node_types", {"node_type": name, "include_examples": False})
                for name in ("condition", "http", "tv_show", "episode")
            ]
        elif step == 4:
            names = {tool["function"]["name"] for tool in body["tools"]}
            assert "preview_workflow_draft" in names
            assert "list_media_items" not in names
            calls = [
                (
                    "probe_http_request",
                    {"request": {"url": url}, "formatter": HTML_FORMATTER},
                )
                for url in (
                    "https://tv.example.test/search?q=Wayfinder",
                    "https://tv.example.test/episodes?q=Wayfinder",
                )
            ]
        elif step == 5:
            wrong = EPISODE_TEMPLATE.replace(
                "('./@data-season')|int", "('./@data-season')|tojson"
            )
            calls = [validation(wrong, "episode", {"html": EPISODE_HTML, **EPISODE})]
        elif step == 6:
            assert (
                "Correct the reported output fields" in dumps(body["messages"]).decode()
            )
            special = {**EPISODE, "season": 0, "episode": 1, "series_id": "wayfinder"}
            calls = [
                validation(SHOW_TEMPLATE, "tv_show", {"html": SHOW_HTML, **SHOW}),
                validation(SHOW_TEMPLATE, "tv_show", {"html": "<html/>", **SHOW}),
                validation(
                    EPISODE_TEMPLATE, "episode", {"html": EPISODE_HTML, **EPISODE}
                ),
                validation(
                    EPISODE_TEMPLATE, "episode", {"html": EPISODE_HTML, **special}
                ),
                validation(
                    EPISODE_TEMPLATE,
                    "episode",
                    {"html": EPISODE_HTML, **EPISODE, "episode": None},
                ),
            ]
        elif step == 7:
            calls = [("preview_workflow_draft", {**base, "changes": tv_changes()})]
        else:
            return response(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "content": "Ready to inspect; not executed or saved.",
                        },
                        "stop",
                    )
                ]
            )
        return response(
            [
                chunk(
                    {
                        "role": "assistant",
                        "content": "Inspecting samples. " * 200
                        if verbose and step >= 3
                        else "",
                        "tool_calls": [
                            call_delta(
                                name, dumps(args).decode(), f"{step}-{index}", index
                            )
                            for index, (name, args) in enumerate(calls)
                        ],
                    },
                    "tool_calls",
                )
            ]
        )

    def endpoint(req):
        network.append(req.url.path)
        return httpx.Response(
            200, text=EPISODE_HTML if req.url.path == "/episodes" else SHOW_HTML
        )

    configure(monkeypatch, handler)
    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(endpoint)
    )

    async def run():
        async with database() as (owner, _):
            shows = await MediaLib.create(
                lib_type=LibType.TV_SHOW,
                dir=str(tmp_path / "shows"),
                name="Shows",
                priority=1,
            )
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            created = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request("create", prompt="Create a TV scraper for Shows."),
                    owner,
                )
            ]
            assert created[-1].event == "done", created
            draft = next(
                event.data["data"] for event in created if event.event == "tool_result"
            )
            graph = await FlowGraph.get(id=draft["workflow_id"])
            assert draft["editor_path"].endswith(f"/{graph.id}")
            # the designer opened the draft and the user placed a start node
            snapshot = await Sessions.snapshot(
                conversation["id"],
                graph.id,
                0,
                GraphDocument(nodes=[start], edges=[]),
                "canvas",
                owner,
            )
            base.update(workflow_id=graph.id, draft_version=0, snapshot_id=snapshot.id)
            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request(
                        "build",
                        prompt="Build it from the example.test samples; do not "
                        "publish or execute.",
                        context={
                            "page_type": "workflow_designer",
                            "route": draft["editor_path"],
                            "resource_id": graph.id,
                            "snapshot_id": snapshot.id,
                            "draft_version": 0,
                            "unsaved": True,
                            "editable": True,
                            "selected_node_ids": [],
                        },
                    ),
                    owner,
                )
            ]
            assert events[-1].event == "done", events[-1]
            assert len(streams) == 8
            results = [event.data for event in events if event.event == "tool_result"]
            assert all(result["ok"] for result in results), results
            checks = [
                result["data"]
                for result in results
                if result["name"] == "validate_workflow_template"
            ]
            assert not checks[0]["valid"]
            assert [issue["path"] for issue in checks[0]["issues"]] == ["0.season"]
            assert all(check["valid"] for check in checks[1:]), checks
            assert checks[2]["value"] == [] and checks[5]["value"] == []
            assert any("0 is written" in note for note in checks[4]["warnings"])
            preview = results[-1]["data"]
            assert preview["validation"]["valid"], preview
            proposed = await Sessions.read_snapshot(
                conversation["id"], preview["proposal_snapshot_id"], owner
            )
            assert len(proposed.document["nodes"]) == 6
            assert tv_route_issue(proposed.document) is None
            assert network == ["/search", "/episodes"]
            await graph.refresh_from_db()
            assert graph.definition is None and graph.draft_version == 0
            assert await FlowLog.all().count() == 0

            record = await AssistantProposal.get(id=preview["proposal_id"])
            identity = {
                "workflow_id": record.workflow_id,
                "base_version": record.base_version,
                "digest": record.digest,
            }
            if decision == "keep":
                body = ProposalConfirm(
                    **identity, base_snapshot_id=record.base_snapshot_id
                )
                saved = await Proposals.confirm(
                    conversation["id"], record.id, body, owner
                )
                # a lost response recovers the single recorded save
                repeated = await Proposals.confirm(
                    conversation["id"], record.id, body, owner
                )
                recovered = await Proposals.outcome(
                    conversation["id"], record.id, owner
                )
                await graph.refresh_from_db()
                assert saved["proposal"] == repeated["proposal"]
                assert recovered["proposal"]["status"] == "saved"
                assert graph.draft_version == 1 and graph.definition is None
                assert {n["id"] for n in graph.draft["nodes"]} >= {"start", "route"}
                issues, _ = await MediaLibService.ingestion_check(
                    shows.id, graph.id, owner
                )
                assert "Publish this workflow first" in issues[0]["message"]
                # publishing stays an explicit user action in the designer
                await FlowGraphService.publish_graph(
                    graph.id, GraphWrite(**graph.draft, draft_version=1), owner
                )
                issues, review = await MediaLibService.ingestion_check(
                    shows.id, graph.id, owner
                )
                assert not issues and review["binding"] == "add"
            else:
                cancelled = await Proposals.cancel(
                    conversation["id"], record.id, ProposalDecision(**identity), owner
                )
                await graph.refresh_from_db()
                original = await Sessions.read_snapshot(
                    conversation["id"], record.base_snapshot_id, owner
                )
                assert cancelled["proposal"]["status"] == "cancelled"
                assert graph.draft_version == 0 and graph.definition is None
                # Revert restores the captured canvas, including the unsaved edit
                assert [n["id"] for n in original.document["nodes"]] == ["start"]
            assert not await FlowTrigger.filter(rel_id=shows.id).exists()

    asyncio.run(run())


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_resumed_download(monkeypatch, tmp_path, verbose):
    fixture, identities = {}, []
    stage = {"number": 0, "step": 0}
    driver = Rpc()

    def handler(upstream):
        body = loads(upstream.content)
        assert history.context_size(body["messages"], body.get("tools", [])) <= 27904
        if not body.get("stream"):
            return summary(body, "Download one Wayfinder result into Shows")
        number, step = stage["number"], stage["step"]
        stage["step"] += 1
        flow, library = fixture["graph_id"], fixture["library_id"]
        unlock = [
            ("get_workflow", {"workflow_id": flow}),
            ("get_download_plan_options", {"workflow_id": flow}),
        ]
        create = (
            "create_download_task",
            {"request_id": identities[0] if identities else "", "confirmed": True},
        )
        actions = [
            [("list_workflows", {"category": "indexer"})],
            [
                *unlock,
                (
                    "search_indexer",
                    {"workflow_id": flow, "keyword": "Wayfinder", "sources": True},
                ),
                ("list_media_libraries", {}),
                ("get_library_ingestion", {"library_id": library}),
            ],
            [
                *unlock,
                (
                    "read_conversation_history",
                    {"tool_call_id": fixture.get("search_call_id", ""), "limit": 8000},
                ),
                ("get_library_ingestion", {"library_id": library}),
                ("validate_download_task", fixture.get("check", {})),
            ],
            [*unlock, create, create],
        ][number]
        if step >= len(actions):
            if number == 1:
                names = {item["function"]["name"] for item in body["tools"]}
                assert {"create_workflow_draft", "get_workflow_reference"} <= names
            content = [
                "Choose the published indexer for Wayfinder.",
                "Results: 1) 1080p  2) 720p. Shows has no TV scraper routing series "
                "and episodes; which site should it read?",
                "720p to Shows/Wayfinder (2024)/Season 01 by hardlink; Wayfinder "
                "TV is appended after the existing bindings for all future Shows "
                "imports. Confirm?",
                "Bound Wayfinder TV and submitted the task; it continues in the "
                "background.",
            ][number]
            return response([chunk({"role": "assistant", "content": content}, "stop")])
        name, args = actions[step]
        if name == "validate_download_task":
            # the chosen source is recovered from the original search result
            assert (
                fixture["check"]["resource"]["source"]
                in dumps(body["messages"]).decode()
            )
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
    execute = engine_fixture(monkeypatch, RESULTS)
    monkeypatch.setattr(service, "load_driver", lambda _: driver)
    service.Sanic.get_app().ctx.lib_watcher = SimpleNamespace(
        is_scanning=lambda _: False
    )

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(site)) as client,
        ):
            service.Sanic.get_app().ctx.httpx = client
            data = await tv_libraries(tmp_path, complete=False)
            fixture.update(graph_id=data.graph.id, library_id=data.shows.id)
            before = await bindings(data.shows.id)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            results = []
            for number, prompt in enumerate(
                [
                    "下载一次 Wayfinder，入库到剧集库",
                    "选择 Published indexer",
                    "已保存并发布剧集刮削工作流；选第二个，硬链接到剧集库",
                    "确认",
                ]
            ):
                if number == 2:
                    # the user built, saved and published the scraper in the designer
                    data.series = await FlowGraph.create(
                        name="Wayfinder TV",
                        category="ingest",
                        state="published",
                        definition=tv_scraper(),
                    )
                    fixture["check"]["ingest_workflow_id"] = data.series.id
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
                if number == 1:
                    search = next(r for r in turn if r["name"] == "search_indexer")
                    fixture["search_call_id"] = search["id"]
                    found = {i["id"]: i for i in search["data"]["items"]}
                    options = turn[1]["data"]["defaults"]
                    fixture["check"] = settings(
                        data.graph.id,
                        found["r2"],
                        options["downloader_id"],
                        dir=options["dir"],
                        transfer_lib_id=data.shows.id,
                        transfer_method="hardlink",
                        sub_pattern="^",
                        sub_repl="Wayfinder (2024)/Season 01/",
                    )
                if number == 2:
                    identities.append(turn[-1]["data"]["request_id"])
                if number < 3:
                    assert await bindings(data.shows.id) == before
                    assert not driver.added and not await DownloadTask.exists()

            by_name = {}
            for result in results:
                by_name.setdefault(result["name"], []).append(result["data"])
            first, second = by_name["get_library_ingestion"]
            assert first["supported"] and first["workflows"] == []
            assert [w["workflow_id"] for w in second["workflows"]] == [data.series.id]
            review = by_name["validate_download_task"][0]["ingestion"]
            assert review["binding"] == "add" and review["lib_type"] == "tv_show"
            assert "future imports" in review["scope"]
            created, replayed = by_name["create_download_task"]
            assert created["created"] and created["ingestion"]["binding"] == "added"
            assert replayed["replayed"] and replayed["task_id"] == created["task_id"]
            # the confirmed preview names the torrent's file and its TV location
            transfer = by_name["validate_download_task"][0]["download"]["transfer"]
            assert transfer["basis"] == "known_files"
            assert transfer["items"] == [
                {
                    "from": "movie.mkv",
                    "to": "Wayfinder (2024)/Season 01/movie.mkv",
                    "season": 1,
                    "episode": None,
                }
            ]
            assert created["download"]["transfer"] == transfer
            task = await DownloadTask.get(id=created["task_id"])
            assert task.sub_repl == "Wayfinder (2024)/Season 01/"
            assert await bindings(data.shows.id) == [
                *before,
                (data.series.id, 3, False),
            ]
            # submission returns at once; search ran once and nothing was ingested
            assert execute.await_count == 1 and len(driver.added) == 1
            assert not await DownloadPlan.exists()
            assert await FlowLog.all().count() == 0
            assert "PASSKEY" not in dumps(results).decode()

            checked = await AssistantTurn.get(request_id="step-2")
            assert f"media_lib:{data.shows.id}" in checked.resource_ids
            assert data.series.id in checked.resource_ids
            owner.role = UserRole.USER
            owner.perms = Permissions(indexer_ids=[data.graph.id], media_lib_ids=[])
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": checked.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="read",
            )
            assert denied["error"]["code"] == "permission_denied"

    asyncio.run(run())
