"""Tests for metadata diagnostics, manual recovery and automatic ingestion."""

import asyncio
import json
from unittest.mock import Mock

import pytest

from app.core.config import KaloscopeConfig
from app.core.flow.context import Context
from app.core.flow.nodes.metadata.episode import EpisodeNode
from app.core.flow.nodes.metadata.movie import MovieNode
from app.core.flow.nodes.metadata.tvshow import TVShowNode
from app.core.media import watcher
from app.core.media.common import ContentError
from app.models.flow import (
    FlowGraph,
    FlowInstance,
    FlowTrigger,
    GraphCategory,
    GraphState,
)
from app.models.media import LibType, MediaEvent, MediaFormat, MediaItem, MediaLib
from app.services import media as media_service
from app.services.media import MediaItemService
from tests.helpers import memory_db
from tests.test_services_media_index import _ingest_engine, _ingest_trigger, _source
from tests.test_services_media_reading import _client, _user


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """Isolate media locks and caches from the application workspace.

    Args:
        tmp_path: Temporary directory for this test.
        monkeypatch: Fixture restoring configuration after the test.
    """
    directory = tmp_path / "cache"
    directory.mkdir()
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(directory))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_manual_recovery(tmp_path, monkeypatch, asynchronous):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            graph = await _ingest_trigger(lib, asynchronous=asynchronous)
            definition = graph.definition
            assert definition is not None
            definition["nodes"][1]["data"]["response"] = "invalid"
            await FlowGraph.filter(id=graph.id).update(definition=definition)
            engine = _ingest_engine(monkeypatch)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            event = await MediaEvent.get(event_type="ingest")
            item = await MediaItem.get(lib_id=lib.id)
            original = item.original

            assert await MediaItemService.consume_ingest(event.id)
            if asynchronous:
                assert not await MediaItemService.consume_ingest(event.id)
                instance = await FlowInstance.get()
                await engine.execute(
                    instance.graph_id,
                    instance.bootparams,
                    recoverable=True,
                    recovery_id=instance.id,
                    asynchronous=None,
                )
            details = await MediaItemService.get_details(item.id, _user())
            assert details["metadata_task"] == {
                "state": "error",
                "error": "invalid_metadata",
            }
            assert not await MediaItemService.consume_ingest(event.id)

            async with _client(_user()) as client:
                response = await client.post(
                    f"/_api/media/{item.id}/metadata",
                    json={
                        "graph_id": graph.id,
                        "metadata": {"title": "Bad\u0001title"},
                    },
                )
                assert response.status_code == 422
                assert await MediaEvent.filter(id=event.id).exists()
                response = await client.post(
                    f"/_api/media/{item.id}/metadata",
                    json={"graph_id": graph.id, "metadata": {"title": "Manual"}},
                )
                assert response.status_code == 204
            await MediaItemService.finish_ingest(
                event.id, ContentError("invalid_metadata")
            )

            assert not await MediaEvent.exists()
            assert not await FlowInstance.exists()
            details = await MediaItemService.get_details(item.id, _user())
            assert details["metadata_task"] is None
            assert details["title"] == "Manual"
            assert (await MediaItem.get(id=item.id)).original == original

    asyncio.run(run())


def test_manual_save_scope(tmp_path):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            item = await MediaItemService.create_reading(lib.id, source)
            other = await MediaItem.create(
                lib=lib,
                path=str(tmp_path / "Other.txt"),
                dir=str(tmp_path),
                name="Other",
            )
            failures = []
            for owner in (item, other):
                failures.append(
                    await MediaEvent.create(
                        lib=lib,
                        src_path=owner.path,
                        event_type="ingest",
                        payload={
                            "bootparams": [{"item_id": owner.id}],
                            "error_code": "workflow_failed",
                        },
                    )
                )
            organization = await MediaEvent.create(
                lib=lib,
                src_path=item.path,
                event_type="ingest",
                payload={"bootparams": [], "organize_ids": [item.id]},
            )

            assert await MediaItemService.save_metadata(
                item.id, {"title": "Manual"}, overwrite=True
            )

            assert not await MediaEvent.filter(id=failures[0].id).exists()
            assert await MediaEvent.filter(id=failures[1].id).exists()
            assert await MediaEvent.filter(id=organization.id).exists()
            details = await MediaItemService.get_details(item.id, _user())
            assert details["metadata_task"] is None
            assert details["title"] == "Manual"

    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_workflow_chain(tmp_path, monkeypatch, failure):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            first = await _ingest_trigger(lib, asynchronous=True)
            await FlowTrigger.filter(graph_id=first.id).update(priority=0)
            definition = first.definition
            assert definition is not None
            definition["nodes"][1]["data"]["response"] = "invalid" if failure else "[]"
            await FlowGraph.filter(id=first.id).update(
                name="First ingest", definition=definition
            )
            second = await _ingest_trigger(lib, asynchronous=True)
            engine = _ingest_engine(monkeypatch)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            event = await MediaEvent.get(event_type="ingest")
            item = await MediaItem.get(lib_id=lib.id)

            assert await MediaItemService.consume_ingest(event.id)
            assert await FlowInstance.all().count() == 2
            for graph in (first, second):
                instance = await FlowInstance.get(graph_id=graph.id)
                await engine.execute(
                    instance.graph_id,
                    instance.bootparams,
                    recoverable=True,
                    recovery_id=instance.id,
                    asynchronous=None,
                )
                if graph.id == first.id:
                    assert await MediaEvent.filter(id=event.id).exists()
                    assert not await MediaItemService.consume_ingest(event.id)
                    details = await MediaItemService.get_details(item.id, _user())
                    assert details["metadata_task"] == {
                        "state": "pending",
                        "error": None,
                    }

            assert not await FlowInstance.exists()
            assert await MediaEvent.filter(id=event.id).exists() == failure
            details = await MediaItemService.get_details(item.id, _user())
            assert details["title"] == "Scraped"
            assert details["metadata_task"] == (
                {"state": "error", "error": "invalid_metadata"} if failure else None
            )

    asyncio.run(run())


@pytest.mark.parametrize("pending", [False, True])
def test_save_status(tmp_path, pending):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            item = await MediaItemService.create_reading(lib.id, source)
            await MediaEvent.create(
                lib=lib,
                src_path=item.path,
                event_type="ingest",
                payload={
                    "bootparams": [{"item_id": item.id}],
                    "error_code": "workflow_failed",
                },
            )
            payload: dict[str, int | str] = {"item_id": item.id}
            if not pending:
                payload["error_code"] = "metadata_write_failed"
            await MediaEvent.create(
                lib=lib,
                src_path=item.path,
                event_type="metadata",
                payload=payload,
            )

            details = await MediaItemService.get_details(item.id, _user())

            assert details["metadata_task"]["state"] == (
                "pending" if pending else "error"
            )
            if pending:
                assert details["metadata_task"]["error"] is None
            else:
                assert details["metadata_task"]["error"] in (
                    "workflow_failed",
                    "metadata_write_failed",
                )

    asyncio.run(run())


def test_concurrent_dispatch(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await _ingest_trigger(lib, asynchronous=True)
            _ingest_engine(monkeypatch)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            event = await MediaEvent.get(event_type="ingest")
            entered, release = asyncio.Event(), asyncio.Event()
            create = FlowInstance.create

            async def paused_create(**kwargs):
                """Pause the first workflow reservation before it reaches SQLite.

                Args:
                    **kwargs: Original workflow instance fields.

                Returns:
                    Persisted workflow instance.
                """
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                return await create(**kwargs)

            monkeypatch.setattr(FlowInstance, "create", paused_create)
            first = asyncio.create_task(MediaItemService.consume_ingest(event.id))
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(MediaItemService.consume_ingest(event.id))
            try:
                await asyncio.wait_for(asyncio.shield(second), 0.2)
            except TimeoutError:
                pass
            finally:
                release.set()
                results = await asyncio.wait_for(asyncio.gather(first, second), 5)

            assert sum(results) == 1
            assert await FlowInstance.all().count() == 1

    asyncio.run(run())


def test_completed_dispatch(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            graph = await _ingest_trigger(lib, asynchronous=True)
            definition = graph.definition
            assert definition is not None
            definition["nodes"][1]["data"]["response"] = "[]"
            await FlowGraph.filter(id=graph.id).update(definition=definition)
            engine = _ingest_engine(monkeypatch)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            event = await MediaEvent.get(event_type="ingest")
            assert await MediaItemService.consume_ingest(event.id)
            instance = await FlowInstance.get()
            entered, release = asyncio.Event(), asyncio.Event()
            finish = MediaItemService.finish_ingest

            async def paused_finish(*args, **kwargs):
                """Pause completion after the last workflow record is removed.

                Args:
                    *args: Original event and error arguments.
                    **kwargs: Original keyword arguments.
                """
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                await finish(*args, **kwargs)

            monkeypatch.setattr(MediaItemService, "finish_ingest", paused_finish)
            task = asyncio.create_task(
                engine.execute(
                    instance.graph_id,
                    instance.bootparams,
                    recoverable=True,
                    recovery_id=instance.id,
                    asynchronous=None,
                )
            )
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert not await FlowInstance.exists()
                assert await MediaItemService.consume_ingest(event.id)
            finally:
                release.set()
                await asyncio.wait_for(task, 5)

            assert not await FlowInstance.exists()
            assert not await MediaEvent.exists()

    asyncio.run(run())


def test_reserved_dispatch_recovery(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await _ingest_trigger(lib)
            engine = _ingest_engine(monkeypatch)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            event = await MediaEvent.get(event_type="ingest")
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    engine, "execute", Mock(side_effect=asyncio.CancelledError)
                )
                with pytest.raises(asyncio.CancelledError):
                    await MediaItemService.consume_ingest(event.id)

            assert not await MediaItemService.consume_ingest(event.id)
            assert await FlowInstance.all().count() == 1
            instance = await FlowInstance.get()
            await engine.execute(
                instance.graph_id,
                instance.bootparams,
                recoverable=True,
                recovery_id=instance.id,
                asynchronous=None,
            )

            assert not await FlowInstance.exists()
            assert not await MediaEvent.exists()
            assert (await MediaItem.get(lib_id=lib.id)).title == "Scraped"

    asyncio.run(run())


def test_running_ingests():
    async def run():
        async with memory_db():
            graph = await FlowGraph.create(
                name="Ingest",
                category=GraphCategory.INGEST,
                state=GraphState.PUBLISHED,
                definition={},
            )
            for params in (
                {"$media_ingest_event": 10, "item_id": 20},
                {"$manual": True, "$media_ingest_event": 30, "item_id": 40},
                {"$manual": False, "item_id": 50},
                {"$media_ingest_event": True, "item_id": True},
                {"$media_ingest_event": "10", "item_id": "20"},
                {"item_id": 0},
                {"item_id": -1},
                None,
                [],
            ):
                await FlowInstance.create(
                    graph=graph, definition={}, context={}, bootparams=params
                )

            assert await media_service._running_ingests() == ({10}, {20, 50})

    asyncio.run(run())


def test_running_unbound(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await _ingest_trigger(lib, asynchronous=True)
            engine = _ingest_engine(monkeypatch)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            event = await MediaEvent.get(event_type="ingest")
            assert await MediaItemService.consume_ingest(event.id)
            instance = await FlowInstance.get()
            await FlowTrigger.all().delete()

            assert not await MediaItemService.consume_ingest(event.id)
            assert await MediaEvent.filter(id=event.id).exists()
            await engine.execute(
                instance.graph_id,
                instance.bootparams,
                recoverable=True,
                recovery_id=instance.id,
                asynchronous=None,
            )

            assert not await MediaEvent.exists()
            assert not await FlowInstance.exists()

    asyncio.run(run())


@pytest.mark.parametrize("empty", [False, True])
def test_existing_workflow(tmp_path, monkeypatch, empty):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            graph = await _ingest_trigger(lib, asynchronous=True)
            if empty:
                definition = graph.definition
                assert definition is not None
                definition["nodes"][1]["data"]["response"] = "[]"
                await FlowGraph.filter(id=graph.id).update(definition=definition)
            engine = _ingest_engine(monkeypatch)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            event = await MediaEvent.get(event_type="ingest")
            item = await MediaItem.get(lib_id=lib.id)
            assert await MediaItemService.consume_ingest(event.id)
            instance = await FlowInstance.get()
            assert instance.bootparams is not None
            instance.bootparams.pop("$media_ingest_event")
            await instance.save(update_fields=["bootparams"])
            event.payload = {"bootparams": [{"item_id": item.id}]}
            await event.save(update_fields=["payload"])

            assert not await MediaItemService.consume_ingest(event.id)
            assert await FlowInstance.all().count() == 1
            await engine.execute(
                instance.graph_id,
                instance.bootparams,
                recoverable=True,
                recovery_id=instance.id,
                asynchronous=None,
            )
            assert await MediaItemService.consume_ingest(event.id)

            assert (await MediaItem.get(id=item.id)).title == (
                item.title if empty else "Scraped"
            )
            assert not await FlowInstance.exists()
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize("deleted", [False, True])
def test_obsolete_failure(tmp_path, deleted):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await _ingest_trigger(lib)
            item = await MediaItemService.create_reading(lib.id, source)
            event = await MediaEvent.get(event_type="ingest")
            assert event.payload is not None
            event.payload = {**event.payload, "error_code": "workflow_failed"}
            await event.save(update_fields=["payload"])
            if deleted:
                await item.delete()
            else:
                await MediaItem.filter(id=item.id).update(visible=False)

            assert await MediaItemService.consume_ingest(event.id)
            assert not await MediaEvent.exists()

    asyncio.run(run())


def test_manual_save_recovery(tmp_path, monkeypatch):
    async def run():
        async with memory_db():
            lib, source = await _source(tmp_path, MediaFormat.TXT)
            await _ingest_trigger(lib)
            _ingest_engine(monkeypatch)
            await MediaItemService.ingest_reading_work(lib.id, source.directory)
            ingest = await MediaEvent.get(event_type="ingest")
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    media_service,
                    "_publish_metadata",
                    Mock(side_effect=ContentError("metadata_write_failed")),
                )
                assert await MediaItemService.consume_ingest(ingest.id)
            save = await MediaEvent.get(event_type="metadata")
            assert not await MediaItemService.consume_ingest(ingest.id)
            assert not await MediaItemService.consume_metadata(save.id)
            assert not await FlowInstance.exists()
            assert await MediaItemService.save_metadata(
                (await MediaItem.get(lib_id=lib.id)).id,
                {"title": "Manual"},
                overwrite=True,
            )
            assert not await MediaItemService.consume_ingest(ingest.id)
            assert not await MediaItemService.consume_metadata(save.id)

            assert not await MediaEvent.exists()
            assert (await MediaItem.get(lib_id=lib.id)).title == "Manual"

    asyncio.run(run())


@pytest.mark.parametrize("valid", [False, True])
def test_video_save_http(tmp_path, valid):
    async def run():
        async with memory_db():
            video = tmp_path / "Movie.mkv"
            video.write_bytes(b"video")
            nfo = video.with_suffix(".nfo")
            original = b"<movie><title>Old</title><year>2000</year></movie>"
            nfo.write_bytes(original)
            lib = await MediaLib.create(
                name="Movies", dir=str(tmp_path), lib_type=LibType.MOVIE, priority=1
            )
            item = await MediaItem.create(
                lib=lib,
                path=str(video),
                dir=str(tmp_path),
                name="Movie",
                nfo_path=str(nfo),
                title="Old",
                year=2000,
            )
            before = await MediaItem.all().values()
            async with _client(_user()) as client:
                response = await client.post(
                    f"/_api/media/{item.id}/gen_nfo",
                    json={
                        "graph_id": 1,
                        "metadata": {
                            "title": "New & title" if valid else "Bad\x01title",
                            "rating": 0,
                        },
                    },
                )
                assert response.status_code == (204 if valid else 422)
                if valid:
                    current = await MediaItem.get(id=item.id)
                    assert current.title == "New & title" and current.year is None
                    assert current.rating == 0
                    details = (await client.get(f"/_api/media/{item.id}")).json()[
                        "data"
                    ]
                    assert (
                        details["title"]
                        == details["metadata"]["title"]
                        == current.title
                    )
                    assert details["metadata_state"] == "ready"
                else:
                    assert response.json()["message"] == "invalid_metadata"
                    assert nfo.read_bytes() == original
                    assert await MediaItem.all().values() == before

    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_video_ingest(tmp_path, monkeypatch, asynchronous, multipart, existing):
    async def run():
        async with memory_db():
            directory = tmp_path / "Movies"
            directory.mkdir()
            container = directory / "Film" if multipart else directory
            if multipart:
                container.mkdir()
            video = container / ("Film-cd1.mkv" if multipart else "Film.mkv")
            video.write_bytes(b"video")
            nfo = container / "Film.nfo"
            original = b"<movie><title>Local</title></movie>"
            if existing:
                nfo.write_bytes(original)
            lib = await MediaLib.create(
                name="Movies", dir=str(directory), lib_type=LibType.MOVIE, priority=1
            )
            await _ingest_trigger(lib, asynchronous=asynchronous)
            engine = _ingest_engine(monkeypatch)
            event = await MediaEvent.create(
                lib=lib, src_path=str(video), event_type="created", is_directory=False
            )

            assert await watcher.consume_event(event)
            for instance in await FlowInstance.all():
                await engine.execute(
                    instance.graph_id,
                    instance.bootparams,
                    recoverable=True,
                    recovery_id=instance.id,
                    asynchronous=None,
                )
            assert not await FlowInstance.exists()
            assert nfo.exists()
            if existing:
                assert nfo.read_bytes() == original
            event = await MediaEvent.create(
                lib=lib, src_path=str(nfo), event_type="modified", is_directory=False
            )
            assert await watcher.consume_event(event)

            item = await MediaItem.get(lib=lib, parent_id__isnull=True)
            assert item.title == ("Local" if existing else "Scraped")
            assert await MediaItem.filter(lib=lib).count() == (2 if multipart else 1)
            assert video.read_bytes() == b"video"
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("node", "kind", "mode"),
    [
        (MovieNode, "movie", "preview"),
        (MovieNode, "movie", "invalid"),
        (MovieNode, "movie", "empty"),
        (MovieNode, "movie", "existing"),
        (MovieNode, "movie", "no_target"),
        (MovieNode, "movie", "no_target_invalid"),
        (MovieNode, "movie", "deleted"),
        (TVShowNode, "tvshow", "preview"),
        (TVShowNode, "tvshow", "invalid"),
        (TVShowNode, "tvshow", "existing"),
        (EpisodeNode, "episode", "preview"),
        (EpisodeNode, "episode", "invalid"),
        (EpisodeNode, "episode", "existing"),
    ],
)
def test_video_workflow(tmp_path, node, kind, mode):
    async def run():
        async with memory_db():
            path = tmp_path / "video.nfo"
            original = b"original"
            if mode == "existing":
                path.write_bytes(original)
            context = await Context.create(
                1,
                {
                    "item_id": 1 if mode == "deleted" else None,
                    "nfo_type": None if mode.startswith("no_target") else kind,
                    "nfo_path": str(path),
                    "$manual": mode == "preview",
                },
            )
            data = {
                "response": "invalid"
                if mode == "no_target_invalid"
                else json.dumps(
                    []
                    if mode == "empty"
                    else [{"title": "Bad\x01title" if mode == "invalid" else "Title"}]
                ),
                "force_end": False,
            }
            if mode == "invalid":
                with pytest.raises(ContentError, match="invalid_metadata"):
                    await node.execute(node_data=data, context=context)
            else:
                await node.execute(node_data=data, context=context)
            assert (
                path.read_bytes() == original
                if mode == "existing"
                else not path.exists()
            )

    asyncio.run(run())
