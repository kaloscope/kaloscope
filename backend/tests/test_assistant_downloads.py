"""Unit tests for read-only assistant download task queries."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import httpx
import pytest
from tortoise import Tortoise

from app.core.ai import tools
from app.core.config import KaloscopeConfig
from app.models.download import (
    Downloader,
    DownloadTask,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)
from app.models.user import Permissions, UserInfo, UserRole

ADMIN = cast(UserInfo, SimpleNamespace(id=1, role=UserRole.ADMIN))
READER = cast(UserInfo, SimpleNamespace(id=2, role=UserRole.USER, perms=Permissions()))
CONFIG = """driver: rpc
name: {name}
protocol: http
host: localhost
port: 9000
auth:
  password: private-downloader-password
methods:
  list:
    get: /tasks
"""
LONG_ERROR = "Tracker rejected the request. " * 40


@pytest.fixture(autouse=True)
def network(monkeypatch):
    monkeypatch.setattr(
        KaloscopeConfig, "_config", SimpleNamespace(secret_key_enabled=False)
    )
    trap = AsyncMock(side_effect=AssertionError("Reads must not poll downloaders"))
    monkeypatch.setattr(httpx.AsyncClient, "send", trap)
    return trap


@asynccontextmanager
async def downloads():
    """Create two downloaders with active, intermediate, failed and completed tasks.

    Yields:
        The downloaders and tasks used by the queries.
    """
    await Tortoise.init(db_url="sqlite://:memory:", modules={"models": ["app.models"]})
    try:
        await Tortoise.generate_schemas()
        alpha = await Downloader.create(
            name="Alpha", config=CONFIG.format(name="Alpha"), priority=1
        )
        beta = await Downloader.create(
            name="Beta", config=CONFIG.format(name="Beta"), priority=2
        )
        rows = [
            (alpha, "Ubuntu ISO", "downloading", "active", 25.0, None),
            # intermediate phases can report full progress before completion
            (alpha, "Archive pull", "pulling", "running", 100.0, None),
            (beta, "Unconfirmed submit", "submit_unknown", None, None, None),
            (beta, "Broken tracker", "error", "error", 10.0, LONG_ERROR),
            (alpha, "Paused show", "paused", "paused", 40.0, "Paused by user"),
            (alpha, "Finished movie", "completed", "seeding", 100.0, None),
            (beta, "Finished album", "completed", "complete", 100.0, None),
        ]
        tasks = {}
        for downloader, name, state, raw_state, percentage, error in rows:
            tasks[name] = await DownloadTask.create(
                downloader=downloader,
                name=name,
                dir="/private/downloads",
                state=state,
                raw_state=raw_state,
                percentage=percentage,
                error_msg=error,
                total_size=1000 if percentage is not None else None,
                completed_size=(
                    int(percentage * 10) if percentage is not None else None
                ),
                dl_speed=2048 if state == "downloading" else None,
                magnet_link="magnet:?xt=urn:btih:private-source-token",
                files=["private-file"],
            )
        await OfflineDownloadJob.create(
            download=tasks["Broken tracker"],
            job_uuid="fixture",
            source_fingerprint="fixture",
            remote_dir="/remote/fixture",
            last_error_kind=OfflineDownloadErrorKind.REMOTE_FAILED,
        )
        yield SimpleNamespace(alpha=alpha, beta=beta, tasks=tasks)
    finally:
        await Tortoise.close_connections()


def tasks(result):
    """Read the compact task summaries from a list result.

    Args:
        result: Successful `list_download_tasks` result.

    Returns:
        One summary per returned task.
    """
    return result["data"]["items"]


async def call(arguments=None, user=ADMIN, name="list_download_tasks"):
    """Execute one assistant tool with JSON arguments.

    Args:
        arguments: Tool arguments; omitted for none.
        user: Account executing the tool.
        name: Registered tool name.

    Returns:
        The sanitized tool result.
    """
    return await tools.execute_tool(
        name, json.dumps(arguments or {}), user, conversation_id=1, call_id="test"
    )


@pytest.mark.parametrize(
    ("scope", "total"), [("all", 7), ("active", 5), ("completed", 2)]
)
def test_scopes(scope, total):
    async def run():
        async with downloads():
            result = await call({"scope": scope})

            data = result["data"]
            assert data["scope"] == scope
            assert data["total"] == total
            assert data["page_counts"] == {"returned": total}
            assert data["next_page_num"] is None
            states = {item["state"] for item in tasks(result)}
            assert ("completed" in states) == (scope != "active")
            assert states == {"completed"} or scope != "completed"

    asyncio.run(run())


def test_filters():
    async def run():
        async with downloads() as data:
            named = await call({"name": "FINISHED"})
            failed = await call({"scope": "active", "state": "error"})
            contradictory = await call({"scope": "active", "state": "completed"})
            beta = await call({"downloader_id": data.beta.id, "ordering": "name"})
            descending = await call({"scope": "completed", "ordering": "-name"})

            assert {item["name"] for item in tasks(named)} == {
                "Finished movie",
                "Finished album",
            }
            assert [item["name"] for item in tasks(failed)] == ["Broken tracker"]
            assert contradictory["data"]["total"] == 0
            assert [item["name"] for item in tasks(beta)] == [
                "Broken tracker",
                "Finished album",
                "Unconfirmed submit",
            ]
            assert beta["data"]["downloaders"] == [{"id": data.beta.id, "name": "Beta"}]
            assert [item["name"] for item in tasks(descending)] == [
                "Finished movie",
                "Finished album",
            ]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("page_num", "returned", "next_page"), [(1, 2, 2), (3, 1, None), (4, 0, None)]
)
def test_pages(page_num, returned, next_page):
    async def run():
        async with downloads():
            result = await call(
                {"scope": "active", "page_num": page_num, "page_size": 2}
            )

            assert result["data"]["total"] == 5
            assert result["data"]["page_counts"] == {"returned": returned}
            assert result["data"]["next_page_num"] == next_page

    asyncio.run(run())


def test_empty():
    async def run():
        async with downloads():
            result = await call({"name": "no such task"})

            assert result["ok"] is True
            assert result["data"]["total"] == 0
            assert result["data"]["items"] == []
            assert result["data"]["downloaders"] == []

    asyncio.run(run())


def test_stored_status():
    async def run():
        async with downloads() as fixture:
            result = await call({"page_size": 25})

            items = {item["name"]: item for item in tasks(result)}
            pulling = items["Archive pull"]
            assert (pulling["state"], pulling["percentage"]) == ("pulling", 100.0)
            unknown = items["Unconfirmed submit"]
            assert unknown["state"] == "submit_unknown"
            # unknown values stay null, never zero
            assert unknown["percentage"] is None
            detail = await call(
                {"task_id": fixture.tasks["Archive pull"].id}, name="get_download_task"
            )
            assert detail["data"]["raw_state"] == "running"
            assert detail["data"]["completed_at"] is None
            detail = await call(
                {"task_id": fixture.tasks["Ubuntu ISO"].id}, name="get_download_task"
            )
            assert detail["data"]["dl_speed"] == 2048

    asyncio.run(run())


def test_errors():
    async def run():
        async with downloads() as fixture:
            result = await call({"scope": "active", "page_size": 25})

            items = {item["name"]: item for item in tasks(result)}
            broken = items["Broken tracker"]
            assert broken["error_length"] == len(LONG_ERROR)
            assert items["Paused show"]["error_length"] == len("Paused by user")
            detail = await call(
                {"task_id": fixture.tasks["Broken tracker"].id},
                name="get_download_task",
            )
            assert detail["data"]["error_kind"] == "remote_failed"
            assert detail["data"]["error"]["data"] == LONG_ERROR

    asyncio.run(run())


def test_error_pages(network):
    async def run():
        async with downloads() as fixture:
            task = fixture.tasks["Broken tracker"]
            error = LONG_ERROR * 3 + "Final cause: destination directory is missing."
            await DownloadTask.filter(id=task.id).update(error_msg=error)
            before = await DownloadTask.get(id=task.id).values()
            restored = ""
            offset = 0

            while offset is not None:
                result = await call(
                    {"task_id": task.id, "offset": offset}, READER, "get_download_task"
                )
                assert result["ok"] is True
                data = result["data"]
                assert data["id"] == task.id
                assert data["raw_state"] == "error"
                assert data["error_kind"] == "remote_failed"
                assert data["error"]["total"] == len(error)
                assert len(data["error"]["data"]) <= 2000
                restored += data["error"]["data"]
                offset = data["error"]["next_offset"]

            assert restored == error
            assert await DownloadTask.get(id=task.id).values() == before
            assert (
                not {"dir", "magnet_link", "files", "config", "capabilities"}
                & data.keys()
            )
            network.assert_not_awaited()

    asyncio.run(run())


def test_task_details():
    async def run():
        async with downloads() as fixture:
            task = fixture.tasks["Unconfirmed submit"]

            result = await call({"task_id": task.id}, name="get_download_task")
            missing = await call({"task_id": 9999}, name="get_download_task")

            assert result["data"]["state"] == "submit_unknown"
            assert result["data"]["percentage"] is None
            assert result["data"]["error"] is None
            assert missing["error"]["code"] == "not_found"

    asyncio.run(run())


def test_shared_visibility():
    async def run():
        async with downloads():
            admin = await call({"page_size": 25})
            reader = await call({"page_size": 25}, READER)

            assert reader["ok"] is True
            assert reader["data"] == admin["data"]

    asyncio.run(run())


def test_read_only(network):
    async def run():
        async with downloads():
            before = await DownloadTask.all().order_by("id").values()

            result = await call({"page_size": 25})

            assert await DownloadTask.all().order_by("id").values() == before
            encoded = json.dumps(result)
            for private in ("private-downloader-password", "private-source-token"):
                assert private not in encoded
            assert (
                not {
                    "dir",
                    "magnet_link",
                    "files",
                    "config",
                    "capabilities",
                }
                & tasks(result)[0].keys()
            )
            network.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize(
    "arguments",
    [
        {"scope": "running"},
        {"state": "unknown"},
        {"ordering": "magnet_link"},
        {"page_size": 26},
        {"page_num": 0},
        {"downloader_id": 0},
        {"name": "x" * 256},
        {"task_ids": [1]},
    ],
)
def test_invalid(arguments):
    result = asyncio.run(call(arguments))

    assert result["error"]["code"] == "bad_request"


def test_navigation():
    async def run():
        active = await call({"page": "active_downloads"}, name="navigate")
        completed = await call({"page": "completed_downloads"}, READER, "navigate")
        task = await call(
            {"page": "completed_downloads", "resource_id": 3}, name="navigate"
        )

        assert active["data"]["path"] == "/downloads/downloading"
        assert completed["data"]["path"] == "/downloads/completed"
        assert completed["data"]["library_id"] is None
        assert task["error"]["code"] == "bad_request"

    asyncio.run(run())
