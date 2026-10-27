"""Tests for HTML download-indexer authoring, validation and plan readiness."""

import asyncio
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from sanic import Sanic

from app.core.ai import history, tools
from app.core.dl import syncer
from app.core.flow.engine import FlowEngine
from app.core.flow.outputs import download_link_kind
from app.core.flow.reference import get_workflow_reference
from app.models.assistant import ConversationCreate
from app.models.download import DownloadPlan, DownloadPlanCheck, DownloadTask
from app.models.flow import FlowGraph, FlowLog, GraphDocument
from app.models.workflow_authoring import TemplateValidation
from app.services import download as service
from app.services import workflow_authoring as authoring
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowGraphService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_indexer_auth_authoring import definition, execute
from tests.test_indexer_auth_authoring import runtime as runtime
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_services_download import DraftDriver

HASHES = {f"t{n}": f"{n:040x}" for n in range(101, 108)}
TORRENT = (
    b"d4:infod6:lengthi5e4:name9:movie.mkv12:piece lengthi16384e6:pieces20:"
    + b"\x01" * 20
    + b"ee"
)
SEARCH_FORMATTER = (
    '{"status": {{ response.status_code }}, '
    '"login": {{ (response.text|xpath_first(\'//form[@id="login"]\') is not none)'
    "|tojson }}, "
    '"cards": [{% for c in response.text|xpath_all(\'//li[@class="item"]\') %}'
    "{% if not loop.first %},{% endif %}"
    "{\"id\": {{ c|xpath_first('./@data-id')|tojson }}, "
    "\"title\": {{ c|xpath_first('.//a/text()')|tojson }}, "
    '"size": {{ c|xpath_first(\'.//span[@class="size"]/text()\')|tojson }}, '
    "\"detail\": {{ response.url.join(c|xpath_first('.//a/@href'))|string|tojson }}}"
    "{% endfor %}]}"
)
DETAIL_FORMATTER = (
    "{% set magnet = response.text|xpath_first('//a[@class=\"magnet\"]/@href') %}"
    "{% set file = response.text|xpath_first('//a[@class=\"torrent\"]/@href') %}"
    '{"details": { {{ card.id|tojson }}: {"link": '
    "{{ (magnet or (response.url.join(file)|string if file else none))|tojson }}"
    "}}}"
)
SEARCH_END = (
    "{% if status != 200 %}"
    '{"error": {{ ("status " ~ status)|tojson }}}'
    '{% elif login %}{"error": "login_required"}'
    '{% else %}{"items": [{% for card in cards[:page_size] %}'
    "{% if not loop.first %},{% endif %}"
    '{"id": {{ card.id|tojson }}, "title": {{ card.title|tojson }}, '
    '"size": {{ card.size|tojson }}, '
    '"link": {{ (details|default({})).get(card.id, {}).link|tojson }}}'
    "{% endfor %}]}{% endif %}"
)
CARDS = {
    1: [("t101", "Lantern 2024 1080p", "2.1 GB"), ("t102", "Lantern 2024 720p", None)],
    2: [("t103", "Lantern Saga", "4.0 GB"), ("t104", "Lantern Extras", "1.0 GB")],
}


class Downloader(DraftDriver):
    """Give each submission its own offline job, as a real downloader does."""

    def prepare(self, request):
        """Prepare one submission with a fresh remote job.

        Args:
            request: Download request built by the plan.

        Returns:
            Draft submission from the shared test driver.
        """
        self.job_uuid = uuid4().hex
        return super().prepare(request)


def search_html(page, *, extra=False):
    """Render a synthetic server-side search page.

    Args:
        page: One-based page number.
        extra: Whether to add a third card beyond the requested page size.

    Returns:
        HTML with result cards, or a no-results page.
    """
    cards = CARDS.get(page, [])
    if extra and cards:
        cards = [*cards, ("t105", "Lantern Bonus", "0.5 GB")]
    items = "".join(
        f'<li class="item" data-id="{key}"><a href="/torrent/{key}">{title}</a>'
        + (f'<span class="size">{size}</span>' if size else "")
        + "</li>"
        for key, title, size in cards
    )
    return f"<html><ul>{items}</ul></html>" if items else "<p>No results</p>"


def detail_html(key):
    """Render a synthetic detail page with a magnet or a relative torrent link.

    Args:
        key: Remote result ID.

    Returns:
        HTML containing exactly one download link, or none for `t104`.
    """
    if key == "t104":
        return "<html><p>Removed</p></html>"
    if key in {"t102", "t103"}:
        return f'<a class="torrent" href="../files/{key}.torrent">Download</a>'
    return f'<a class="magnet" href="magnet:?xt=urn:btih:{HASHES[key]}">Magnet</a>'


class Site:
    """Serve a synthetic tracker that only answers its own pages."""

    def __init__(self, *, login=False, extra=False):
        """Configure the login wall and an extra card beyond page_size.

        Args:
            login: Whether search pages show a login form instead of results.
            extra: Whether page 1 lists one card more than requested.
        """
        self.requests = []
        self.login = login
        self.extra = extra

    def __call__(self, request):
        """Answer one search, detail or torrent request.

        Args:
            request: Request sent by a workflow node, probe or plan.

        Returns:
            Synthetic HTML or torrent response.
        """
        self.requests.append(request)
        path = request.url.path
        if path == "/search":
            if self.login:
                return httpx.Response(200, text='<form id="login"></form>')
            page = int(parse_qs(urlsplit(str(request.url)).query)["p"][0])
            return httpx.Response(200, text=search_html(page, extra=self.extra))
        if path.startswith("/torrent/"):
            return httpx.Response(200, text=detail_html(path.rsplit("/", 1)[-1]))
        if path.startswith("/files/"):
            return httpx.Response(
                200,
                content=TORRENT,
                headers={"content-type": "application/x-bittorrent"},
            )
        return httpx.Response(404, text="missing")


def indexer_changes():
    """Build canvas edits for `search_start -> http -> loop(http) -> search_end`.

    Returns:
        Add-node and add-edge operations using the tested templates.
    """
    nodes = [
        ("start", {"$schema": "search_start"}),
        (
            "search",
            {
                "$schema": "http",
                "url": "https://tracker.test/search?q={{ keyword|quote('') }}"
                "&p={{ page_num }}",
                "formatter": SEARCH_FORMATTER,
            },
        ),
        (
            "each",
            {
                "$schema": "loop",
                "expression": "{{ cards[:page_size] }}",
                "varname": "card",
            },
        ),
        (
            "detail",
            {
                "$schema": "http",
                "url": "{{ card.detail }}",
                "formatter": DETAIL_FORMATTER,
            },
        ),
        ("end", {"$schema": "search_end", "response": SEARCH_END}),
    ]
    edges = [
        ("start", "output", "search", "input"),
        ("search", "output", "each", "input"),
        ("each", "loop_start", "detail", "input"),
        ("detail", "output", "each", "loop_continue"),
        ("each", "output", "end", "input"),
    ]
    return [
        {
            "op": "add_node",
            "node": {
                "id": key,
                "type": "node",
                "position": {"x": index * 800, "y": 0},
                "data": data,
            },
        }
        for index, (key, data) in enumerate(nodes)
    ] + [
        {
            "op": "add_edge",
            "edge": {
                "id": f"{source}-{handle}-{target}",
                "source": source,
                "sourceHandle": handle,
                "target": target,
                "targetHandle": target_handle,
            },
        }
        for source, handle, target, target_handle in edges
    ]


def check(template, context, mode="search"):
    """Validate a template through the shared authoring service.

    Args:
        template: Exact node template.
        context: Explicit fixtures.
        mode: Output contract.

    Returns:
        Shared template result.
    """
    return authoring.validate_workflow_template(
        TemplateValidation(template=template, mode=mode, context=context)
    )


def page_fixture(page, *, links=True, **extra):
    """Describe the merged context that `search_end` reads for one page.

    Args:
        page: One-based page number.
        links: Whether detail pages supplied download links.
        **extra: Additional fixtures such as `previous_ids`.

    Returns:
        Context with cards, detail links and pagination inputs.
    """
    cards = [
        {
            "id": key,
            "title": title,
            "size": size,
            "detail": f"https://tracker.test/torrent/{key}",
        }
        for key, title, size in CARDS.get(page, [])
    ]
    details = {
        card["id"]: {"link": f"magnet:?xt=urn:btih:{HASHES.get(card['id'], '0' * 40)}"}
        for card in cards
        if links
    }
    return {
        "status": 200,
        "login": False,
        "cards": cards,
        "details": details,
        "page_num": page,
        "page_size": 2,
        "downloads": True,
        **extra,
    }


@pytest.mark.parametrize(
    ("link", "kind"),
    [
        ("a" * 40, "hash"),
        (f"magnet:?xt=urn:btih:{'b' * 40}&dn=x", "hash"),
        ("magnet:?dn=missing-hash", None),
        ("https://tracker.test/files/t1.torrent", "http"),
        ("/files/t1.torrent", None),
        ("javascript:void(0)", None),
        ("", None),
        (None, None),
    ],
)
def test_link_kinds(link, kind):
    assert download_link_kind(link) == kind


@pytest.mark.parametrize(
    ("context", "path", "warning"),
    [
        (page_fixture(1), None, None),
        (page_fixture(2, previous_ids=["t101", "t102"]), None, None),
        (page_fixture(3), None, None),
        (page_fixture(1) | {"login": True}, "items", None),
        (page_fixture(1) | {"status": 503}, "items", None),
        (page_fixture(1, links=False), "items.0.link", None),
        (
            page_fixture(2, previous_ids=["t103", "t104"]),
            "items",
            None,
        ),
        (
            page_fixture(1)
            | {"details": {"t101": {"link": "/files/t101.torrent"}, "t102": {}}},
            "items.0.link",
            None,
        ),
        (
            page_fixture(1)
            | {"details": {"t101": {"link": "https://tracker.test/torrent/t101"}}},
            "items.1.link",
            "not a torrent",
        ),
    ],
    ids=[
        "first",
        "next",
        "empty",
        "login",
        "error-page",
        "missing-links",
        "unchanged-page",
        "relative-link",
        "detail-page-link",
    ],
)
def test_download_checks(context, path, warning):
    result = check(SEARCH_END, context)

    assert result.valid is (path is None), result
    if path:
        assert path in [issue.path for issue in result.issues]
    if warning:
        assert any(warning in item for item in result.warnings), result.warnings
    if context.get("page_num") == 3 and path is None:
        assert result.value == {"items": []}
    if path is None and context.get("cards"):
        # missing optional metadata stays null instead of being invented
        assert result.value["items"][1]["size"] in (None, "4.0 GB", "1.0 GB")


def test_contract_scope():
    plain = page_fixture(1, links=False)
    plain.pop("downloads")
    counted = check(
        '{"total": 2, "items": [{"id": "0", "title": "A"}, {"id": "1", "title": "B"}]}',
        {"page_num": 1, "page_size": 2},
    )

    # non-download indexers keep the original search contract
    assert check(SEARCH_END, plain).valid
    assert any("this full page's count" in item for item in counted.warnings)
    assert any("positions" in item for item in counted.warnings)
    assert counted.valid


@pytest.mark.parametrize(
    ("template", "page", "expected"),
    [
        (
            SEARCH_FORMATTER,
            "/search?q=Lantern&p=1",
            "https://tracker.test/torrent/t101",
        ),
        (DETAIL_FORMATTER, "/torrent/t102", "https://tracker.test/files/t102.torrent"),
    ],
    ids=["search", "detail"],
)
def test_page_fixtures(template, page, expected):
    url = f"https://tracker.test{page}"
    site = Site()
    html = site(httpx.Request("GET", url)).text
    context = {"response": {"text": html, "url": url, "status_code": 200}}
    if template == DETAIL_FORMATTER:
        context["card"] = {"id": "t102"}

    result = check(template, context, mode="object")

    assert result.valid, result
    # relative links resolve against the page that was fetched
    links = (
        [card["detail"] for card in result.value["cards"]]
        if template == SEARCH_FORMATTER
        else [result.value["details"]["t102"]["link"]]
    )
    assert expected in links
    assert all(link.startswith("https://tracker.test/") for link in links)


def test_engine_feeds_plans(monkeypatch, runtime):
    site = Site(extra=True)

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(site)) as client,
        ):
            runtime.ctx.httpx = client
            runtime.ctx.flow_engine = FlowEngine.__new__(FlowEngine)
            monkeypatch.setattr(Sanic, "get_app", lambda *_, **__: runtime)
            graph, downloader, body = await fixture_plan()
            await FlowGraph.filter(id=graph.id).update(
                definition=definition(indexer_changes()), state="published"
            )
            assert (
                await FlowGraphService.validate_graph(
                    graph.id, GraphDocument(**definition(indexer_changes())), owner
                )
            ).valid

            pages = [
                await execute(
                    graph.id,
                    **{"$start": "search_start"},
                    keyword="Lantern",
                    page_num=number,
                    page_size=2,
                )
                for number in (1, 2, 3)
            ]
            detail_requests = [
                r for r in site.requests if r.url.path.startswith("/torrent/")
            ]
            preview = await service.DownloadPlanService.search_indexer(
                service.IndexerSearch(
                    workflow_id=graph.id, keyword="Lantern", page_size=2
                ),
                owner,
            )
            # the generated search entry declares no filters
            settings = body.plan.model_copy(
                update={"filters": None, "batch_limit": 2, "total_limit": 2}
            )
            sample = await service.DownloadPlanService.validate_creation(
                DownloadPlanCheck(plan=settings, sample=pages[0]), owner
            )
            created = await service.DownloadPlanService.create_once(
                body.model_copy(update={"plan": settings}), owner
            )
            assert created.get("created"), created
            plan = await DownloadPlan.get(id=created["plan_id"])
            attempt = await syncer.execute_download_plan(plan, Downloader())

            assert [item["id"] for item in pages[0]["items"]] == ["t101", "t102"]
            assert (
                pages[0]["items"][0]["link"] == f"magnet:?xt=urn:btih:{HASHES['t101']}"
            )
            assert (
                pages[0]["items"][1]["link"]
                == "https://tracker.test/files/t102.torrent"
            )
            assert pages[0]["items"][1]["size"] is None
            assert "total" not in pages[0]
            assert [item["id"] for item in pages[1]["items"]] == ["t103", "t104"]
            assert pages[1]["items"][1]["link"] is None
            assert pages[2] == {"items": []}
            # the third card exceeds page_size, so its detail page is never requested
            assert "/torrent/t105" not in {r.url.path for r in detail_requests}
            for page, previous in ((pages[0], None), (pages[1], ["t101", "t102"])):
                checked = check(
                    "{{ page|tojson }}",
                    {
                        "page": page,
                        "page_num": 1 if previous is None else 2,
                        "page_size": 2,
                        "downloads": True,
                        "previous_ids": previous or [],
                    },
                )
                assert checked.valid is (previous is None), checked.issues
            assert preview["status"] == "ok" and preview["returned_count"] == 2
            assert sample["valid"], sample["issues"]
            assert attempt["added"] == 2, attempt
            tasks = await DownloadTask.all()
            assert {task.info_hash for task in tasks} >= {HASHES["t101"]}
            assert len(tasks) == 2
            assert all(
                log["exc_info"] is None
                for log in await FlowLog.filter(graph_id=graph.id).values()
            )

    asyncio.run(run())


def test_login_wall(monkeypatch, runtime):
    site = Site(login=True)

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(site)) as client,
        ):
            runtime.ctx.httpx = client
            runtime.ctx.flow_engine = FlowEngine.__new__(FlowEngine)
            monkeypatch.setattr(Sanic, "get_app", lambda *_, **__: runtime)
            graph, _, _ = await fixture_plan()
            await FlowGraph.filter(id=graph.id).update(
                definition=definition(indexer_changes()), state="published"
            )

            output = await execute(
                graph.id,
                **{"$start": "search_start"},
                keyword="Lantern",
                page_num=1,
                page_size=2,
            )
            preview = await service.DownloadPlanService.search_indexer(
                service.IndexerSearch(workflow_id=graph.id, keyword="Lantern"), owner
            )

            # a login page is an invalid result, never an empty search
            assert output == {"error": "login_required"}
            assert preview["status"] == "invalid_output"
            assert not [r for r in site.requests if r.url.path.startswith("/torrent/")]

    asyncio.run(run())


def test_mcp_parity():
    async def run():
        async with application() as app:
            reference = await call(
                app, "get_workflow_reference", {"topic": "html_indexer"}
            )
            fixture = {
                "template": SEARCH_END,
                "mode": "search",
                "context": page_fixture(1, links=False),
            }
            mcp = await call(app, "validate_workflow_template", {"validation": fixture})
            assistant = await tools.execute_tool(
                "validate_workflow_template",
                dumps(fixture).decode(),
                app.shared_ctx.sessions["reader"],
                conversation_id=1,
                call_id="html",
            )

            assert (
                reference["structuredContent"]
                == get_workflow_reference("html_indexer").model_dump()
            )
            assert mcp["structuredContent"] == assistant["data"]
            assert not mcp["structuredContent"]["valid"]

    asyncio.run(run())


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_assistant_build(monkeypatch, runtime, verbose):
    streams, base = [], {}
    changes = indexer_changes()
    site = Site()
    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(site)
    )

    def validation(template, context, mode="search"):
        """Describe one template check.

        Args:
            template: Exact node template.
            context: Isolated fixtures.
            mode: Output contract.

        Returns:
            Named assistant tool call.
        """
        return "validate_workflow_template", {
            "template": template,
            "mode": mode,
            "context": context,
        }

    def handler(upstream):
        body = loads(upstream.content)
        assert history.context_size(body["messages"], body.get("tools", [])) <= 27904
        if not body.get("stream"):
            summary = {
                "goals": ["Build the HTML download indexer."],
                "source_turn_ids": loads(body["messages"][-1]["content"])["turn_ids"],
            }
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": dumps(summary).decode(),
                            },
                        }
                    ]
                },
            )
        streams.append(body)
        step = len(streams)
        text = dumps(body["messages"]).decode()
        if step == 1:
            calls = [
                (
                    "create_workflow_draft",
                    {
                        "name": "Tracker",
                        "category": "indexer",
                        "description": "HTML downloads",
                    },
                )
            ]
        elif step == 3:
            calls = [("get_workflow_reference", {"topic": "html_indexer"})] + [
                ("list_node_types", {"node_type": name, "include_examples": False})
                for name in ("http", "loop")
            ]
        elif step == 4:
            names = {tool["function"]["name"] for tool in body["tools"]}
            assert "preview_workflow_draft" in names and "list_media_items" not in names
            calls = [
                (
                    "probe_http_request",
                    {
                        "request": {"url": "https://tracker.test/search?q=Lantern&p=1"},
                        "formatter": SEARCH_FORMATTER,
                    },
                ),
                (
                    "probe_http_request",
                    {
                        "request": {"url": "https://tracker.test/torrent/t102"},
                        "context": {"card": {"id": "t102"}},
                        "formatter": DETAIL_FORMATTER,
                    },
                ),
            ]
        elif step == 5:
            calls = [
                validation(
                    SEARCH_END.replace(".link|tojson", ".href|tojson"), page_fixture(1)
                )
            ]
        elif step == 6:
            assert "Correct the reported output fields" in text
            calls = [
                validation(SEARCH_END, page_fixture(1)),
                validation(SEARCH_END, page_fixture(2, previous_ids=["t101", "t102"])),
                validation(SEARCH_END, page_fixture(3)),
                validation(SEARCH_END, page_fixture(1) | {"login": True}),
                validation(SEARCH_END, page_fixture(1, links=False)),
            ]
        elif step == 7:
            assert "HTML download indexer" in text
            calls = [
                (
                    "preview_workflow_draft",
                    {
                        "workflow_id": base["workflow_id"],
                        "draft_version": 0,
                        "snapshot_id": base["snapshot_id"],
                        "changes": changes,
                    },
                )
            ]
        else:
            return response(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "content": "Preview ready; publish to use it in plans.",
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

    configure(monkeypatch, handler)

    async def run():
        async with database() as (owner, _):
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            created = [
                e
                async for e in AssistantService.persistent_stream(
                    conversation["id"],
                    request(
                        "create",
                        prompt="Create an HTML download indexer for tracker.test.",
                    ),
                    owner,
                )
            ]
            assert created[-1].event == "done", created
            graph = await FlowGraph.get(name="Tracker")
            snapshot = await Sessions.snapshot(
                conversation["id"],
                graph.id,
                0,
                GraphDocument(nodes=[], edges=[]),
                "canvas",
                owner,
            )
            base.update(workflow_id=graph.id, snapshot_id=snapshot.id)
            events = [
                e
                async for e in AssistantService.persistent_stream(
                    conversation["id"],
                    request(
                        "build",
                        prompt="Build search and detail extraction from "
                        "tracker.test; keyword Lantern, plan one movie.",
                        context={
                            "page_type": "workflow_designer",
                            "route": f"/settings/workflows/graphs/{graph.id}",
                            "resource_id": graph.id,
                            "snapshot_id": snapshot.id,
                            "draft_version": 0,
                            "unsaved": False,
                            "editable": True,
                            "selected_node_ids": [],
                        },
                    ),
                    owner,
                )
            ]
            assert events[-1].event == "done", events[-1]
            assert len(streams) == 8
            results = [e.data for e in events if e.event == "tool_result"]
            assert all(result["ok"] for result in results), results
            probes = [r["data"] for r in results if r["name"] == "probe_http_request"]
            assert (
                probes[0]["formatted"]["value"]["cards"][0]["detail"]
                == "https://tracker.test/torrent/t101"
            )
            assert (
                probes[1]["formatted"]["value"]["details"]["t102"]["link"]
                == "https://tracker.test/files/t102.torrent"
            )
            checks = [
                r["data"] for r in results if r["name"] == "validate_workflow_template"
            ]
            assert [c["valid"] for c in checks] == [
                False,
                True,
                True,
                True,
                False,
                False,
            ]
            assert checks[0]["issues"][0]["path"] == "items.0.link"
            preview = results[-1]["data"]
            assert preview["validation"]["valid"], preview
            proposed = await Sessions.read_snapshot(
                conversation["id"], preview["proposal_snapshot_id"], owner
            )
            nodes = {node["id"]: node for node in proposed.document["nodes"]}
            for item in changes:
                if item["op"] == "add_node":
                    assert nodes[item["node"]["id"]]["data"] == item["node"]["data"]
            assert len(site.requests) == 2
            await graph.refresh_from_db()
            assert graph.definition is None and graph.draft_version == 0
            assert await DownloadPlan.all().count() == await FlowLog.all().count() == 0

    asyncio.run(run())
