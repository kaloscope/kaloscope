"""Tests for complete fixture-based authoring through the persistent A1 loop."""

import asyncio

import httpx
import pytest

from app.core.ai import history
from app.models.assistant import ConversationCreate
from app.models.flow import FlowGraph, FlowLog, GraphDocument
from app.services import workflow_authoring as authoring
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database, request
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_workflow_outputs import (
    DETAILS_TEMPLATE,
    HTML_FORMATTER,
    JSON_FORMATTER,
    MOVIE_HTML,
    MOVIE_TEMPLATE,
    SEARCH_TEMPLATE,
    search_page,
)


def recipe_nodes(movie):
    """Build the exact graph that the scripted authoring client proposes.

    Args:
        movie: Select an HTML movie scraper or both JSON indexer branches.

    Returns:
        Add-node and add-edge operations using canonical node types and handles.
    """
    paths = (
        [
            (
                "ingest_start",
                "movie",
                "https://example.test/movies?q={{ title|quote('') }}",
                HTML_FORMATTER,
                MOVIE_TEMPLATE,
            )
        ]
        if movie
        else [
            (
                "search_start",
                "search_end",
                "https://example.test/search?page={{ page_num }}"
                "&size={{ page_size }}&q={{ keyword|quote('') }}",
                JSON_FORMATTER,
                SEARCH_TEMPLATE,
            ),
            (
                "details_start",
                "details_end",
                "https://example.test/details/{{ id|quote('') }}",
                JSON_FORMATTER,
                DETAILS_TEMPLATE,
            ),
        ]
    )
    changes = []
    for row, (start, end, url, formatter, template) in enumerate(paths):
        for column, (name, fields) in enumerate(
            (
                (start, {}),
                ("http", {"url": url, "formatter": formatter}),
                (end, {"response": template}),
            )
        ):
            changes.append(
                {
                    "op": "add_node",
                    "node": {
                        "id": f"{row}-{column}",
                        "type": "node",
                        "position": {"x": column * 800, "y": row * 600},
                        "data": {"$schema": name, **fields},
                    },
                }
            )
        for column in (0, 1):
            changes.append(
                {
                    "op": "add_edge",
                    "edge": {
                        "id": f"e{row}-{column}",
                        "source": f"{row}-{column}",
                        "target": f"{row}-{column + 1}",
                        "sourceHandle": "output",
                        "targetHandle": "input",
                    },
                }
            )
    return changes


@pytest.mark.parametrize("movie", [False, True], ids=["json-indexer", "html-movie"])
@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
@pytest.mark.parametrize("read_back", [False, True], ids=["reference", "read-back"])
def test_generate_and_repair(monkeypatch, movie, verbose, read_back):
    streams = []
    network = []
    base = {}
    changes = recipe_nodes(movie)
    topic = "movie_scraper" if movie else "json_indexer"
    detail = {
        "resource": {
            "id": 1,
            "name": "Lantern 1",
            "playback": "https://example.test/1.mp4",
        }
    }

    def validation(template, mode, context):
        """Describe a tool call with explicit sample data.

        Args:
            template: Exact end-node template.
            mode: Business output contract.
            context: Isolated supplied fixture values.

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
        assert (
            history.context_size(body["messages"], body.get("tools", []))
            <= 32000 - 4096
        )
        if not body.get("stream"):
            summary = {
                "goals": ["Build the requested fixture workflow."],
                "constraints": ["Do not publish or execute."],
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
        if step == 1:
            calls = [
                (
                    "create_workflow_draft",
                    {
                        "name": topic,
                        "category": "ingest" if movie else "indexer",
                        "description": "Check and fix fixture templates.",
                    },
                )
            ]
        elif step == 3:
            names = (
                ("ingest_start", "http", "movie")
                if movie
                else (
                    "search_start",
                    "http",
                    "search_end",
                    "details_start",
                    "details_end",
                )
            )
            calls = [("get_workflow_reference", {"topic": topic})] + [
                ("list_node_types", {"node_type": name, "include_examples": False})
                for name in names
            ]
            if read_back:
                calls.append(("get_workflow", {"workflow_id": base["workflow_id"]}))
        elif step == 4:
            names = {tool["function"]["name"] for tool in body["tools"]}
            assert "preview_workflow_draft" in names
            assert "list_media_items" not in names
            calls = (
                [
                    (
                        "probe_http_request",
                        {
                            "request": {"url": "https://example.test/movies"},
                            "formatter": HTML_FORMATTER,
                        },
                    )
                ]
                if movie
                else [
                    (
                        "probe_http_request",
                        {
                            "request": {"url": "https://example.test/search?page=2"},
                            "formatter": JSON_FORMATTER,
                        },
                    ),
                    (
                        "probe_http_request",
                        {
                            "request": {"url": "https://example.test/details/1"},
                            "formatter": JSON_FORMATTER,
                        },
                    ),
                ]
            )
        elif step == 5:
            calls = (
                [
                    validation(
                        MOVIE_TEMPLATE.replace(
                            "xpath_all('.//span", "xpath_first('.//span"
                        ),
                        "movie",
                        {"html": MOVIE_HTML},
                    )
                ]
                if movie
                else [
                    validation(
                        SEARCH_TEMPLATE.replace("page.count", "page.results|length"),
                        "search",
                        {
                            "payload": dumps(search_page(2)).decode(),
                            "page_num": 2,
                            "page_size": 1,
                        },
                    ),
                    validation(
                        DETAILS_TEMPLATE, "details", {"payload": dumps(detail).decode()}
                    ),
                ]
            )
        elif step == 6:
            assert (
                "Correct the reported output fields" in dumps(body["messages"]).decode()
            )
            calls = (
                [
                    validation(MOVIE_TEMPLATE, "movie", {"html": html})
                    for html in (
                        MOVIE_HTML,
                        MOVIE_HTML.replace('<time data-year="2024"/>', ""),
                        "<html/>",
                    )
                ]
                if movie
                else [
                    validation(
                        SEARCH_TEMPLATE,
                        "search",
                        {
                            "payload": dumps(search_page(page)).decode(),
                            "page_num": page,
                            "page_size": 1,
                        },
                    )
                    for page in (1, 2, 3)
                ]
                + [
                    validation(
                        DETAILS_TEMPLATE, "details", {"payload": '{"resource":null}'}
                    )
                ]
            )
        elif step == 7:
            assert "preview_workflow_draft" in {
                tool["function"]["name"] for tool in body["tools"]
            }
            calls = [("preview_workflow_draft", {**base, "changes": changes})]
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

    configure(monkeypatch, handler)

    def endpoint(req):
        network.append(req)
        if req.url.path == "/movies":
            return httpx.Response(200, text=MOVIE_HTML)
        return httpx.Response(
            200,
            json=detail
            if req.url.path.startswith("/details/")
            else search_page(int(req.url.params["page"])),
        )

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(endpoint)
    )

    async def run():
        async with database() as (owner, _):
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            created = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request("create", prompt="Create a fixture workflow."),
                    owner,
                )
            ]
            assert created[-1].event == "done", created
            graph = await FlowGraph.get(name=topic)
            snapshot = await Sessions.snapshot(
                conversation["id"],
                graph.id,
                0,
                GraphDocument(nodes=[], edges=[]),
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
                        prompt="Use the supplied example.test fixtures to build "
                        "the complete workflow; correct template errors, "
                        "do not publish or execute.",
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
            assert events[-1].event == "done", (
                events[-1],
                [(len(s["messages"]), s.get("tool_choice")) for s in streams],
            )
            assert len(streams) == 8
            results = [event.data for event in events if event.event == "tool_result"]
            assert all(result["ok"] for result in results), results
            checks = [
                result["data"]
                for result in results
                if result["name"] == "validate_workflow_template"
            ]
            assert not checks[0]["valid"] and checks[0]["issues"]
            assert all(check["valid"] for check in checks[1:]), checks
            preview = results[-1]["data"]
            assert preview["validation"]["valid"], preview
            assert preview["proposal_id"] and preview["canvas_change_count"] > 0
            proposed = await Sessions.read_snapshot(
                conversation["id"], preview["proposal_snapshot_id"], owner
            )
            assert len(proposed.document["nodes"]) == (3 if movie else 6)
            assert len(proposed.document["edges"]) == (2 if movie else 4)
            for item in changes:
                if item["op"] == "add_node":
                    assert (
                        next(
                            n
                            for n in proposed.document["nodes"]
                            if n["id"] == item["node"]["id"]
                        )["data"]
                        == item["node"]["data"]
                    )
            await graph.refresh_from_db()
            assert graph.draft in ({"nodes": [], "edges": []}, None)
            assert graph.definition is None and graph.draft_version == 0
            assert await FlowLog.all().count() == 0
            assert len(network) == (1 if movie else 2)
            # unrelated tools return on the next turn without changing account rights
            followup = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"],
                    request("followup", prompt="Summarize the result."),
                    owner,
                )
            ]
            assert followup[-1].event == "done"
            assert "list_media_items" in {
                tool["function"]["name"] for tool in streams[-1]["tools"]
            }

    asyncio.run(run())
