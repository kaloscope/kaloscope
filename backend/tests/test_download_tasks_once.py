"""Tests for conversational one-time downloads, recovery and optional ingestion."""

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from app.core.ai import history, tools
from app.core.config import KaloscopeConfig
from app.core.dl.driver import DownloadSnapshot, DownloadSource, DownloadState
from app.core.dl.rpc import RpcConfig, RpcDriver
from app.core.exceptions import ErrorCode, ForbiddenException, KaloscopeException
from app.models.assistant import (
    AssistantConversation,
    AssistantTurn,
    ConversationCreate,
)
from app.models.download import (
    DownloadPlan,
    DownloadTask,
    DownloadTaskCheck,
    DownloadTaskCreate,
    IndexerSearch,
    TransferMethod,
)
from app.models.flow import FlowGraph, FlowLog, FlowTrigger
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.user import Permissions, UserRole
from app.services import download as service
from app.services.assistant import AssistantService
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.mcp import MCPAccessTokenService
from app.utils.bittorrent import decode_torrent
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, completion_client, database, request
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_conversation import engine_fixture
from tests.test_download_plan_ingestion import bindings, libraries
from tests.test_html_indexer_authoring import TORRENT
from tests.test_mcp import application, call
from tests.test_services_assistant import call_delta, chunk, configure, response
from tests.test_services_download import DraftDriver

HASH = "a" * 40
OTHER = "b" * 40
RESULTS = {
    "items": [
        {
            "id": "r1",
            "title": "Lantern 2024 1080p",
            "size": "2 GB",
            "link": f"magnet:?xt=urn:btih:{HASH}&tr=https://tracker.test/a/PASSKEY",
        },
        {
            "id": "r2",
            "title": "Lantern 2024 720p",
            "link": "https://tracker.test/files/r2.torrent?passkey=PASSKEY",
        },
        {"id": "r3", "title": "Lantern player", "link": "https://tracker.test/watch/3"},
        {"id": "r4", "title": "Lantern details"},
    ],
    "total": 9,
}


@pytest.fixture(autouse=True)
def sealing(isolate, tmp_path):
    # sealed sources use the workspace key like stored provider credentials
    KaloscopeConfig._config.secret_key_path = tmp_path / "secret.key"


class Rpc(RpcDriver):
    """Accept submissions like a local RPC downloader without the network."""

    def __init__(self, *, error=None, sources=("magnet", "torrent")):
        """Configure one optional submission failure and supported sources.

        Args:
            error: Exception raised by the first submission.
            sources: Source kinds the downloader accepts.
        """
        self.error = error
        self.kinds = frozenset(DownloadSource(kind) for kind in sources)
        self.added = []
        self.config = RpcConfig(
            name="Fixture",
            host="downloader.test",
            port=80,
            methods={"add_link": {"post": "/add"}, "add_torrent": {"post": "/add"}},
        )

    @property
    def source_types(self):
        """Report the configured source kinds.

        Returns:
            Accepted download sources.
        """
        return self.kinds

    async def add(self, request):
        """Record one submission, raising the configured failure once.

        Args:
            request: Normalized submission.

        Returns:
            Snapshot with a remote identity.
        """
        self.added.append(request.link)
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return DownloadSnapshot(
            identity=SimpleNamespace(remote_id=f"rpc-{len(self.added)}")
        )

    async def close(self):
        """Release nothing; the fake owns no client."""


class Offline(DraftDriver):
    """Give each offline submission its own job and record remote calls."""

    def __init__(self, **kwargs):
        """Initialize the shared offline driver fixture.

        Args:
            **kwargs: Options forwarded to the shared test driver.
        """
        super().__init__(**kwargs)
        self.added = []

    def prepare(self, request):
        """Prepare a submission with a fresh offline job.

        Args:
            request: Normalized submission.

        Returns:
            Draft persisted before the remote call.
        """
        self.job_uuid = uuid4().hex
        return super().prepare(request)

    async def add(self, request):
        """Record the remote call before using the shared driver.

        Args:
            request: Submission identity and directory.

        Returns:
            Shared snapshot.
        """
        self.added.append(request.link)
        return await super().add(request)

    async def close(self):
        """Release nothing; the fake owns no client."""


def site(request):
    """Serve the synthetic tracker's torrent and player pages.

    Args:
        request: HEAD or GET request from source normalization.

    Returns:
        Torrent content for the file URL, otherwise an HTML page.
    """
    if request.url.path.startswith("/files/"):
        return httpx.Response(
            200, content=TORRENT, headers={"content-type": "application/x-bittorrent"}
        )
    return httpx.Response(
        200, text="<html>player</html>", headers={"content-type": "text/html"}
    )


async def searched(owner, graph_id, monkeypatch, driver, client):
    """Search with sealed sources and route submissions to a fake downloader.

    Args:
        owner: Administrator account.
        graph_id: Published indexer.
        monkeypatch: Pytest patch helper.
        driver: Fake downloader driver.
        client: HTTP client serving torrent files.

    Returns:
        Search result items with sealed sources.
    """
    service.Sanic.get_app().ctx.httpx = client
    monkeypatch.setattr(service, "load_driver", lambda _: driver)
    result = await service.DownloadPlanService.search_indexer(
        IndexerSearch(workflow_id=graph_id, keyword="Lantern", sources=True), owner
    )
    return {item["id"]: item for item in result["items"]}


def settings(graph_id, item, downloader_id, **extra):
    """Build one selected result's download settings.

    Args:
        graph_id: Indexer that returned the result.
        item: Search item with a sealed source.
        downloader_id: Target downloader.
        **extra: Transfer or ingestion settings.

    Returns:
        Shared validation fields.
    """
    return {
        "resource": {
            "workflow_id": graph_id,
            "resource_id": item["id"],
            "source": item.get("source", "missing"),
        },
        "downloader_id": downloader_id,
        "dir": "/synthetic/downloads",
        **extra,
    }


def setup(monkeypatch, driver):
    """Patch the search engine and return an HTTP client for torrent files.

    Args:
        monkeypatch: Pytest patch helper.
        driver: Fake downloader receiving submissions.

    Returns:
        Engine mock and HTTP client context.
    """
    execute = engine_fixture(monkeypatch, RESULTS)
    return execute, httpx.AsyncClient(transport=httpx.MockTransport(site))


def test_sealed_sources(monkeypatch):
    driver = Rpc()
    execute, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            plain = await service.DownloadPlanService.search_indexer(
                IndexerSearch(workflow_id=graph.id, keyword="Lantern"), owner
            )

            assert "PASSKEY" not in dumps(items).decode()
            assert all("source" not in item for item in plain["items"])
            assert set(items) == {"r1", "r2", "r3", "r4"}
            assert "source" not in items["r4"]
            second = await service.DownloadPlanService.search_indexer(
                IndexerSearch(
                    workflow_id=graph.id,
                    keyword="Lantern",
                    page_num=2,
                    page_size=4,
                    sources=True,
                ),
                owner,
            )
            assert second["status"] == "ok", second
            assert second["has_more"] and second["next_page"] == 3
            assert [item["id"] for item in second["items"]] == list(items)
            assert execute.await_args.args[1]["page_num"] == 2
            for item, valid in [
                (second["items"][1], True),
                ({**items["r2"], "id": "r1"}, False),
                ({**items["r1"], "source": items["r1"]["source"][:-6]}, False),
            ]:
                check = await service.DownloadTaskService.validate_request(
                    DownloadTaskCheck.model_validate(
                        settings(graph.id, item, downloader.id)
                    ),
                    owner,
                )
                assert check["valid"] is valid, check["issues"]
                if not valid:
                    assert check["issues"][0]["path"] == "resource.source"
            # validation opens the reviewed result instead of searching again
            assert execute.await_count == 3

    asyncio.run(run())


@pytest.mark.parametrize(
    ("item", "sources", "path", "kind"),
    [
        ("r1", ("magnet", "torrent"), None, "magnet"),
        ("r2", ("magnet", "torrent"), None, "torrent"),
        ("r2", ("magnet",), None, "torrent"),
        ("r3", ("magnet", "torrent"), "resource.source", None),
        ("r1", ("raw",), "downloader_id", "magnet"),
    ],
    ids=["magnet", "torrent-url", "torrent-as-magnet", "player-page", "unsupported"],
)
def test_validation(monkeypatch, item, sources, path, kind):
    driver = Rpc(sources=sources)
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)

            result = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(
                    settings(graph.id, items[item], downloader.id)
                ),
                owner,
            )

            assert result["valid"] is (path is None), result["issues"]
            if path:
                assert path in [issue["path"] for issue in result["issues"]]
            download = result["download"]
            assert download["title"] == RESULTS["items"][int(item[1]) - 1]["title"]
            assert download.get("source_kind") == kind
            assert "PASSKEY" not in dumps(result).decode()
            assert not driver.added and not await DownloadTask.exists()

    asyncio.run(run())


def test_submission_and_replay(monkeypatch):
    driver = Rpc()
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            fields = settings(graph.id, items["r1"], downloader.id)
            check = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(fields), owner
            )
            body = DownloadTaskCreate(
                request_id=check["request_id"],
                info_hash=check["download"]["info_hash"],
                **fields,
            )

            preview = await service.DownloadTaskService.create_once(body, owner)
            confirmed = body.model_copy(update={"confirmed": True})
            created = await service.DownloadTaskService.create_once(confirmed, owner)
            replayed = await service.DownloadTaskService.create_once(confirmed, owner)
            changed = await service.DownloadTaskService.create_once(
                confirmed.model_copy(update={"dir": "/elsewhere"}), owner
            )
            again = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(fields), owner
            )
            independent = await service.DownloadTaskService.create_once(
                confirmed.model_copy(update={"request_id": uuid4()}), owner
            )

            assert preview["confirmation_required"] and not preview["created"]
            assert created["created"] and not created["replayed"]
            assert created["state"] == DownloadState.DOWNLOADING
            assert replayed["replayed"] and replayed["task_id"] == created["task_id"]
            assert replayed["download"] == created["download"]
            assert changed["conflict"] and not changed["created"]
            assert again["existing_download"]["task_id"] == created["task_id"]
            assert not again["valid"]
            assert not independent["created"]
            assert independent["existing_download"]["task_id"] == created["task_id"]
            assert len(driver.added) == 1
            task = await DownloadTask.get(id=created["task_id"])
            assert task.info_hash == HASH and task.unique_id == "rpc-1"
            assert task.creation_key and "PASSKEY" in task.magnet_link
            assert not await DownloadPlan.exists()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["rpc", "offline"])
def test_concurrent_retries(monkeypatch, kind):
    driver = Rpc() if kind == "rpc" else Offline()
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            fields = settings(graph.id, items["r2"], downloader.id)
            body = DownloadTaskCreate(
                request_id=uuid4(),
                confirmed=True,
                info_hash=decode_torrent(TORRENT).info_hash,
                **fields,
            )

            results = await asyncio.gather(
                *[
                    service.DownloadTaskService.create_once(body, owner)
                    for _ in range(3)
                ]
            )

            assert {result["task_id"] for result in results} == {results[0]["task_id"]}
            assert [result["replayed"] for result in results].count(False) == 1
            assert len(driver.added) == 1
            assert await DownloadTask.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("error", "uncertain"),
    [
        (httpx.ReadTimeout("no response"), True),
        (httpx.ConnectError("refused"), False),
    ],
    ids=["timeout", "refused"],
)
def test_rpc_outcomes(monkeypatch, error, uncertain):
    # the RPC client wraps transport errors while keeping them as context
    try:
        try:
            raise error
        except httpx.RequestError:
            raise KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED) from None
    except KaloscopeException as wrapped:
        failure = wrapped
    driver = Rpc(error=failure)
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            body = DownloadTaskCreate(
                request_id=uuid4(),
                confirmed=True,
                info_hash=HASH,
                **settings(graph.id, items["r1"], downloader.id),
            )

            if uncertain:
                first = await service.DownloadTaskService.create_once(body, owner)
                retry = await service.DownloadTaskService.create_once(body, owner)
                assert first["uncertain"] and first["state"] == "submit_unknown"
                assert retry["replayed"] and retry["task_id"] == first["task_id"]
                assert len(driver.added) == 1
            else:
                with pytest.raises(KaloscopeException):
                    await service.DownloadTaskService.create_once(body, owner)
                assert not await DownloadTask.exists()
                retry = await service.DownloadTaskService.create_once(body, owner)
                assert retry["created"] and not retry["uncertain"]
                assert len(driver.added) == 2

    asyncio.run(run())


def test_offline_unknown(monkeypatch):
    driver = Offline(state=DownloadState.SUBMIT_UNKNOWN)
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            body = DownloadTaskCreate(
                request_id=uuid4(),
                confirmed=True,
                info_hash=HASH,
                **settings(graph.id, items["r1"], downloader.id),
            )

            first = await service.DownloadTaskService.create_once(body, owner)
            retry = await service.DownloadTaskService.create_once(body, owner)

            assert first["uncertain"] and retry["replayed"]
            assert len(driver.added) == 1

    asyncio.run(run())


def test_ingestion(monkeypatch, tmp_path):
    driver = Rpc(error=httpx.ConnectError("downloader connection refused"))
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            data = await libraries(tmp_path)
            items = await searched(owner, data.graph.id, monkeypatch, driver, client)
            fields = settings(
                data.graph.id,
                items["r1"],
                data.downloader.id,
                transfer_lib_id=data.movies.id,
                transfer_method=TransferMethod.COPY,
            )
            before = await bindings(data.movies.id)
            check = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(
                    {**fields, "ingest_workflow_id": data.movie.id}
                ),
                owner,
            )
            review = check["ingestion"]
            body = DownloadTaskCreate(
                request_id=check["request_id"],
                confirmed=True,
                info_hash=check["download"]["info_hash"],
                ingestion={
                    "workflow_id": review["workflow_id"],
                    "revision": review["revision"],
                },
                **fields,
            )

            failed = await service.DownloadTaskService.create_once(body, owner)
            kept = await bindings(data.movies.id)
            retried = await service.DownloadTaskService.create_once(body, owner)
            replayed = await service.DownloadTaskService.create_once(body, owner)
            shows = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(
                    {
                        **fields,
                        "transfer_lib_id": data.shows.id,
                        "ingest_workflow_id": data.movie.id,
                    }
                ),
                owner,
            )

            assert check["valid"] and review["binding"] == "add"
            assert "not only" in review["scope"]
            assert not failed["created"] and failed["ingestion"]["binding"] == "added"
            assert "retry with the same request_id" in failed["submit_error"]
            assert kept == [*before, (data.movie.id, 3, False)]
            assert retried["created"] and retried["ingestion"]["binding"] == "reused"
            assert replayed["replayed"]
            assert replayed["ingestion"]["binding"] == "configured"
            assert replayed["download"]["workflow_id"] == data.graph.id
            assert replayed["download"]["transfer_lib_id"] == data.movies.id
            assert len(driver.added) == 2
            assert await bindings(data.movies.id) == kept
            assert await DownloadTask.all().count() == 1
            task = await DownloadTask.get(id=retried["task_id"])
            assert task.transfer_lib_id == data.movies.id
            assert not shows["valid"]
            assert "transfer_lib_id" in [issue["path"] for issue in shows["issues"]]

    asyncio.run(run())


def test_permissions(monkeypatch):
    driver = Rpc()
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            fields = settings(graph.id, items["r1"], downloader.id)
            check = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(fields), owner
            )
            await FlowGraph.filter(id=graph.id).update(state="draft")
            unpublished = await service.DownloadTaskService.create_once(
                DownloadTaskCreate(
                    request_id=check["request_id"],
                    confirmed=True,
                    info_hash=check["download"]["info_hash"],
                    **fields,
                ),
                owner,
            )
            await FlowGraph.filter(id=graph.id).update(state="published")
            owner.role = UserRole.USER
            owner.perms = Permissions(indexer_ids=[graph.id])

            for call_ in (
                service.DownloadTaskService.validate_request(
                    DownloadTaskCheck.model_validate(fields), owner
                ),
                service.DownloadTaskService.create_once(
                    DownloadTaskCreate(
                        request_id=uuid4(), confirmed=True, info_hash=HASH, **fields
                    ),
                    owner,
                ),
            ):
                with pytest.raises(ForbiddenException):
                    await call_

            assert check["valid"]
            assert not unpublished["created"]
            assert "resource.workflow_id" in [i["path"] for i in unpublished["issues"]]
            assert not driver.added and not await DownloadTask.exists()

    asyncio.run(run())


def test_mcp_parity(monkeypatch):
    driver = Rpc()
    _, client = setup(monkeypatch, driver)

    async def run():
        async with application() as app, client:
            graph, downloader, _ = await fixture_plan()
            admin = app.shared_ctx.sessions["admin"]
            items = await searched(admin, graph.id, monkeypatch, driver, client)
            fields = settings(graph.id, items["r1"], downloader.id)

            mcp = await call(
                app, "validate_download_task", {"check": fields}, token="admin"
            )
            assistant = await tools.execute_tool(
                "validate_download_task",
                dumps(fields).decode(),
                admin,
                conversation_id=1,
                call_id="check",
            )
            body = {
                "request_id": mcp["structuredContent"]["request_id"],
                "info_hash": mcp["structuredContent"]["download"]["info_hash"],
                "confirmed": True,
                **fields,
            }
            read_only = await call(
                app, "create_download_task", {"creation": body}, token="admin"
            )
            write = await MCPAccessTokenService.create(
                admin.id,
                MCPAccessTokenCreate(name="once", scope=MCPTokenScope.READ_WRITE),
            )
            created = await call(
                app, "create_download_task", {"creation": body}, token=write["token"]
            )
            replayed = await call(
                app, "create_download_task", {"creation": body}, token=write["token"]
            )

            assert not mcp.get("isError"), mcp
            mcp_data = mcp["structuredContent"]
            assert mcp_data.pop("request_id") and assistant["data"].pop("request_id")
            assert mcp_data == loads(dumps(assistant["data"]))
            assert read_only["isError"] and "read-only" in str(read_only)
            assert created["structuredContent"]["created"]
            assert replayed["structuredContent"]["replayed"]
            assert len(driver.added) == 1

    asyncio.run(run())


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_conversation(monkeypatch, verbose):
    fixture, identities = {}, []
    stage = {"number": 0, "step": 0}
    driver = Rpc()

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
                                        "goals": ["Download one Lantern result"],
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
                (
                    "search_indexer",
                    {"workflow_id": flow, "keyword": "Lantern", "sources": True},
                ),
            ],
            [
                *unlock,
                (
                    "read_conversation_history",
                    {"tool_call_id": fixture.get("search_call_id", ""), "limit": 8000},
                ),
                ("validate_download_task", fixture.get("check", {})),
            ],
            [
                *unlock,
                (
                    "create_download_task",
                    {
                        "request_id": identities[0] if identities else "",
                        "confirmed": True,
                    },
                ),
                (
                    "create_download_task",
                    {
                        "request_id": identities[0] if identities else "",
                        "confirmed": True,
                    },
                ),
            ],
        ][number]
        if step >= len(actions):
            content = [
                "Choose the published indexer for Lantern.",
                "Results: 1) Lantern 2024 1080p  2) Lantern 2024 720p. Which?",
                "Lantern 2024 720p (torrent), default downloader and dir. Confirm?",
                "Submitted task; downloading continues in the background.",
            ][number]
            return response([chunk({"role": "assistant", "content": content}, "stop")])
        name, args = actions[step]
        if name == "validate_download_task":
            # the chosen source must be recoverable from model-visible history
            assert (
                fixture["check"]["resource"]["source"]
                in dumps(body["messages"]).decode()
            )
        if step >= 2 and number:
            names = {item["function"]["name"] for item in body["tools"]}
            assert {"validate_download_task", "create_download_task"} <= names
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

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(site)) as client,
        ):
            service.Sanic.get_app().ctx.httpx = client
            graph, downloader, _ = await fixture_plan()
            fixture["graph_id"] = graph.id
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            results = []
            for number, prompt in enumerate(
                [
                    "下载一次 Lantern",
                    "选择 Published indexer",
                    "选第二个，其他用默认",
                    "确认下载",
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
                if number == 1:
                    fixture["search_call_id"] = turn[-1]["id"]
                    found = {i["id"]: i for i in turn[-1]["data"]["items"]}
                    options = turn[1]["data"]["defaults"]
                    fixture["check"] = settings(
                        graph.id,
                        found["r2"],
                        options["downloader_id"],
                        dir=options["dir"],
                    )
                    fixture["check"]["dir"] = options["dir"]
                if number == 2:
                    identities.append(turn[-1]["data"]["request_id"])
                if number < 3:
                    assert not driver.added and not await DownloadTask.exists()

            created = [
                r["data"] for r in results if r["name"] == "create_download_task"
            ]
            assert created[0]["created"] and created[1]["replayed"]
            assert created[0]["task_id"] == created[1]["task_id"]
            assert execute.await_count == 1
            assert len(driver.added) == 1
            assert not await DownloadPlan.exists()
            assert await FlowLog.all().count() == 0
            assert "PASSKEY" not in dumps(results).decode()
            checked = await AssistantTurn.get(request_id="step-2")
            assert graph.id in checked.resource_ids
            owner.role, owner.perms = UserRole.USER, Permissions(indexer_ids=[])
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": checked.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="read",
            )
            assert denied["error"]["code"] == "permission_denied"

    asyncio.run(run())


def test_same_turn_confirmation(monkeypatch):
    driver = Rpc()
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            turn = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="same",
                status="running",
                prompt="Download now",
                model={"model_id": "m"},
            )
            fields = settings(graph.id, items["r1"], downloader.id)
            check = await tools.DownloadTaskService.validate_request(
                DownloadTaskCheck.model_validate(fields), owner
            )
            from app.models.assistant import AssistantToolCall

            await AssistantToolCall.create(
                turn=turn,
                call_id="v",
                name="validate_download_task",
                arguments=dumps(fields).decode(),
                status="complete",
                result={"ok": True, "data": check},
            )
            await AssistantToolCall.create(
                turn=turn, call_id="c", name="create_download_task", arguments="{}"
            )

            result = await tools.create_task(
                tools.TaskCreate(request_id=check["request_id"], confirmed=True),
                conversation["id"],
                "c",
                owner,
            )

            # approval must come from a later turn after the user saw the review
            assert result["confirmation_required"] and not result["created"]
            assert not driver.added and not await DownloadTask.exists()
            assert not await FlowTrigger.exists()

    asyncio.run(run())


@pytest.mark.parametrize("changed", [False, True], ids=["same", "conflict"])
def test_worker_race(monkeypatch, changed):
    driver = Rpc()
    _, client = setup(monkeypatch, driver)
    # separate workers do not share the in-process locks
    monkeypatch.setattr(
        service, "_REQUEST_LOCKS", SimpleNamespace(setdefault=lambda _, lock: lock)
    )

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            body = DownloadTaskCreate(
                request_id=uuid4(),
                confirmed=True,
                info_hash=HASH,
                **settings(graph.id, items["r1"], downloader.id),
            )
            entered, second_entered, release = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )
            original = driver.add

            async def delayed(request):
                """Hold the simulated remote submission until the racing call ends.

                Args:
                    request: Normalized request sent to the fake downloader.

                Returns:
                    Fake submission snapshot.
                """
                snapshot = await original(request)
                (entered if len(driver.added) == 1 else second_entered).set()
                await release.wait()
                return snapshot

            monkeypatch.setattr(driver, "add", delayed)
            first = asyncio.create_task(
                service.DownloadTaskService.create_once(body, owner)
            )
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(
                service.DownloadTaskService.create_once(
                    body.model_copy(update={"dir": "/changed"}) if changed else body,
                    owner,
                )
            )
            second_call = asyncio.create_task(second_entered.wait())
            try:
                await asyncio.wait_for(
                    asyncio.wait(
                        {second, second_call}, return_when=asyncio.FIRST_COMPLETED
                    ),
                    2,
                )
            finally:
                release.set()
                second_call.cancel()
            results = await asyncio.gather(first, second)

            assert len(driver.added) == 1
            assert results[0]["created"]
            assert results[1].get("conflict", False) is changed
            assert await DownloadTask.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["cancel", "storage", "response"])
def test_interrupted_submission(monkeypatch, failure):
    error = (
        asyncio.CancelledError()
        if failure == "cancel"
        else (
            ValueError("Malformed downloader response")
            if failure == "response"
            else None
        )
    )
    driver = Rpc(error=error)
    _, client = setup(monkeypatch, driver)

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            body = DownloadTaskCreate(
                request_id=uuid4(),
                confirmed=True,
                info_hash=HASH,
                **settings(graph.id, items["r1"], downloader.id),
            )
            if failure == "storage":
                monkeypatch.setattr(
                    service.DownloadDirService,
                    "upsert",
                    AsyncMock(side_effect=RuntimeError("storage unavailable")),
                )
            with suppress(asyncio.CancelledError, RuntimeError, ValueError):
                await service.DownloadTaskService.create_once(body, owner)
            monkeypatch.setattr(service.DownloadDirService, "upsert", AsyncMock())
            retry = await service.DownloadTaskService.create_once(body, owner)

            assert len(driver.added) == 1
            assert retry["replayed"] and retry["uncertain"]
            assert await DownloadTask.all().count() == 1

    asyncio.run(run())


def test_changed_torrent(monkeypatch):
    driver = Rpc()
    execute = engine_fixture(monkeypatch, RESULTS)
    content = [TORRENT]
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=content[0],
                headers={"content-type": "application/x-bittorrent"},
            )
        )
    )

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            fields = settings(graph.id, items["r2"], downloader.id)
            check = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck(**fields), owner
            )
            content[0] = TORRENT.replace(b"movie.mkv", b"other.mkv")
            result = await service.DownloadTaskService.create_once(
                DownloadTaskCreate(
                    request_id=check["request_id"],
                    confirmed=True,
                    info_hash=check["download"]["info_hash"],
                    **fields,
                ),
                owner,
            )

            assert not result["created"]
            assert not driver.added and not await DownloadTask.exists()
            assert execute.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("revoked", ["indexer", "library", "ingestion"])
def test_replay_history(monkeypatch, tmp_path, revoked):
    driver = Rpc()
    _, client = setup(monkeypatch, driver)
    fixture, requests = {}, []

    def handler(upstream):
        requests.append(loads(upstream.content))
        step = len(requests)
        if step <= 2:
            name, args = (
                ("get_workflow", {"workflow_id": fixture["workflow_id"]})
                if step == 1
                else (
                    "create_download_task",
                    {"request_id": fixture["request_id"], "confirmed": True},
                )
            )
            return response(
                [
                    chunk(
                        {
                            "role": "assistant",
                            "tool_calls": [
                                call_delta(
                                    name, dumps(args).decode(), id=f"call-{step}"
                                )
                            ],
                        },
                        "tool_calls",
                    )
                ]
            )
        return response(
            [chunk({"role": "assistant", "content": "Recovered private task."}, "stop")]
        )

    configure(monkeypatch, handler)

    async def run():
        async with database() as (owner, _), client:
            data = await libraries(tmp_path)
            items = await searched(owner, data.graph.id, monkeypatch, driver, client)
            fields = settings(
                data.graph.id,
                items["r1"],
                data.downloader.id,
                transfer_lib_id=data.movies.id,
                transfer_method=TransferMethod.COPY,
            )
            check = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck(**fields, ingest_workflow_id=data.movie.id), owner
            )
            body = DownloadTaskCreate(
                request_id=check["request_id"],
                info_hash=check["download"]["info_hash"],
                confirmed=True,
                **fields,
                ingestion={
                    "workflow_id": data.movie.id,
                    "revision": check["ingestion"]["revision"],
                },
            )
            await service.DownloadTaskService.create_once(body, owner)
            replay = await service.DownloadTaskService.create_once(body, owner)
            assert replay["replayed"]
            # isolate the replay turn's provenance from any earlier validation history
            monkeypatch.setattr(tools, "create_task", AsyncMock(return_value=replay))
            fixture.update(workflow_id=data.graph.id, request_id=check["request_id"])
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            events = [
                event
                async for event in AssistantService.persistent_stream(
                    conversation["id"], request("replay"), owner
                )
            ]
            assert events[-1].event == "done", events[-1]
            turn = await AssistantTurn.get(request_id="replay")
            assert {data.graph.id, data.movie.id, f"media_lib:{data.movies.id}"} <= set(
                turn.resource_ids
            )
            await AssistantConversation.filter(id=conversation["id"]).update(
                summary={"goals": ["private recovered task"]},
                summary_resources=turn.resource_ids,
                summary_through=turn.id,
            )
            owner.role = UserRole.USER
            owner.perms = Permissions(
                indexer_ids=[
                    row.id
                    for row in (data.graph, data.movie, data.tv, data.draft)
                    if row.id
                    != {"indexer": data.graph.id, "ingestion": data.movie.id}.get(
                        revoked
                    )
                ],
                media_lib_ids=[] if revoked == "library" else [data.movies.id],
            )
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": turn.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="read",
            )
            assert denied["error"]["code"] == "permission_denied"
            record = (await Sessions.turns(conversation["id"], owner))["items"][0]
            assert record["content"] == "" and record["tools"] == []
            current = await Sessions.begin(
                conversation["id"], request("after-revoke"), owner
            )
            create = AsyncMock()
            messages, _, _ = await history.prepare_context(
                completion_client(create), current, owner, "system", [], None
            )
            assert "private" not in dumps(messages).decode()
            assert (await Sessions.owned(conversation["id"], owner)).summary == {}
            create.assert_not_awaited()
            with pytest.raises(ForbiddenException):
                await service.DownloadTaskService.create_once(body, owner)
            assert len(driver.added) == 1

    asyncio.run(run())


def test_long_result_id(monkeypatch):
    driver = Rpc()
    _, client = setup(monkeypatch, driver)
    result = {**RESULTS["items"][0], "id": "r" * 256}
    engine_fixture(monkeypatch, {"items": [result], "total": 1})

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            check = await service.DownloadTaskService.validate_request(
                DownloadTaskCheck(
                    **settings(graph.id, items[result["id"]], downloader.id)
                ),
                owner,
            )
            assert check["valid"]
            assert check["download"]["resource_id"] == result["id"]
            assert not driver.added

    asyncio.run(run())


@pytest.mark.parametrize("error", [ValueError("bad torrent"), asyncio.CancelledError()])
def test_validation_cleanup(monkeypatch, error):
    driver = Rpc()
    _, client = setup(monkeypatch, driver)
    close = AsyncMock()
    monkeypatch.setattr(driver, "close", close)
    monkeypatch.setattr(service, "standardize_magnet", AsyncMock(side_effect=error))

    async def run():
        async with database() as (owner, _), client:
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            with pytest.raises(type(error)):
                await service.DownloadTaskService.validate_request(
                    DownloadTaskCheck(**settings(graph.id, items["r2"], downloader.id)),
                    owner,
                )
            close.assert_awaited_once()
            assert not driver.added and not await DownloadTask.exists()

    asyncio.run(run())


def test_multistep_failure(monkeypatch):
    submitted = []

    def remote(request):
        if request.url.path == "/add":
            submitted.append(request.content)
            return httpx.Response(200, json={"ok": True})
        raise httpx.ConnectError("follow-up unavailable")

    driver = RpcDriver(
        RpcConfig.model_validate(
            {
                "name": "Fixture",
                "host": "downloader.test",
                "port": 443,
                "ssl": True,
                "methods": {
                    "add_link": {"steps": [{"post": "/add"}, {"get": "/receipt"}]}
                },
            }
        )
    )
    engine_fixture(monkeypatch, RESULTS)

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client,
        ):
            graph, downloader, _ = await fixture_plan()
            items = await searched(owner, graph.id, monkeypatch, driver, client)
            body = DownloadTaskCreate(
                request_id=uuid4(),
                confirmed=True,
                info_hash=HASH,
                **settings(graph.id, items["r1"], downloader.id),
            )
            first = await service.DownloadTaskService.create_once(body, owner)
            retry = await service.DownloadTaskService.create_once(body, owner)
            assert len(submitted) == 1
            assert first["uncertain"] and retry["replayed"]
            task = await DownloadTask.all().get()
            assert task.state == DownloadState.SUBMIT_UNKNOWN

    asyncio.run(run())
