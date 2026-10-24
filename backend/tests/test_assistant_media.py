"""Unit tests for read-only assistant media library and item tools."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast

import pytest
from tortoise import Tortoise

from app.core.ai import queries, tools
from app.core.exceptions import ErrorCode
from app.models.flow import FlowGraph, FlowTrigger, GraphCategory, GraphState
from app.models.media import MediaItem, MediaLib
from app.models.user import Permissions, UserInfo, UserRole

ADMIN = cast(UserInfo, SimpleNamespace(id=1, role=UserRole.ADMIN))
PLOT = "A linguist decodes an alien language. " * 20


@asynccontextmanager
async def media(tmp_path):
    """Create libraries with paged works, a hidden item, and a series of episodes.

    Args:
        tmp_path: Temporary directory holding library roots and the NFO file.

    Yields:
        Library and item fixtures plus a reader limited to the movie library.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        movies = await MediaLib.create(
            lib_type="movie", dir=str(tmp_path / "movies"), name="Movies", priority=1
        )
        shows = await MediaLib.create(
            lib_type="tv_show", dir=str(tmp_path / "shows"), name="Shows", priority=2
        )
        empty = await MediaLib.create(
            lib_type="movie", dir=str(tmp_path / "empty"), name="Empty", priority=3
        )
        ingest = await FlowGraph.create(
            name="Organize movies",
            category=GraphCategory.INGEST,
            state=GraphState.PUBLISHED,
        )
        await FlowTrigger.create(
            category=GraphCategory.INGEST, rel_id=movies.id, priority=1, graph=ingest
        )
        for index in range(1, 13):
            await MediaItem.create(
                lib=movies,
                dir=movies.dir,
                path=f"{movies.dir}/Movie {index:02}.mkv",
                name=f"Movie {index:02}.mkv",
                title=f"Movie {index:02}",
                year=2000 + index,
            )
        nfo = tmp_path / "arrival.nfo"
        nfo.write_text(
            f"<movie><title>Arrival</title><year>2016</year><plot>{PLOT}</plot></movie>"
        )
        arrival = await MediaItem.create(
            lib=movies,
            dir=movies.dir,
            path=f"{movies.dir}/Arrival.mkv",
            name="Arrival.mkv",
            title="Arrival",
            nfo_path=str(nfo),
        )
        hidden = await MediaItem.create(
            lib=movies,
            dir=movies.dir,
            path=f"{movies.dir}/Hidden.mkv",
            name="Hidden.mkv",
            title="Hidden Movie",
            visible=False,
        )
        show = await MediaItem.create(
            lib=shows, dir=shows.dir, path=f"{shows.dir}/Show", name="Show"
        )
        # create episodes out of order so the summary must sort them
        for episode in range(12, 0, -1):
            await MediaItem.create(
                lib=shows,
                parent=show,
                dir=show.path,
                path=f"{show.path}/S01E{episode:02}.mkv",
                name=f"S01E{episode:02}.mkv",
                season=1,
                episode=episode,
            )
        reader = cast(
            UserInfo,
            SimpleNamespace(
                id=2, role=UserRole.USER, perms=Permissions(media_lib_ids=[movies.id])
            ),
        )
        yield SimpleNamespace(
            movies=movies,
            shows=shows,
            empty=empty,
            arrival=arrival,
            hidden=hidden,
            show=show,
            reader=reader,
            ingest=ingest,
        )
    finally:
        await Tortoise.close_connections()


@pytest.fixture(autouse=True)
def watcher(monkeypatch):
    monkeypatch.setattr(
        queries,
        "lib_watcher",
        lambda: SimpleNamespace(is_scanning=lambda path: path.endswith("shows")),
    )


async def call(name, arguments=None, user=ADMIN):
    """Execute one assistant tool with JSON arguments.

    Args:
        name: Registered tool name.
        arguments: Tool arguments; omitted for none.
        user: Account executing the tool.

    Returns:
        The sanitized tool result.
    """
    return await tools.execute_tool(
        name, json.dumps(arguments or {}), user, conversation_id=1, call_id="test"
    )


def test_libraries(tmp_path):
    async def run():
        async with media(tmp_path) as data:
            listing = await call("list_media_libraries")
            limited = await call("list_media_libraries", user=data.reader)

            assert listing["data"]["total"] == 3
            libraries = {item["name"]: item for item in listing["data"]["items"]}
            assert libraries["Movies"]["ingest_workflows"] == [
                {"id": data.ingest.id, "name": "Organize movies"}
            ]
            assert libraries["Shows"]["scanning"] is True
            assert libraries["Movies"]["scanning"] is False
            assert "danmaku_server" not in libraries["Movies"]
            assert limited["data"]["total"] == 1
            assert limited["data"]["items"][0]["id"] == data.movies.id

    asyncio.run(run())


@pytest.mark.parametrize(
    ("page_num", "returned", "next_page"), [(1, 5, 2), (3, 3, None), (4, 0, None)]
)
def test_item_pages(tmp_path, page_num, returned, next_page):
    async def run():
        async with media(tmp_path) as data:
            result = await call(
                "list_media_items",
                {"library_id": data.movies.id, "page_num": page_num, "page_size": 5},
            )

            # twelve numbered movies and Arrival; the hidden item is excluded
            assert result["data"]["total"] == 13
            assert result["data"]["page_counts"] == {"returned": returned}
            assert result["data"]["next_page_num"] == next_page
            assert result["data"]["scope"] == "top_level_works"
            assert all(
                item["lib_id"] == data.movies.id for item in result["data"]["items"]
            )

    asyncio.run(run())


def test_item_filters(tmp_path):
    async def run():
        async with media(tmp_path) as data:
            keyword = await call("list_media_items", {"keyword": "movie 1"})
            hidden = await call("list_media_items", {"keyword": "Hidden"})
            top_level = await call("list_media_items", {"library_id": data.shows.id})
            episode = await call(
                "list_media_items", {"path": f"{data.show.path}/S01E03.mkv"}
            )
            limited = await call("list_media_items", user=data.reader)

            assert {item["title"] for item in keyword["data"]["items"]} == {
                "Movie 10",
                "Movie 11",
                "Movie 12",
            }
            assert hidden["data"]["total"] == 0
            assert [item["name"] for item in top_level["data"]["items"]] == ["Show"]
            assert episode["data"]["scope"] == "exact_path"
            assert episode["data"]["items"][0]["episode"] == 3
            assert episode["data"]["items"][0]["parent_id"] == data.show.id
            assert limited["data"]["total"] == 13

    asyncio.run(run())


@pytest.mark.parametrize(
    ("library", "user", "code"),
    [("missing", "admin", "not_found"), ("shows", "reader", "permission_denied")],
)
def test_unavailable_library(tmp_path, library, user, code):
    async def run():
        async with media(tmp_path) as data:
            library_id = 999 if library == "missing" else data.shows.id
            account = ADMIN if user == "admin" else data.reader

            result = await call("list_media_items", {"library_id": library_id}, account)

            assert result["ok"] is False
            assert result["error"]["code"] == code

    asyncio.run(run())


def test_empty_library(tmp_path):
    async def run():
        async with media(tmp_path) as data:
            result = await call("list_media_items", {"library_id": data.empty.id})

            assert result["ok"] is True
            assert result["data"]["total"] == 0
            assert result["data"]["page_counts"] == {"returned": 0}

    asyncio.run(run())


def test_item_summary(tmp_path):
    async def run():
        async with media(tmp_path) as data:
            movie = await call("get_media_item", {"item_id": data.arrival.id})
            show = await call("get_media_item", {"item_id": data.show.id})

            summary = movie["data"]
            assert summary["library_id"] == data.movies.id
            assert summary["lib"] == {
                "id": data.movies.id,
                "name": "Movies",
                "lib_type": "movie",
            }
            assert summary["metadata_state"] == "ready"
            assert summary["metadata"]["title"] == "Arrival"
            # the long plot is described, not silently cut to a partial string
            assert summary["metadata"]["plot"]["truncated"] is True
            assert summary["metadata"]["plot"]["characters"] == len(PLOT.strip())
            assert summary["metadata_field_count"] >= 3
            assert summary["children"] == {"total": 0, "items": [], "omitted": 0}
            series = show["data"]
            assert series["metadata_state"] == "missing"
            assert series["children"]["total"] == 12
            assert series["children"]["omitted"] == 2
            assert [child["episode"] for child in series["children"]["items"]] == list(
                range(1, 11)
            )

    asyncio.run(run())


def test_item_sections(tmp_path):
    async def run():
        async with media(tmp_path) as data:
            children = await call(
                "get_media_item",
                {"item_id": data.show.id, "section": "children", "offset": 10},
            )
            plot = await call(
                "get_media_item",
                {
                    "item_id": data.arrival.id,
                    "section": "metadata",
                    "field": "plot",
                    "offset": 20,
                    "limit": 30,
                },
            )
            missing = await call(
                "get_media_item",
                {"item_id": data.arrival.id, "section": "metadata", "field": "studio"},
            )

            assert children["data"]["total"] == 12
            assert [item["episode"] for item in children["data"]["data"]] == [11, 12]
            assert children["data"]["next_offset"] is None
            assert children["data"]["library_id"] == data.shows.id
            assert plot["data"]["data"] == PLOT.strip()[20:50]
            assert plot["data"]["next_offset"] == 50
            assert missing["error"]["code"] == ErrorCode.NOT_FOUND

    asyncio.run(run())


def test_long_metadata_text(tmp_path):
    async def run():
        async with media(tmp_path) as data:
            plot = "这是一段用于验收的虚构故事。" * 180 + "结尾：青岚灯塔。"
            (tmp_path / "arrival.nfo").write_text(
                f"<movie><title>Arrival</title><plot>{plot}</plot></movie>"
            )
            arguments = {
                "item_id": data.arrival.id,
                "section": "metadata",
                "field": "plot",
                "limit": 2000,
            }
            first = await call("get_media_item", arguments)

            assert first["ok"] is True
            assert first["data"]["data"] == plot[:2000]
            assert first["data"]["total"] == len(plot)
            assert first["data"]["next_offset"] == 2000

            last = await call("get_media_item", {**arguments, "offset": 2000})

            assert last["ok"] is True
            assert first["data"]["data"] + last["data"]["data"] == plot
            assert last["data"]["next_offset"] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    ("item", "user", "code"),
    [
        ("show", "reader", "permission_denied"),
        ("hidden", "admin", "not_found"),
        ("missing", "admin", "not_found"),
    ],
)
def test_unavailable_item(tmp_path, item, user, code):
    async def run():
        async with media(tmp_path) as data:
            item_id = 999 if item == "missing" else getattr(data, item).id
            account = ADMIN if user == "admin" else data.reader

            result = await call("get_media_item", {"item_id": item_id}, account)
            navigation = await call(
                "navigate", {"page": "media_item", "resource_id": item_id}, account
            )

            assert result["error"]["code"] == code
            assert navigation["error"]["code"] == code

    asyncio.run(run())


def test_navigation(tmp_path):
    async def run():
        async with media(tmp_path) as data:
            library = await call(
                "navigate",
                {"page": "media_library", "resource_id": data.movies.id},
                data.reader,
            )
            item = await call(
                "navigate", {"page": "media_item", "resource_id": data.show.id + 3}
            )
            denied = await call(
                "navigate",
                {"page": "media_library", "resource_id": data.shows.id},
                data.reader,
            )

            assert library["data"]["path"] == f"/medialibs/{data.movies.id}"
            assert library["data"]["library_id"] == data.movies.id
            assert (
                item["data"]["path"] == f"/medialibs/{data.shows.id}/{data.show.id + 3}"
            )
            assert denied["error"]["code"] == "permission_denied"

    asyncio.run(run())


def test_resources():
    libraries = {"items": [{"id": 1}, {"id": 2}]}
    items = {"items": [{"lib_id": 3}, {"lib_id": None}]}

    assert tools.media_resources("list_media_libraries", {}, libraries) == [
        "media_lib:1",
        "media_lib:2",
    ]
    assert tools.media_resources("list_media_items", {"library_id": 4}, items) == [
        "media_lib:3",
        "media_lib:4",
    ]
    assert tools.media_resources("get_media_item", {}, {"library_id": 5}) == [
        "media_lib:5"
    ]
    assert tools.media_resources("navigate", {}, {"library_id": None}) == []
    assert tools.media_resources("list_workflows", {}, {"items": [{"id": 1}]}) == []
