"""Tests for novel and comic workflow previews and automatic publication."""

import asyncio
import base64
import zipfile
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sanic import Sanic
from sanic.response import BaseHTTPResponse
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.exceptions import error_handler
from app.core.flow.context import MANUAL_KEY, RETVAL_KEY, Context
from app.core.flow.engine import FlowEngine
from app.core.flow.nodes.base import CancellationSignal, Node, NodeGroup
from app.core.flow.nodes.metadata.comic import ComicNode
from app.core.flow.nodes.metadata.novel import NovelNode
from app.core.media.common import ContentError
from app.core.media.metadata import METADATA_BYTES, parse_comicinfo, parse_opf
from app.models.flow import FlowGraph, FlowLog, GraphCategory, GraphState
from app.models.media import (
    LibType,
    MediaEvent,
    MediaFormat,
    MediaItem,
    MediaLib,
)
from app.models.user import UserRole
from app.routes.flow import flow as flow_blueprint
from app.services.media import MediaItemService
from app.utils import json
from app.utils.dict import TrackableDict

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)


def _context(params: dict) -> Context:
    """Create an isolated workflow context without querying a database.

    Args:
        params: The execution boot parameters.

    Returns:
        A mutable context retaining boot parameters separately from node variables.
    """
    context = Context.__new__(Context)
    context.globalvars = {}
    context.localvars = {}
    context.bootparams = params
    context.storage = TrackableDict()
    context.union()
    return context


@asynccontextmanager
async def _database() -> AsyncGenerator[None]:
    """Provide an in-memory schema without starting application workers.

    Yields:
        Control while the isolated schema is available.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    await Tortoise.generate_schemas()
    try:
        yield
    finally:
        await Tortoise.close_connections()


@pytest.fixture(
    params=[("novel", NovelNode), ("comic", ComicNode)], ids=["novel", "comic"]
)
def reading_node(request):
    """Provide the expected library type and its metadata node.

    Args:
        request: The current parametrized fixture request.

    Returns:
        The library type and node class to exercise.
    """
    return request.param


def test_node_schema(reading_node):
    lib_type, node = reading_node
    schema = next(schema for schema in Node.schemas if schema.node_type == lib_type)
    assert schema.name == node.__name__
    assert schema.group == NodeGroup.END
    assert schema.categories == (GraphCategory.INGEST,)
    assert schema.icon == ("bookText" if lib_type == "novel" else "imageMultiple")
    assert schema.order == (4 if lib_type == "novel" else 5)
    assert [handle.id for handle in schema.handles] == ["input"]
    assert [field.id for field in schema.fields] == ["example", "response", "force_end"]
    assert node.example.template == f"resp/{lib_type}.jsonc"
    assert lib_type in Node.executors
    assert "reading" not in Node.executors
    assert all(schema.node_type != "reading" for schema in Node.schemas)


def test_reading_example(reading_node):
    lib_type, node = reading_node
    template = (
        Path(__file__).resolve().parents[2]
        / "frontend/src/templates"
        / node.example.template
    )
    candidates = json.try_loads(template.read_text(), with_comments=True)
    assert isinstance(candidates, list) and len(candidates) == 1
    candidate = candidates[0]
    assert isinstance(candidate, dict)
    candidate["title"] = "Book"
    cover: json.JSONType = {"href": "cover.jpg"} if lib_type == "novel" else {"page": 0}
    candidate["cover"] = cover
    if lib_type == "novel":
        assert {"volume", "tags", "page_count", "black_and_white"}.isdisjoint(candidate)
    context = _context({MANUAL_KEY: True, "lib_type": lib_type})
    asyncio.run(
        node.execute(node_data={"response": json.pretty(candidates)}, context=context)
    )
    assert context[RETVAL_KEY][0]["title"] == "Book"
    assert context[RETVAL_KEY][0]["cover"] == cover


def test_reading_preview(monkeypatch, reading_node):
    lib_type, node = reading_node
    save = AsyncMock()
    monkeypatch.setattr(MediaItemService, "save_metadata", save)
    context = _context(
        {MANUAL_KEY: True, "lib_type": lib_type, "item_id": 999, "title": "Book"}
    )
    # node variables cannot turn a manual preview into automatic publication
    context[MANUAL_KEY] = False
    context["lib_type"] = "movie"
    asyncio.run(
        node.execute(
            node_data={
                "response": '[ // candidates\n {"title":" {{title}} ",'
                '"authors":[" A ","A"],"rating":8.5}, {"title":"Second"},]'
            },
            context=context,
        )
    )
    assert context[RETVAL_KEY] == [
        {"title": "Book", "authors": ["A"], "rating": "8.5"},
        {"title": "Second"},
    ]
    save.assert_not_awaited()


@pytest.mark.parametrize(
    "response",
    [
        "broken",
        "null",
        '{"title":"Book"}',
        "[null]",
        '[{"title":""}]',
        '[{"title":"Book","output_path":"/tmp/metadata.opf"}]',
        '[{"title":"Book","overwrite":true}]',
        '[{"title":"Book","rating":true}]',
        '[{"title":"Book","page_count":-1}]',
        '[{"title":"Book","year":2026,"month":2,"day":30}]',
        '[{"title":"Book"},{"title":""}]',
    ],
)
def test_reading_invalid(response, reading_node):
    lib_type, node = reading_node
    context = _context({MANUAL_KEY: True, "lib_type": lib_type})
    with pytest.raises(ContentError, match="invalid_metadata"):
        asyncio.run(
            node.execute(
                node_data={"response": response, "force_end": True}, context=context
            )
        )
    assert context.get(RETVAL_KEY) is None


def test_reading_unrepresentable(reading_node):
    lib_type, node = reading_node
    fields = {"tags": ["Tag"]} if lib_type == "novel" else {"volume": "Special"}
    context = _context({MANUAL_KEY: True, "lib_type": lib_type})
    with pytest.raises(ContentError, match="invalid_metadata"):
        asyncio.run(
            node.execute(
                node_data={"response": json.pretty([{"title": "Book", **fields}])},
                context=context,
            )
        )


@pytest.mark.parametrize("limit", ["bytes", "count"])
def test_reading_limits(limit, reading_node):
    lib_type, node = reading_node
    response = (
        " " * (METADATA_BYTES + 1) if limit == "bytes" else "[{}" + ",{}" * 100 + "]"
    )
    with pytest.raises(ContentError, match="media_limit_exceeded"):
        asyncio.run(
            node.execute(
                node_data={"response": response},
                context=_context({MANUAL_KEY: True, "lib_type": lib_type}),
            )
        )


@pytest.mark.parametrize("response", ["[]", '[{"title":"Book"}]'])
def test_reading_force_end(response, reading_node):
    lib_type, node = reading_node
    context = _context({MANUAL_KEY: True, "lib_type": lib_type})
    with pytest.raises(CancellationSignal):
        asyncio.run(
            node.execute(
                node_data={"response": response, "force_end": True}, context=context
            )
        )
    assert context[RETVAL_KEY] == json.loads(response)


@pytest.mark.parametrize("id", [None, True, 0, -1, "1"])
def test_reading_item_id(id, reading_node):
    lib_type, node = reading_node
    context = _context({"item_id": id, "lib_type": lib_type})
    # a node variable does not authorize manual mode or a different target item
    context[MANUAL_KEY] = True
    context["item_id"] = 1
    with pytest.raises(ContentError, match="invalid_metadata"):
        asyncio.run(node.execute(node_data={"response": "[]"}, context=context))


@pytest.mark.parametrize(
    "lib_type", [None, "movie", "tv_show", "opf", "image", True, [], {}, "other"]
)
def test_reading_lib_type(lib_type, reading_node):
    expected_type, node = reading_node
    if lib_type == "other":
        lib_type = "comic" if expected_type == "novel" else "novel"
    params: dict[str, object] = {MANUAL_KEY: True}
    if lib_type is not None:
        params["lib_type"] = lib_type
    with pytest.raises(ContentError, match="unsupported_media_format"):
        asyncio.run(
            node.execute(node_data={"response": "[]"}, context=_context(params))
        )


@pytest.mark.parametrize("problem", ["missing", "video", "other"])
def test_reading_source(tmp_path, monkeypatch, problem, reading_node):
    lib_type, node = reading_node
    source_type = LibType(lib_type)
    if problem == "video":
        source_type = LibType.MOVIE
    elif problem == "other":
        source_type = LibType.COMIC if lib_type == "novel" else LibType.NOVEL
    save = AsyncMock()
    monkeypatch.setattr(MediaItemService, "save_metadata", save)

    async def run():
        async with _database():
            lib = await MediaLib.create(
                name="Source",
                dir=str(tmp_path),
                priority=1,
                lib_type=source_type,
            )
            item = await MediaItem.create(
                lib=lib,
                name="Book",
                path=str(tmp_path / "Book.txt"),
                dir=str(tmp_path),
                format=MediaFormat.TXT,
            )
            # automatic execution must check the database type, not the supplied one
            params = {"item_id": item.id, "lib_type": lib_type}
            if problem == "missing":
                await item.delete()
            error = "not_found" if problem == "missing" else "unsupported_media_format"
            with pytest.raises(ContentError, match=error):
                await node.execute(
                    node_data={"response": '[{"title":"Book"}]'},
                    context=_context(params),
                )
            assert not await MediaEvent.all().exists()
            save.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.USER])
@pytest.mark.parametrize("category", list(GraphCategory))
def test_ingest_execution_mode(role, category):
    async def run():
        async with _database():
            graph = await FlowGraph.create(
                name="Execution", category=category, state=GraphState.PUBLISHED
            )
            engine = SimpleNamespace(execute=AsyncMock(return_value=[]))
            original_dumps = BaseHTTPResponse._dumps
            app = Sanic(
                f"flow_reading_{uuid4().hex}",
                configure_logging=False,
                dumps=json.dumps,
            )
            app.config.AUTO_EXTEND = False
            app.config.TOUCHUP = False
            app.ctx.flow_engine = engine
            app.blueprint(flow_blueprint, url_prefix="/_api/flow")
            app.error_handler.add(Exception, error_handler)
            app.register_middleware(
                lambda request: setattr(
                    request.ctx, "user", SimpleNamespace(role=role)
                ),
                "request",
            )
            app.asgi = True
            try:
                await app._startup()
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    response = await client.post(
                        f"/_api/flow/graph/{graph.id}/execute",
                        json={MANUAL_KEY: False, "item_id": 1},
                    )
            finally:
                Sanic.unregister_app(app)
                BaseHTTPResponse._dumps = original_dumps
            if category == GraphCategory.INGEST and role != UserRole.ADMIN:
                assert response.status_code == 403
                engine.execute.assert_not_awaited()
            else:
                assert response.status_code == 200 and response.json() == []
                assert engine.execute.await_args is not None
                assert engine.execute.await_args.kwargs["bootparams"][MANUAL_KEY] is (
                    category == GraphCategory.INGEST
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "format", [MediaFormat.TXT, MediaFormat.DIR, MediaFormat.CBZ, MediaFormat.ZIP, None]
)
@pytest.mark.parametrize("mode", ["manual", "automatic", "empty", "failed", "local"])
def test_reading_graph(tmp_path, monkeypatch, format, mode):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(cache))

    async def run():
        async with _database():
            root = tmp_path / "Library"
            directory = root / "Work"
            directory.mkdir(parents=True)
            novel = format == MediaFormat.TXT
            lib = await MediaLib.create(
                name="Reading",
                dir=str(root),
                priority=1,
                lib_type=LibType.NOVEL if novel else LibType.COMIC,
            )
            path = (
                directory
                if format in (None, MediaFormat.DIR)
                else directory / f"Book.{format}"
            )
            if novel:
                path.write_text("Chapter 1\nBody")
            elif format == MediaFormat.DIR:
                (path / "1.png").write_bytes(_PNG)
            elif format is not None:
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("1.png", _PNG)
            item = await MediaItem.create(
                lib=lib, path=str(path), dir=str(directory), name="Work", format=format
            )
            assert item.index_version is None
            target = directory / ("metadata.opf" if novel else "ComicInfo.xml")
            if mode == "local":
                assert await MediaItemService.save_metadata(item.id, {"title": "Local"})
            before = (await MediaItem.get(id=item.id)).title
            candidate = {"title": "First", "authors": ["Writer"]}
            if not novel:
                candidate["volume"] = "2"
            response = [] if mode == "empty" else [candidate, {"title": "Second"}]
            graph = await FlowGraph.create(
                name="Reading",
                category=GraphCategory.INGEST,
                state=GraphState.PUBLISHED,
                definition={
                    "nodes": [
                        {"id": "start", "data": {"$schema": "ingest_start"}},
                        {
                            "id": "end",
                            "data": {
                                "$schema": lib.lib_type,
                                "response": json.pretty(response),
                                "force_end": True,
                            },
                        },
                    ],
                    "edges": [
                        {
                            "source": "start",
                            "sourceHandle": "output",
                            "target": "end",
                            "targetHandle": "input",
                        }
                    ],
                },
            )
            if mode == "failed":
                target.write_text("invalid local metadata")
            params = {
                MANUAL_KEY: mode == "manual",
                "item_id": item.id,
                "lib_type": lib.lib_type if mode == "manual" else "movie",
                "item_path": str(tmp_path / "unowned.txt"),
                "metadata_path": str(tmp_path / "unowned.xml"),
            }
            engine = FlowEngine.__new__(FlowEngine)
            result = await engine.execute(graph.id, params)
            assert result == (None if mode == "failed" else response)
            assert not (tmp_path / "unowned.xml").exists()
            current = await MediaItem.get(id=item.id)
            if mode in ("manual", "empty"):
                assert not target.exists() and current.title == before
            elif mode == "failed":
                assert target.read_text() == "invalid local metadata"
                assert current.title == before
                assert await FlowLog.filter(
                    graph_id=graph.id, exc_info__not_isnull=True
                ).exists()
            else:
                parser = parse_opf if novel else parse_comicinfo
                assert parser(target.read_bytes()).data.title == current.title
                assert current.title == ("Local" if mode == "local" else "First")
            assert await MediaEvent.all().count() == (1 if mode == "failed" else 0)

    asyncio.run(run())
