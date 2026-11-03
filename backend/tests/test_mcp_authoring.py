"""Tests for shared recipe validation and MCP draft previews with isolated data."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.ai import tools
from app.core.flow.engine import FlowEngine
from app.models.download import DownloadTask
from app.models.flow import FlowGraph, FlowJob, FlowLog
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.services import workflow_authoring as authoring
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps
from tests.test_assistant_authoring import recipe_nodes
from tests.test_mcp import application, call
from tests.test_workflow_outputs import (
    DETAILS_TEMPLATE,
    HTML_FORMATTER,
    JSON_FORMATTER,
    MOVIE_HTML,
    MOVIE_TEMPLATE,
    SEARCH_TEMPLATE,
    search_page,
)


def test_shared_html_probe(monkeypatch):
    monkeypatch.setattr(
        authoring,
        "NetworkTransport",
        lambda **_: httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text='<table><tr><td><a href="/download/42">Fixture</a>'
                "</td></tr></table>",
                headers={"content-type": "text/html"},
            )
        ),
    )

    async def run():
        async with application() as app:
            args = {"request": {"url": "https://example.test/search"}}
            issued = await MCPAccessTokenService.create(
                app.shared_ctx.sessions["admin"].id,
                MCPAccessTokenCreate(name="fixture", scope=MCPTokenScope.READ_WRITE),
            )
            mcp = await call(
                app, "probe_http_request", {"probe": args}, token=issued["token"]
            )
            assistant = await tools.execute_tool(
                "probe_http_request",
                dumps(args).decode(),
                app.shared_ctx.sessions["admin"],
                conversation_id=1,
                call_id="probe",
            )

            assert assistant["ok"]
            assert mcp["structuredContent"] == assistant["data"]
            assert assistant["data"]["html_sample"]["table"]["observed_rows"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("movie", [False, True])
@pytest.mark.parametrize("include_nodes", [False, True])
def test_shared_recipe(monkeypatch, movie, include_nodes):
    execute = AsyncMock(side_effect=AssertionError("Recipes never execute workflows"))
    monkeypatch.setattr(FlowEngine, "execute", execute)
    network = []

    def endpoint(request):
        network.append(request)
        return (
            httpx.Response(200, text=MOVIE_HTML)
            if movie
            else httpx.Response(200, json=search_page(2))
        )

    monkeypatch.setattr(
        authoring, "NetworkTransport", lambda **_: httpx.MockTransport(endpoint)
    )

    async def run():
        async with application() as app:
            identity = app.shared_ctx.sessions["admin"]
            before = [
                await model.all().count()
                for model in (FlowGraph, FlowJob, FlowLog, DownloadTask)
            ]
            issued = await MCPAccessTokenService.create(
                identity.id,
                MCPAccessTokenCreate(name="fixture", scope=MCPTokenScope.READ_WRITE),
            )
            token = issued["token"]
            topic = "movie_scraper" if movie else "json_indexer"
            recipe = await call(
                app,
                "get_workflow_reference",
                {"topic": topic, "include_nodes": include_nodes},
                token="reader",
            )
            assert recipe["structuredContent"]["topic"] == topic
            shared = await tools.execute_tool(
                "get_workflow_reference",
                dumps({"topic": topic, "include_nodes": include_nodes}).decode(),
                app.shared_ctx.sessions["reader"],
                conversation_id=1,
                call_id="recipe",
            )
            assert shared["ok"] and shared["data"] == recipe["structuredContent"]
            node = await call(
                app,
                "list_node_types",
                {
                    "node_type": "movie" if movie else "search_end",
                    "include_examples": False,
                },
                token="reader",
            )
            assert not any(
                field.get("readonly")
                for field in node["structuredContent"]["end"][0]["fields"]
            )
            probe = await call(
                app,
                "probe_http_request",
                {
                    "probe": {
                        "request": {"url": "https://example.test/fixture"},
                        "formatter": HTML_FORMATTER if movie else JSON_FORMATTER,
                    }
                },
                token=token,
            )
            formatted = probe["structuredContent"]["formatted"]
            assert formatted["valid"]
            good = MOVIE_TEMPLATE if movie else SEARCH_TEMPLATE
            bad = (
                good.replace("xpath_all('.//span", "xpath_first('.//span")
                if movie
                else good.replace("page.count", "page.results|length")
            )
            for template, valid in ((bad, False), (good, True)):
                fixture = {
                    "template": template,
                    "mode": "movie" if movie else "search",
                    "context": {**formatted["value"], "page_num": 2, "page_size": 1},
                }
                for account in ("reader", "admin"):
                    result = await call(
                        app,
                        "validate_workflow_template",
                        {"validation": fixture},
                        token=account,
                    )
                    assistant = await tools.execute_tool(
                        "validate_workflow_template",
                        dumps(fixture).decode(),
                        app.shared_ctx.sessions[account],
                        conversation_id=1,
                        call_id="fixture",
                    )
                    assert result["structuredContent"] == assistant["data"]
                    assert result["structuredContent"]["valid"] is valid
            assert before == [
                await model.all().count()
                for model in (FlowGraph, FlowJob, FlowLog, DownloadTask)
            ]
            if not movie:
                for payload in (
                    {
                        "resource": {
                            "id": 1,
                            "name": "Lantern",
                            "playback": "https://example.test/1.mp4",
                        }
                    },
                    {"resource": None},
                ):
                    fixture = {
                        "template": DETAILS_TEMPLATE,
                        "mode": "details",
                        "context": {"payload": dumps(payload).decode()},
                    }
                    checked = await call(
                        app,
                        "validate_workflow_template",
                        {"validation": fixture},
                        token="reader",
                    )
                    assert checked["structuredContent"]["valid"]
            created = await call(
                app,
                "create_workflow_draft",
                {
                    "workflow": {
                        "name": topic,
                        "category": "ingest" if movie else "indexer",
                        "document": {"nodes": [], "edges": []},
                    }
                },
                token=token,
            )
            base = created["structuredContent"]
            changes = recipe_nodes(movie)
            document = {
                "nodes": [item["node"] for item in changes if item["op"] == "add_node"],
                "edges": [item["edge"] for item in changes if item["op"] == "add_edge"],
                "draft_version": base["draft_version"],
            }
            preview = await call(
                app,
                "preview_workflow_draft",
                {"workflow_id": base["workflow_id"], "draft": document},
                token=token,
            )
            assert preview["structuredContent"]["validation"]["valid"], preview
            graph = await FlowGraph.get(id=base["workflow_id"])
            assert graph.draft == {"nodes": [], "edges": []}
            assert (
                graph.definition is None
                and graph.draft_version == base["draft_version"]
            )
            assert len(network) == 1
            execute.assert_not_awaited()

    asyncio.run(run())
