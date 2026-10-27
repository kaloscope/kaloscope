"""Unit tests for preserving stored library bindings in the library editor."""

import asyncio
import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sanic.response.types import BaseHTTPResponse
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.models.flow import FlowGraph, FlowTrigger, GraphCategory, GraphRef
from app.models.media import LibType, MediaLib, MediaLibUpsert
from app.models.user import Permissions, UserRole
from app.routes import media as routes
from app.services.flow import FlowTriggerService
from app.services.media import MediaLibService
from app.utils.json import dumps, loads

WATCHER = SimpleNamespace(is_scanning=lambda _: False)
ADMIN = SimpleNamespace(id=1, role=UserRole.ADMIN, perms=None)


@pytest.fixture(autouse=True)
def workspace(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp("workspace-temp")
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))
    # encode route responses with the application's JSON rules
    monkeypatch.setattr(BaseHTTPResponse, "_dumps", lambda value, **_: dumps(value))


@asynccontextmanager
async def library(tmp_path, layout):
    """Create a movie library with stored bindings in the given order.

    Args:
        tmp_path: Isolated library root.
        layout: Workflow state and asynchronous flag of each binding.

    Yields:
        The library and its bound workflows in stored order.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        lib = await MediaLib.create(
            lib_type=LibType.MOVIE, dir=str(tmp_path), name="Movies", priority=1
        )
        graphs = []
        for index, (state, asynchronous) in enumerate(layout, start=1):
            graph = await FlowGraph.create(
                name=f"Scraper {index}", category=GraphCategory.INGEST, state=state
            )
            await FlowTrigger.create(
                category=GraphCategory.INGEST,
                rel_id=lib.id,
                graph=graph,
                priority=index * 10,
                asynchronous=asynchronous,
            )
            graphs.append(graph)
        yield lib, graphs
    finally:
        await Tortoise.close_connections()


async def stored(lib_id):
    """Read stored bindings in run order.

    Args:
        lib_id: Library whose bindings are read.

    Returns:
        Workflow ID and asynchronous flag of each stored binding.
    """
    return [
        (row.graph_id, row.asynchronous)
        for row in await FlowTrigger.filter(rel_id=lib_id).order_by("priority")
    ]


async def editor_list(user=ADMIN):
    """Read libraries as the library settings page does.

    Args:
        user: Account loading the page.

    Returns:
        Library list response data.
    """
    route = inspect.unwrap(routes.list_libraries)
    http = SimpleNamespace(
        ctx=SimpleNamespace(user=user),
        app=SimpleNamespace(ctx=SimpleNamespace(lib_watcher=WATCHER)),
    )
    return loads((await route(http)).body)


async def save(lib, triggers, **settings):
    """Save the library as the editor does, sending back its listed bindings.

    Args:
        lib: Library being edited.
        triggers: Bindings currently shown in the editor.
        **settings: Library fields changed in the editor.

    Returns:
        Updated library.
    """
    return await MediaLibService.upsert(
        MediaLibUpsert(
            id=lib.id,
            name=settings.get("name", lib.name),
            danmaku_ttl=24,
            triggers=[GraphRef.model_validate(trigger) for trigger in triggers],
        )
    )


@pytest.mark.parametrize(
    "layout",
    [
        [("draft", False)],
        [("published", True), ("draft", False), ("modified", False)],
        [("draft", True), ("published", False), ("draft", False)],
    ],
    ids=["unpublished", "mixed", "unpublished-first"],
)
def test_unchanged_save(tmp_path, layout):
    async def run():
        async with library(tmp_path, layout) as (lib, graphs):
            before = await stored(lib.id)

            listed = (await editor_list())[0]["triggers"]
            await save(lib, listed, name="Renamed")
            runtime = await FlowTriggerService.get_triggers(
                GraphCategory.INGEST, lib.id
            )

            assert [
                (item["graph_id"], item["asynchronous"], item["published"])
                for item in listed
            ] == [
                (graph.id, asynchronous, state != "draft")
                for graph, (state, asynchronous) in zip(graphs, layout, strict=True)
            ]
            assert await stored(lib.id) == before
            assert (await MediaLib.get(id=lib.id)).name == "Renamed"
            # stored bindings to unpublished workflows still never execute
            assert [item["graph_id"] for item in runtime] == [
                graph.id
                for graph, (state, _) in zip(graphs, layout, strict=True)
                if state != "draft"
            ]
            assert all("published" not in item for item in runtime)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["remove", "reorder", "asynchronous", "clear"])
def test_deliberate_changes(tmp_path, change):
    layout = [("published", False), ("draft", True), ("published", True)]

    async def run():
        async with library(tmp_path, layout) as (lib, graphs):
            listed = (await editor_list())[0]["triggers"]
            if change == "remove":
                edited = [item for item in listed if item["graph_id"] != graphs[1].id]
            elif change == "reorder":
                edited = list(reversed(listed))
            elif change == "asynchronous":
                edited = [
                    {**item, "asynchronous": not item["asynchronous"]}
                    for item in listed
                ]
            else:
                edited = []

            await save(lib, edited)

            assert await stored(lib.id) == [
                (item["graph_id"], item["asynchronous"]) for item in edited
            ]

    asyncio.run(run())


def test_other_views(tmp_path):
    layout = [("draft", False), ("published", True)]

    async def run():
        async with library(tmp_path, layout) as (lib, graphs):
            reader = SimpleNamespace(
                id=2, role=UserRole.USER, perms=Permissions(media_lib_ids=[lib.id])
            )

            page = (await editor_list(reader))[0]["triggers"]
            default = (await MediaLibService.list_libraries(ADMIN, WATCHER))[0][
                "triggers"
            ]

            # only administrators editing libraries receive unpublished bindings
            assert [item["graph_id"] for item in page] == [graphs[1].id]
            assert [item["graph_id"] for item in default] == [graphs[1].id]
            assert all("published" not in item for item in page + default)

    asyncio.run(run())
