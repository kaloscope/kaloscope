"""Unit tests for reviewed download task pauses, resumes, retries and deletions."""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qsl

import httpx
import pytest
from pydantic import ValidationError
from sanic import Sanic

from app.core.ai import history, tools
from app.core.ai.documents import tool_facts
from app.core.dl import syncer
from app.core.dl.driver import DownloadState
from app.core.dl.openlist import puller
from app.core.dl.openlist.manifest import RemoteManifestEntry, serialize_manifest
from app.core.exceptions import ForbiddenException, NotFoundException
from app.models.assistant import AssistantToolCall, AssistantTurn, ConversationCreate
from app.models.download import (
    Downloader,
    DownloadPlan,
    DownloadPlanHistory,
    DownloadTask,
    DownloadTaskAction,
    DownloadTaskActionApply,
    DownloadTaskReceipt,
    OfflineDownloadErrorKind,
    OfflineDownloadJob,
)
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import MediaLib
from app.models.user import Permissions, UserRole
from app.services import download as service
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_changes import latest_request, lost
from tests.test_mcp import application, call
from tests.test_services_assistant import configure
from tests.test_workflow_runs import converse, names, scripted

RPC = """driver: rpc
name: Seedbox
protocol: http
host: rpc.test
port: 9000
auth:
  username: admin
  password: private-downloader-password
methods:
  pause:
    post: /pause
    form:
      hashes: '{{hash}}'
  start:
    post: /start
    form:
      hashes: '{{hash}}'
  delete:
    post: /delete
    form:
      hashes: '{{hash}}'
      deleteFiles: '{{local}}'
"""
OPENLIST = """driver: openlist
name: Cloud
protocol: https
host: openlist.test
port: 443
path: /api
auth:
  token: private-openlist-token
tool: Fixture Tool
"""
PRIVATE = (
    "private-downloader-password",
    "private-openlist-token",
    "private-source-token",
)
ACTIVE = {
    "page_type": "active_downloads",
    "route": "/downloads/downloading",
    "filters": {
        "name": "",
        "downloader_id": None,
        "ordering": "",
        "page_num": 1,
        "page_size": 50,
    },
}


class Remote:
    """Answer RPC controls like a reachable downloader and record each request."""

    def __init__(self):
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.failure: type[httpx.RequestError] | int | None = None
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record one control and answer it.

        Args:
            request: RPC request sent by the real driver.

        Returns:
            Success or the configured rejection.
        """
        self.requests.append(
            (request.url.path, dict(parse_qsl(request.content.decode())))
        )
        if self.entered and self.release:
            self.entered.set()
            await self.release.wait()
        if isinstance(self.failure, int):
            return httpx.Response(self.failure, text="Rejected.")
        if self.failure is not None:
            raise self.failure("simulated", request=request)
        return httpx.Response(200, text="Ok.")


def synchronizer():
    """Build a synchronizer owner whose shared state stays in this process.

    Returns:
        Synchronizer that publishes and consumes real task commands.
    """
    runner = cast(Any, object.__new__(syncer.DLSyncer))
    runner._app = SimpleNamespace(
        shared_ctx=SimpleNamespace(
            dl_task_actions={}, dl_sync_requested=threading.Event()
        )
    )
    runner._drivers = {}
    return runner


async def store():
    """Store tasks whose IDs differ from downloader and library IDs.

    Returns:
        RPC and OpenList downloaders, a transfer library and tasks by name.
    """
    seedbox = await Downloader.create(id=3, name="Seedbox", config=RPC, priority=7)
    cloud = await Downloader.create(id=4, name="Cloud", config=OPENLIST, priority=8)
    library = await MediaLib.create(
        id=60, name="Movies", lib_type="movie", dir="/synthetic/lib", priority=1
    )
    rows = {
        "Ubuntu 24.04": (21, seedbox, DownloadState.DOWNLOADING, None),
        "Ubuntu 22.04": (22, seedbox, DownloadState.PAUSED, None),
        "Finished movie": (23, seedbox, DownloadState.COMPLETED, library),
        "Cloud show": (24, cloud, DownloadState.PULLING, None),
        "Cloud failure": (25, cloud, DownloadState.ERROR, None),
        "Cloud paused": (26, cloud, DownloadState.PAUSED, None),
        "Cloud submission": (27, cloud, DownloadState.SUBMITTING, None),
    }
    tasks = {}
    for name, (task_id, downloader, state, transfer) in rows.items():
        tasks[name] = await DownloadTask.create(
            id=task_id,
            downloader=downloader,
            name=name,
            dir="/synthetic/downloads",
            state=state,
            unique_id=f"remote-{task_id}",
            info_hash=f"{task_id:040x}",
            percentage=40.0,
            dl_speed=2048,
            transfer_lib=transfer,
            transfer_method="hardlink" if transfer else None,
            magnet_link="magnet:?xt=urn:btih:private-source-token",
        )
        if downloader is cloud:
            await OfflineDownloadJob.create(
                download=tasks[name],
                job_uuid=f"{task_id:032x}",
                source_fingerprint=f"{task_id}" * 32,
                remote_dir=f"/Kaloscope/{task_id:032x}",
                last_error_kind=OfflineDownloadErrorKind.PULL_FAILED
                if state is DownloadState.ERROR
                else None,
            )
    return SimpleNamespace(seedbox=seedbox, cloud=cloud, library=library, tasks=tasks)


@asynccontextmanager
async def downloads(monkeypatch):
    """Open tasks behind a recording downloader and synchronizer.

    Args:
        monkeypatch: Pytest patch helper.

    Yields:
        Accounts, the downloader, the synchronizer, notifications and tasks.
    """
    remote = Remote()
    runner = synchronizer()
    async with httpx.AsyncClient(transport=httpx.MockTransport(remote)) as client:
        app = SimpleNamespace(
            config={}, ctx=SimpleNamespace(httpx=client, dl_syncer=runner)
        )
        monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
        async with database() as (owner, other):
            reader = SimpleNamespace(
                id=other.id, role=UserRole.USER, perms=Permissions()
            )
            yield SimpleNamespace(
                owner=owner,
                reader=reader,
                remote=remote,
                syncer=runner,
                notify=service.notify_download_changes,
                **vars(await store()),
            )


async def review(user, **change):
    """Review one action through the shared service.

    Args:
        user: Calling account.
        **change: Action fields.

    Returns:
        Shared review.
    """
    return await service.DownloadTaskService.review_action(
        DownloadTaskAction.model_validate(change), user
    )


async def apply(user, reviewed, *, confirmed=True, **change):
    """Apply a review through the shared service.

    Args:
        user: Calling account.
        reviewed: Review whose request ID and revision are confirmed.
        confirmed: The user's approval.
        **change: The reviewed action, possibly altered by the caller.

    Returns:
        Shared outcome.
    """
    return await service.DownloadTaskService.apply_action(
        DownloadTaskActionApply(
            request_id=reviewed["request_id"],
            change=change,
            revision=reviewed["revision"],
            confirmed=confirmed,
        ),
        user,
    )


async def act(user, **change):
    """Review an action and apply it at once with confirmation.

    Args:
        user: Calling account.
        **change: Action fields.

    Returns:
        Review and outcome.
    """
    reviewed = await review(user, **change)
    assert reviewed["valid"], reviewed
    return reviewed, await apply(user, reviewed, **change)


def key_of(user, reviewed):
    """Derive the account-scoped receipt key of a reviewed request.

    Args:
        user: Reviewing account.
        reviewed: Review with its request ID.

    Returns:
        Receipt key.
    """
    return hashlib.sha256(f"{user.id}:{reviewed['request_id']}".encode()).hexdigest()


async def state(task):
    """Read a task's stored state, or `None` once deleted.

    Args:
        task: Stored task.

    Returns:
        Current state.
    """
    current = await DownloadTask.get_or_none(id=task.id)
    return current.state if current else None


def test_listing(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            listed = await tools.execute_tool(
                "list_download_tasks",
                dumps({"name": "Ubuntu"}).decode(),
                env.owner,
                conversation_id=1,
                call_id="list",
            )
            shared = await tools.execute_tool(
                "list_download_tasks",
                dumps({"name": "Ubuntu"}).decode(),
                env.reader,
                conversation_id=1,
                call_id="list",
            )
            details = await tools.execute_tool(
                "get_download_task",
                dumps({"task_id": 25}).decode(),
                env.reader,
                conversation_id=1,
                call_id="details",
            )

            items = {item["id"]: item for item in listed["data"]["items"]}
            # similar names stay distinct by ID, downloader, state and actions
            assert {
                (item["name"], item["downloader_id"], item["state"])
                for item in items.values()
            } == {
                ("Ubuntu 24.04", 3, "downloading"),
                ("Ubuntu 22.04", 3, "paused"),
            }
            assert items[21]["actions"] == ["pause", "delete"]
            assert items[22]["actions"] == ["resume", "delete"]
            # deletion stays administrator-only; other actions stay shared
            assert [item["actions"] for item in shared["data"]["items"]] == [
                [action for action in item["actions"] if action != "delete"]
                for item in listed["data"]["items"]
            ]
            assert details["data"]["actions"] == ["retry"]
            assert listed["data"]["downloaders"] == [{"id": 3, "name": "Seedbox"}]
            for private in PRIVATE:
                assert private not in dumps([listed, details]).decode()
            assert env.remote.requests == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("name", "change", "dispatch", "note"),
    [
        ("Ubuntu 24.04", {"action": "pause"}, "immediate", "pause it"),
        ("Ubuntu 22.04", {"action": "resume"}, "immediate", "resume it"),
        ("Cloud show", {"action": "pause"}, "deferred", "remote copy is kept"),
        ("Cloud paused", {"action": "resume"}, "deferred", "Continues pulling"),
        ("Cloud failure", {"action": "retry"}, "deferred", "nothing new is submitted"),
        ("Finished movie", {"action": "delete"}, "immediate", "Downloaded files stay."),
        (
            "Finished movie",
            {"action": "delete", "delete_files": True},
            "immediate",
            "symlinks to deleted files break",
        ),
        (
            "Cloud submission",
            {"action": "delete"},
            "deferred",
            "submission in progress",
        ),
    ],
)
def test_review(monkeypatch, name, change, dispatch, note):
    async def run():
        async with downloads(monkeypatch) as env:
            task = env.tasks[name]
            before = await DownloadTask.all().order_by("id").values()

            reviewed = await review(env.owner, task_id=task.id, **change)

            assert reviewed["valid"] and reviewed["request_id"]
            assert (reviewed["task_id"], reviewed["name"]) == (task.id, name)
            assert reviewed["dispatch"] == dispatch
            assert reviewed["action"] in reviewed["actions"]
            assert any(note in item for item in reviewed["notes"])
            assert reviewed["delete_files"] is change.get("delete_files", False)
            if change["action"] == "delete":
                assert any(
                    "library records are unchanged" in n for n in reviewed["notes"]
                )
            if change["action"] == "retry":
                assert reviewed["retry_from"] == "pulling"
            if dispatch == "deferred":
                assert any("queues it" in item for item in reviewed["notes"])
            # a review sends nothing and changes nothing
            assert await DownloadTask.all().order_by("id").values() == before
            assert env.remote.requests == [] and env.syncer._task_actions == {}
            assert not await DownloadTaskReceipt.exists()
            for private in PRIVATE:
                assert private not in dumps(reviewed).decode()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("name", "action", "error_kind"),
    [
        ("Ubuntu 24.04", "resume", None),
        ("Ubuntu 22.04", "pause", None),
        # RPC downloaders have no retry; OpenList retries only failed jobs
        ("Ubuntu 24.04", "retry", None),
        ("Cloud show", "retry", None),
        ("Cloud failure", "retry", OfflineDownloadErrorKind.SUBMIT_UNKNOWN),
        ("Cloud submission", "pause", None),
        ("Finished movie", "pause", None),
    ],
)
def test_unavailable(monkeypatch, name, action, error_kind):
    async def run():
        async with downloads(monkeypatch) as env:
            task = env.tasks[name]
            if error_kind:
                await OfflineDownloadJob.filter(download_id=task.id).update(
                    last_error_kind=error_kind
                )

            reviewed = await review(env.owner, task_id=task.id, action=action)

            assert not reviewed["valid"] and "request_id" not in reviewed
            assert [issue["path"] for issue in reviewed["issues"]] == ["action"]
            assert action not in reviewed["actions"] and reviewed["notes"] == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"task_id": 21, "action": "pause", "delete_files": True}, "only to delete"),
        ({"task_id": 0, "action": "pause"}, None),
        ({"task_id": 21, "action": "cancel"}, None),
        ({"task_id": 21, "action": "pause", "ids": [21, 22]}, None),
    ],
)
def test_arguments(monkeypatch, arguments, message):
    async def run():
        async with downloads(monkeypatch) as env:
            return await tools.execute_tool(
                "review_download_task_action",
                dumps(arguments).decode(),
                env.owner,
                conversation_id=1,
                call_id="review",
            )

    result = asyncio.run(run())

    assert result["error"]["code"] == "bad_request"
    if message:
        assert message in result["error"]["guidance"]
    with pytest.raises(ValidationError):
        DownloadTaskAction.model_validate(arguments)


def test_missing(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            with pytest.raises(NotFoundException):
                await review(env.owner, task_id=99, action="pause")
            missing = await tools.execute_tool(
                "review_download_task_action",
                dumps({"task_id": 99, "action": "delete"}).decode(),
                env.owner,
                conversation_id=1,
                call_id="review",
            )

            assert missing["error"]["code"] == "not_found"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("name", "change", "path", "after"),
    [
        ("Ubuntu 24.04", {"action": "pause"}, "/pause", "paused"),
        ("Ubuntu 22.04", {"action": "resume"}, "/start", "downloading"),
        ("Finished movie", {"action": "delete"}, "/delete", None),
        ("Finished movie", {"action": "delete", "delete_files": True}, "/delete", None),
    ],
)
def test_immediate(monkeypatch, name, change, path, after):
    async def run():
        async with downloads(monkeypatch) as env:
            task = env.tasks[name]
            reviewed, applied = await act(env.owner, task_id=task.id, **change)
            notified = env.notify.call_count
            repeated = await apply(env.owner, reviewed, task_id=task.id, **change)
            receipt = await DownloadTaskReceipt.get()

            form = {"hashes": task.info_hash}
            if change["action"] == "delete":
                form["deleteFiles"] = str(change.get("delete_files", False)).lower()
            assert applied["applied"] and not applied["replayed"]
            assert applied["outcome"] == ("deleted" if after is None else "changed")
            assert env.remote.requests == [(path, form)]
            assert await state(task) == after
            # immediate controls notify once and publish nothing
            assert notified == 1 and env.syncer._task_actions == {}
            # the repeat reads the receipt instead of contacting the downloader
            assert repeated["replayed"] and repeated["outcome"] == applied["outcome"]
            assert len(env.remote.requests) == 1 and env.notify.call_count == 1
            assert receipt.result == {
                key: value for key, value in applied.items() if key != "replayed"
            }
            for private in PRIVATE:
                assert private not in dumps([applied, receipt.result]).decode()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("name", "change", "observed"),
    [
        ("Cloud show", {"action": "pause"}, "paused"),
        ("Cloud paused", {"action": "resume"}, "pulling"),
        ("Cloud failure", {"action": "retry"}, "pulling"),
        ("Cloud show", {"action": "delete"}, None),
        ("Cloud show", {"action": "delete", "delete_files": True}, None),
    ],
)
def test_deferred(monkeypatch, name, change, observed):
    async def run():
        async with downloads(monkeypatch) as env:
            task = env.tasks[name]
            reviewed, queued = await act(env.owner, task_id=task.id, **change)
            published = dict(env.syncer._task_actions)
            job = await OfflineDownloadJob.get(download_id=task.id)
            before = await state(task)
            notified = env.notify.call_count

            # synchronization applies the command later and is observed separately
            await env.syncer._consume_actions()
            repeated = await apply(env.owner, reviewed, task_id=task.id, **change)

            deleting = change["action"] == "delete"
            assert queued["applied"] and queued["outcome"] == "queued"
            assert "not completion" in queued["message"]
            assert before == task.state
            assert published == {
                task.id: (
                    change["action"],
                    task.state,
                    change.get("delete_files", False),
                )
            }
            # deletions keep their durable deadline; other commands only publish
            assert (job.delete_due_at is not None) is deleting
            assert job.delete_local is change.get("delete_files", False)
            assert notified == (1 if deleting else 0)
            assert env.remote.requests == []
            assert await state(task) == observed
            assert env.syncer._task_actions == {}
            assert repeated["replayed"] and repeated["outcome"] == "queued"
            assert (repeated["task"] or {}).get("state") == observed
            assert env.syncer._task_actions == {}

    asyncio.run(run())


def test_confirmation(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            values = {"task_id": 21, "action": "pause"}
            reviewed = await review(env.owner, **values)

            unconfirmed = await apply(env.owner, reviewed, confirmed=False, **values)

            assert unconfirmed["confirmation_required"] and not unconfirmed["applied"]
            assert unconfirmed["request_id"] == reviewed["request_id"]
            assert env.remote.requests == [] and not await DownloadTaskReceipt.exists()
            assert await state(env.tasks["Ubuntu 24.04"]) == "downloading"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("values", "stale"),
    [
        ({"percentage": 75.0, "dl_speed": 0, "completed_size": 9}, False),
        ({"raw_state": "stalledDL", "error_msg": "Tracker timeout"}, False),
        ({"state": DownloadState.PAUSED}, True),
        ({"transfer_pending": True}, True),
        ({"downloader_id": 4}, True),
        ({"unique_id": "different-remote-task"}, True),
        ({"name": "Another download"}, True),
        ({"dir": "/different/downloads"}, True),
        ({"transfer_method": "symlink"}, True),
    ],
)
def test_stale(monkeypatch, values, stale):
    async def run():
        async with downloads(monkeypatch) as env:
            change = {"task_id": 21, "action": "pause"}
            reviewed = await review(env.owner, **change)
            await DownloadTask.filter(id=21).update(**values)

            result = await apply(env.owner, reviewed, **change)

            # progress counters never invalidate a review; effects do
            assert bool(result.get("stale")) is stale
            if stale:
                assert not result["applied"] and env.remote.requests == []
                assert result["current"]["state"] == (
                    await state(env.tasks["Ubuntu 24.04"])
                )
                assert not await DownloadTaskReceipt.exists()
            else:
                assert result["outcome"] == "changed"

    asyncio.run(run())


def test_capabilities_changed(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            change = {"task_id": 21, "action": "pause"}
            reviewed = await review(env.owner, **change)
            # the downloader no longer offers pausing
            pause = "  pause:\n    post: /pause\n    form:\n      hashes: '{{hash}}'\n"
            assert pause in RPC
            await Downloader.filter(id=3).update(config=RPC.replace(pause, ""))

            result = await apply(env.owner, reviewed, **change)

            assert result["stale"] and result["current"]["actions"] == ["delete"]
            assert env.remote.requests == []

    asyncio.run(run())


def test_downloader_changed(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            change = {"task_id": 23, "action": "delete", "delete_files": True}
            reviewed = await review(env.owner, **change)
            await Downloader.filter(id=3).update(
                config=RPC.replace("rpc.test", "another-rpc.test")
            )

            result = await apply(env.owner, reviewed, **change)

            assert result["stale"] and not result["applied"]
            assert env.remote.requests == []
            assert await DownloadTask.exists(id=23)

    asyncio.run(run())


@pytest.mark.parametrize(
    "altered",
    [
        {"task_id": 22, "action": "resume"},
        {"task_id": 21, "action": "delete"},
        {"task_id": 23, "action": "delete", "delete_files": True},
    ],
)
def test_conflict(monkeypatch, altered):
    async def run():
        async with downloads(monkeypatch) as env:
            reviewed = await review(env.owner, task_id=23, action="delete")

            result = await apply(env.owner, reviewed, **altered)

            assert result["conflict"] and not result["applied"]
            assert env.remote.requests == [] and not await DownloadTaskReceipt.exists()
            assert None not in [await state(task) for task in env.tasks.values()]

    asyncio.run(run())


def test_concurrent(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            values = {"task_id": 21, "action": "pause"}
            reviewed = await review(env.owner, **values)

            results = await asyncio.gather(
                *(apply(env.owner, reviewed, **values) for _ in range(3))
            )

            assert sorted(result["replayed"] for result in results) == [
                False,
                True,
                True,
            ]
            assert all(result["outcome"] == "changed" for result in results)
            assert len(env.remote.requests) == 1
            assert await DownloadTaskReceipt.all().count() == 1

    asyncio.run(run())


def test_pending(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            values = {"task_id": 21, "action": "pause"}
            reviewed = await review(env.owner, **values)
            key = key_of(env.owner, reviewed)

            # another worker holds this request while it dispatches
            with syncer.submission_lock(f"task-action:{key}"):
                pending = await apply(env.owner, reviewed, **values)
                await DownloadTaskReceipt.create(key=key, action="pause", task_id=21)
                claimed = await apply(env.owner, reviewed, **values)
            # that worker stopped before recording what it sent
            restarted = await apply(env.owner, reviewed, **values)

            assert pending["pending"] and not pending["applied"]
            assert claimed["pending"] and not claimed["applied"]
            assert restarted["replayed"] and restarted["outcome"] == "uncertain"
            assert restarted["applied"] is None
            assert "only reads its recorded outcome" in restarted["message"]
            assert env.remote.requests == []
            assert restarted["task"]["state"] == "downloading"

    asyncio.run(run())


def test_cancelled(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            values = {"task_id": 23, "action": "delete"}
            reviewed = await review(env.owner, **values)
            env.remote.entered, env.remote.release = asyncio.Event(), asyncio.Event()

            caller = asyncio.create_task(apply(env.owner, reviewed, **values))
            await env.remote.entered.wait()
            caller.cancel()
            env.remote.release.set()
            with pytest.raises(asyncio.CancelledError):
                await caller
            # the lost response is read back instead of deleting again
            repeated = await apply(env.owner, reviewed, **values)

            assert repeated["replayed"] and repeated["outcome"] == "deleted"
            assert repeated["task"] is None and len(env.remote.requests) == 1
            assert await DownloadTaskReceipt.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    ("change", "failure", "first", "second", "sent"),
    [
        # refused or unreachable downloaders received nothing to repeat
        (
            {"task_id": 21, "action": "pause"},
            httpx.ConnectError,
            "failed",
            "changed",
            2,
        ),
        ({"task_id": 21, "action": "pause"}, 400, "failed", "changed", 2),
        (
            {"task_id": 23, "action": "delete"},
            httpx.ConnectError,
            "failed",
            "deleted",
            2,
        ),
        # a possibly completed action is never sent again
        ({"task_id": 21, "action": "pause"}, httpx.ReadTimeout, "uncertain", None, 1),
        ({"task_id": 21, "action": "pause"}, 502, "uncertain", None, 1),
        ({"task_id": 23, "action": "delete"}, httpx.ReadTimeout, "uncertain", None, 1),
        ({"task_id": 23, "action": "delete"}, 502, "uncertain", None, 1),
        (
            {"task_id": 23, "action": "delete", "delete_files": True},
            400,
            "failed",
            "deleted",
            2,
        ),
    ],
)
def test_remote_failure(monkeypatch, change, failure, first, second, sent):
    async def run():
        async with downloads(monkeypatch) as env:
            task = next(t for t in env.tasks.values() if t.id == change["task_id"])
            reviewed = await review(env.owner, **change)
            env.remote.failure = failure

            initial = await apply(env.owner, reviewed, **change)
            env.remote.failure = None
            repeated = await apply(env.owner, reviewed, **change)

            assert (initial.get("outcome") or "failed") == first
            if first == "failed":
                assert initial["failed"] and initial["applied"] is False
                assert repeated["outcome"] == second and not repeated["replayed"]
            elif first == "uncertain":
                assert initial["applied"] is None
                assert repeated["replayed"] and repeated["outcome"] == "uncertain"
                assert await state(task) == task.state
            else:
                assert repeated["replayed"] and repeated["outcome"] == second
            assert len(env.remote.requests) == sent

    asyncio.run(run())


def test_rollback(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            values = {"task_id": 24, "action": "pause"}
            reviewed = await review(env.owner, **values)
            create = DownloadTaskReceipt.create
            monkeypatch.setattr(
                DownloadTaskReceipt, "create", AsyncMock(side_effect=OSError("disk"))
            )
            unclaimed = await apply(env.owner, reviewed, **values)
            monkeypatch.setattr(DownloadTaskReceipt, "create", create)
            queued = await apply(env.owner, reviewed, **values)

            # nothing reached synchronization, so the same request may run once
            assert unclaimed["failed"]
            assert queued["outcome"] == "queued" and not queued["replayed"]
            assert list(env.syncer._task_actions) == [24]
            assert await DownloadTaskReceipt.all().count() == 1

    asyncio.run(run())


def test_unrecorded(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            values = {"task_id": 21, "action": "pause"}
            reviewed = await review(env.owner, **values)
            original = DownloadTaskReceipt.filter

            def failing(**fields):
                query = original(**fields)
                query.update = AsyncMock(side_effect=OSError("disk"))
                return query

            monkeypatch.setattr(DownloadTaskReceipt, "filter", failing)
            applied = await apply(env.owner, reviewed, **values)
            monkeypatch.setattr(DownloadTaskReceipt, "filter", original)
            repeated = await apply(env.owner, reviewed, **values)

            # the action happened; its unrecorded claim is never sent again
            assert applied["outcome"] == "changed"
            assert repeated["replayed"] and repeated["outcome"] == "uncertain"
            assert len(env.remote.requests) == 1

    asyncio.run(run())


@pytest.mark.parametrize("queued", [False, True])
def test_queue_notification_lost(monkeypatch, queued):
    async def run():
        async with downloads(monkeypatch) as env:
            change = {"task_id": 24, "action": "pause"}
            reviewed = await review(env.owner, **change)
            if queued:
                monkeypatch.setattr(
                    env.syncer._sync_requested, "set", Mock(side_effect=OSError("lost"))
                )
            else:
                monkeypatch.setattr(
                    env.syncer, "publish", Mock(side_effect=OSError("lost"))
                )

            result = await apply(env.owner, reviewed, **change)
            assert result["outcome"] == "uncertain" and result["applied"] is None
            assert (24 in env.syncer._task_actions) is queued
            await env.syncer._consume_actions()
            assert await state(env.tasks["Cloud show"]) == (
                "paused" if queued else "pulling"
            )
            await DownloadTask.filter(id=24).update(state=DownloadState.PULLING)
            replayed = await apply(env.owner, reviewed, **change)

            assert replayed["replayed"] and replayed["outcome"] == "uncertain"
            assert env.syncer._task_actions == {}
            assert await DownloadTaskReceipt.all().count() == 1

    asyncio.run(run())


def test_delete_notification_lost(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            change = {"task_id": 24, "action": "delete", "delete_files": True}
            reviewed = await review(env.owner, **change)
            monkeypatch.setattr(
                service, "notify_download_changes", Mock(side_effect=OSError("lost"))
            )

            result = await apply(env.owner, reviewed, **change)
            pending = await OfflineDownloadJob.get(download_id=24)

            assert pending.delete_due_at and pending.delete_local
            assert result["applied"] is None and result["outcome"] == "uncertain"
            await env.syncer._consume_actions()
            replayed = await apply(env.owner, reviewed, **change)
            assert replayed["replayed"] and replayed["task"] is None
            assert await DownloadTaskReceipt.all().count() == 1

    asyncio.run(run())


@pytest.mark.parametrize("delete_files", [False, True])
def test_owned_files(monkeypatch, tmp_path, delete_files):
    async def run():
        async with downloads(monkeypatch) as env:
            job = await OfflineDownloadJob.get(download_id=24)
            entries = tuple(
                RemoteManifestEntry(path=name, is_dir=False, size=3)
                for name in ("owned.mkv", "unrelated.mkv")
            )
            owned = tmp_path / "owned.mkv"
            unrelated = tmp_path / "unrelated.mkv"
            partial = tmp_path / f".owned.mkv.{job.job_uuid}.part"
            marker = tmp_path / f".owned.mkv.{job.job_uuid}.done"
            owned.write_bytes(b"abc")
            unrelated.write_bytes(b"xyz")
            marker.write_bytes(puller._ownership_value(owned))
            partial.write_bytes(b"ab")
            library = tmp_path / "library"
            library.mkdir()
            hardlink = library / "movie.mkv"
            hardlink.hardlink_to(owned)
            symlink = library / "linked.mkv"
            symlink.symlink_to(owned)
            await DownloadTask.filter(id=24).update(
                dir=str(tmp_path), transfer_lib_id=60, transfer_method="symlink"
            )
            await OfflineDownloadJob.filter(download_id=24).update(
                manifest=serialize_manifest(entries)
            )

            reviewed, result = await act(
                env.owner, task_id=24, action="delete", delete_files=delete_files
            )
            assert result["outcome"] == "queued" and owned.exists()
            assert any("remote source files stay" in note for note in reviewed["notes"])
            assert any(
                "pending across state changes" in note for note in reviewed["notes"]
            )
            await DownloadTask.filter(id=24).update(state=DownloadState.COMPLETED)
            await env.syncer._consume_actions()

            assert not await DownloadTask.exists(id=24)
            assert owned.exists() is not delete_files
            assert not partial.exists() and not marker.exists()
            assert unrelated.read_bytes() == b"xyz"
            assert hardlink.read_bytes() == b"abc"
            assert symlink.is_symlink() and symlink.exists() is not delete_files
            assert await MediaLib.exists(id=60)

    asyncio.run(run())


def test_deleted_task(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            values = {"task_id": 23, "action": "delete"}
            pausing = await review(env.owner, task_id=21, action="pause")
            reviewed, deleted = await act(env.owner, **values)
            await DownloadTask.filter(id=21).delete()

            replay = await apply(env.owner, reviewed, **values)
            absent = await apply(env.owner, pausing, task_id=21, action="pause")

            assert deleted["outcome"] == "deleted" and deleted["files"] == "kept"
            assert replay["replayed"] and replay["outcome"] == "deleted"
            assert replay["task"] is None
            assert absent["outcome"] == "absent" and not absent["applied"]
            assert len(env.remote.requests) == 1
            with pytest.raises(NotFoundException):
                await review(env.owner, **values)

    asyncio.run(run())


def test_opposite_replay(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            paused_review, paused = await act(env.owner, task_id=21, action="pause")
            _, resumed = await act(env.owner, task_id=21, action="resume")

            replay = await apply(env.owner, paused_review, task_id=21, action="pause")

            assert paused["state"] == "paused" and resumed["state"] == "downloading"
            # the old outcome is reported without pausing the task again
            assert replay["replayed"] and replay["state"] == "paused"
            assert replay["task"]["state"] == "downloading"
            assert [path for path, _ in env.remote.requests] == ["/pause", "/start"]
            assert await state(env.tasks["Ubuntu 24.04"]) == "downloading"

    asyncio.run(run())


def test_permissions(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            # existing pause, resume and retry controls stay open to users
            reader_review, paused = await act(env.reader, task_id=21, action="pause")
            hidden = await review(env.reader, task_id=22, action="resume")
            env.reader.perms = Permissions(media_lib_ids=[60])
            with pytest.raises(ForbiddenException):
                await review(env.reader, task_id=23, action="delete")
            admin_review = await review(env.owner, task_id=23, action="delete")
            demoted = SimpleNamespace(id=env.owner.id, role=UserRole.USER, perms=None)
            with pytest.raises(ForbiddenException):
                await apply(demoted, admin_review, task_id=23, action="delete")
            _, deleted = await act(env.owner, task_id=23, action="delete")

            assert (
                reader_review["actions"] == ["pause"] and paused["outcome"] == "changed"
            )
            assert "transfer_lib_name" not in hidden
            assert admin_review["transfer_lib_name"] == "Movies"
            assert admin_review["transfer_lib_id"] == 60
            assert deleted["outcome"] == "deleted"
            # a deletion outcome is not replayed to an account that lost the right
            receipt = await DownloadTaskReceipt.get(action="delete")
            with pytest.raises(ForbiddenException):
                await service.DownloadTaskService._replay_action(receipt, demoted)

    asyncio.run(run())


def test_unrelated(monkeypatch, tmp_path):
    async def run():
        async with downloads(monkeypatch) as env:
            graph, _, body = await fixture_plan()
            plan = await DownloadPlan.create(
                id=40, **body.plan.model_dump(exclude={"id"})
            )
            await DownloadPlanHistory.create(
                plan=plan, info_hash=env.tasks["Finished movie"].info_hash
            )
            unrelated = tmp_path / "unrelated.mkv"
            unrelated.write_text("kept")
            await DownloadTask.filter(id=24).update(dir=str(tmp_path))
            before = {
                task.id: task.state
                for task in await DownloadTask.exclude(id__in=[23, 24])
            }

            await act(env.owner, task_id=23, action="delete", delete_files=True)
            await act(env.owner, task_id=24, action="delete", delete_files=True)
            await env.syncer._consume_actions()

            assert await state(env.tasks["Finished movie"]) is None
            assert await state(env.tasks["Cloud show"]) is None
            assert {task.id: task.state for task in await DownloadTask.all()} == before
            assert await DownloadPlan.exists(id=plan.id)
            assert await DownloadPlanHistory.filter(plan_id=plan.id).count() == 1
            assert await MediaLib.exists(id=env.library.id)
            assert unrelated.read_text() == "kept"
            assert graph.id != plan.id

    asyncio.run(run())


def test_compacted_review(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            reviewed = await review(
                env.owner, task_id=23, action="delete", delete_files=True
            )
            source = {
                "id": "review",
                "name": "review_download_task_action",
                "ok": True,
                "data": reviewed,
            }
            messages = [
                {
                    "role": "assistant",
                    "content": "Checking. " * 3000,
                    "tool_calls": [{"id": "review"}],
                },
                {
                    "role": "tool",
                    "tool_call_id": "review",
                    "content": dumps(source).decode(),
                },
            ]

            compacted = history.compact_exchanges(messages, 1, 6000)
            fact = loads(compacted[0]["content"].split("\n", 1)[1])["tools"][0]

            # the active review keeps its effect, file choice and request
            assert fact["data"]["notes"] == reviewed["notes"]
            for key in ("delete_files", "request_id", "task_id", "action"):
                assert fact["data"][key] == reviewed[key]
            assert len(dumps(compacted)) < 6000
            # an earlier turn's summary keeps the identities needed to confirm
            summary = tool_facts(source)["data"]
            for key in ("valid", "request_id", "task_id", "action", "delete_files"):
                assert summary[key] == reviewed[key]

    asyncio.run(run())


def test_sizes(monkeypatch):
    async def run():
        async with downloads(monkeypatch) as env:
            for index in range(25):
                await DownloadTask.create(
                    downloader=env.seedbox,
                    name=f"Synthetic episode {index:02d} 1080p WEB-DL",
                    dir="/synthetic/downloads",
                    state=DownloadState.DOWNLOADING,
                    info_hash=f"{index + 100:040x}",
                    percentage=12.5,
                )
            listed = await tools.execute_tool(
                "list_download_tasks",
                dumps({"page_size": 25}).decode(),
                env.owner,
                conversation_id=1,
                call_id="list",
            )
            reviewed = await review(
                env.owner, task_id=23, action="delete", delete_files=True
            )
            _, applied = await act(env.owner, task_id=21, action="pause")
            replayed = await service.DownloadTaskService._replay_action(
                await DownloadTaskReceipt.get(), env.owner
            )
            return listed, reviewed, applied, replayed

    listed, reviewed, applied, replayed = asyncio.run(run())
    admin = SimpleNamespace(id=1, role=UserRole.ADMIN)
    schemas = {
        schema["function"]["name"]: schema
        for schema in tools.tool_schemas(admin, task_action=True)
    }
    added = [schemas[name] for name in sorted(tools.TASK_ACTION_TOOLS)]

    # results stay small; the task tools cost context only once unlocked
    assert len(dumps(listed)) < 4200
    assert len(dumps(reviewed)) < 1500
    assert len(dumps(applied)) < 700 and len(dumps(replayed)) < 800
    assert len(dumps(added)) < 1300
    assert not tools.TASK_ACTION_TOOLS & {
        schema["function"]["name"] for schema in tools.tool_schemas(admin)
    }
    # focused plan and authoring catalogs never carry them
    for focus in ({"plan_only": True}, {"authoring": True}):
        assert not tools.TASK_ACTION_TOOLS & names(
            {"tools": tools.tool_schemas(admin, task_action=True, **focus)}
        )


def listed(body, name):
    """Pick a task from the newest listing as the model sees it.

    Args:
        body: Model request whose last message is the listing result.
        name: Task name to select.

    Returns:
        Matching task ID; compacted listings no longer show it.
    """
    items = loads(body["messages"][-1]["content"])["data"]["items"]
    return next(item["id"] for item in items if item["name"] == name)


def page(body):
    """Read the page context sent with the current prompt.

    Args:
        body: Model request.

    Returns:
        Context captured when the message was sent.
    """
    return loads(body["messages"][-2]["content"].split("\n", 1)[1])


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_conversation(monkeypatch, verbose):
    streams, offered = [], []

    def offer(body, *calls):
        offered.append(names(body) & tools.TASK_ACTION_TOOLS)
        return list(calls)

    def confirm(body):
        return offer(
            body,
            (
                "apply_download_task_action",
                {"request_id": latest_request(body), "confirmed": True},
            ),
        )

    steps = [
        # turn 1: two tasks match, so the model asks instead of reviewing
        lambda body: offer(
            body, ("list_download_tasks", {"name": "Ubuntu", "scope": "active"})
        ),
        lambda body: offer(body),
        # turn 2: the chosen task is reviewed; confirming in the same turn waits
        lambda body: offer(body, ("list_download_tasks", {"name": "Ubuntu 24.04"})),
        lambda body: offer(
            body,
            (
                "review_download_task_action",
                {"task_id": listed(body, "Ubuntu 24.04"), "action": "pause"},
            ),
        ),
        confirm,
        lambda body: offer(body),
        # turn 3: the user confirms, but the response after the action is lost
        confirm,
        lost,
        # turn 4: reading the task and repeating the request reports the outcome
        lambda body: offer(body, ("get_download_task", {"task_id": 21})),
        confirm,
        lambda body: offer(body),
    ]
    configure(monkeypatch, scripted(steps, streams, verbose=verbose))

    async def run():
        async with downloads(monkeypatch) as env:
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            turns = [
                await converse(conversation["id"], env.owner, prompt, key=key)
                for key, prompt in (
                    ("turn-1", "Pause the Ubuntu download."),
                    ("turn-2", "The 24.04 one."),
                    ("turn-3", "Yes, pause it."),
                    ("turn-4", "Did that work? Pause it."),
                )
            ]
            stored = await AssistantTurn.filter(conversation_id=conversation["id"])
            return env, turns, stored, await state(env.tasks["Ubuntu 24.04"])

    env, turns, stored, paused = asyncio.run(run())
    (first, asked), (second, reviewed), (third, confirmed), (fourth, repeated) = turns

    assert [done.event for done, _ in turns] == ["done", "done", "error", "done"]
    # task actions appear after reading tasks, or after a review was shown
    assert offered[0] == offered[2] == set()
    assert offered[1] == offered[3] == offered[6] == tools.TASK_ACTION_TOOLS
    assert offered[7] == tools.TASK_ACTION_TOOLS
    # similar tasks stay distinct and nothing is reviewed until one is chosen
    assert [result["name"] for result in asked] == ["list_download_tasks"]
    assert {item["id"] for item in asked[0]["data"]["items"]} == {21, 22}
    review_data, same_turn = reviewed[1]["data"], reviewed[2]["data"]
    assert review_data["valid"] and review_data["task_id"] == 21
    assert same_turn["confirmation_required"] and not same_turn["applied"]
    assert confirmed[0]["data"]["outcome"] == "changed"
    assert repeated[1]["data"]["replayed"]
    assert repeated[1]["data"]["task"]["state"] == "paused"
    assert len(env.remote.requests) == 1 and paused == "paused"
    # task and downloader IDs are never recorded as workflows or libraries
    for turn in stored:
        assert not {21, 22, 3} & set(turn.resource_ids)


def test_superseded(monkeypatch):
    streams, ids = [], {}

    def confirm(body):
        return [
            (
                "apply_download_task_action",
                {"request_id": ids[name], "confirmed": True},
            )
            for name in ("pause", "delete")
        ]

    steps = [
        lambda body: [("get_download_task", {"task_id": 21})],
        lambda body: [
            ("review_download_task_action", {"task_id": 21, "action": "pause"}),
            ("review_download_task_action", {"task_id": 23, "action": "delete"}),
        ],
        lambda body: None,
        confirm,
        lambda body: None,
    ]
    configure(monkeypatch, scripted(steps, streams))

    async def run():
        async with downloads(monkeypatch) as env:
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            await converse(conversation["id"], env.owner, "Pause or delete?", key="a")
            for record in await AssistantToolCall.filter(
                name="review_download_task_action"
            ):
                ids[record.result["data"]["action"]] = record.result["data"][
                    "request_id"
                ]
            done, results = await converse(
                conversation["id"], env.owner, "Confirmed, both.", key="b"
            )

            older, newer = (result["data"] for result in results)
            assert done.event == "done"
            # one task per confirmed action: the newest review replaces the older
            assert older["confirmation_required"] and "newer review" in older["message"]
            assert newer["outcome"] == "deleted"
            assert [path for path, _ in env.remote.requests] == ["/delete"]
            assert await state(env.tasks["Ubuntu 24.04"]) == "downloading"

    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_invalid_review_supersedes(monkeypatch, failure):
    async def run():
        async with downloads(monkeypatch) as env:
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            turn = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="review-actions",
                status="complete",
                prompt="Pause it.",
            )
            original = None
            for index, action in enumerate(["pause", "resume"]):
                args = {"task_id": 21, "action": action}
                result = await review(env.owner, **args)
                await AssistantToolCall.create(
                    turn_id=turn.id,
                    call_id=f"review-{index}",
                    name="review_download_task_action",
                    arguments=dumps(args).decode(),
                    status="complete",
                    result={"ok": False, "error": "not_found"}
                    if failure and index
                    else {"ok": True, "data": result},
                )
                original = original or result
            confirmed = await tools.apply_task_action(
                tools.TaskActionConfirm(
                    request_id=original["request_id"], confirmed=True
                ),
                conversation["id"],
                env.owner,
            )

            assert confirmed["confirmation_required"] and not confirmed["applied"]
            assert env.remote.requests == []

    asyncio.run(run())


def test_page_selection(monkeypatch):
    streams, seen = [], []

    def selected(body):
        seen.append(page(body))
        return [
            ("get_download_task", {"task_id": task_id})
            for task_id in seen[-1]["selected_ids"]
        ]

    def delete(body):
        task_id = seen[-1]["selected_ids"][0]
        return [
            ("review_download_task_action", {"task_id": task_id, "action": "delete"})
        ]

    steps = [
        selected,
        None,
        selected,
        delete,
        None,
        lambda body: [
            (
                "apply_download_task_action",
                {"request_id": latest_request(body), "confirmed": True},
            )
        ],
        None,
        selected,
        None,
    ]
    configure(
        monkeypatch,
        scripted([step or (lambda body: None) for step in steps], streams),
    )

    async def run():
        async with downloads(monkeypatch) as env:
            owner = env.owner
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            completed = {
                **ACTIVE,
                "page_type": "completed_downloads",
                "route": "/downloads/completed",
                "selected_ids": [23],
            }

            _, ambiguous = await converse(
                conversation["id"],
                owner,
                "Pause the selected download.",
                context={**ACTIVE, "selected_ids": [21, 22]},
                key="a",
            )
            _, reviewed = await converse(
                conversation["id"],
                owner,
                "Delete the selected one.",
                context=completed,
                key="b",
            )
            done, applied = await converse(conversation["id"], owner, "Yes.", key="c")
            _, gone = await converse(
                conversation["id"],
                owner,
                "Delete it again.",
                context=completed,
                key="d",
            )

            # both selected tasks are read with distinct identities; none is reviewed
            assert [result["name"] for result in ambiguous] == ["get_download_task"] * 2
            assert {
                (r["data"]["id"], r["data"]["state"], tuple(r["data"]["actions"]))
                for r in ambiguous
            } == {
                (21, "downloading", ("pause", "delete")),
                (22, "paused", ("resume", "delete")),
            }
            assert reviewed[1]["data"]["task_id"] == 23
            assert reviewed[1]["data"]["transfer_lib_name"] == "Movies"
            assert done.event == "done" and applied[0]["data"]["outcome"] == "deleted"
            assert applied[0]["data"]["path"] == "/downloads/completed"
            # a selection that no longer exists is reported, never guessed
            assert gone[0]["error"]["code"] == "not_found"
            assert seen[-1]["selected_ids"] == [23]
            selection = await AssistantTurn.get(request_id="b")
            assert f"media_lib:{env.library.id}" in selection.resource_ids
            assert 23 not in selection.resource_ids

            # history naming the library follows current permissions
            owner.role = UserRole.USER
            owner.perms = Permissions()
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": selection.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="history",
            )
            refused = await tools.execute_tool(
                "review_download_task_action",
                dumps({"task_id": 22, "action": "delete"}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="review",
            )
            allowed = await tools.execute_tool(
                "review_download_task_action",
                dumps({"task_id": 22, "action": "resume"}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="review",
            )
            assert denied["error"]["code"] == "permission_denied"
            assert refused["error"]["code"] == "permission_denied"
            assert allowed["data"]["valid"]

    asyncio.run(run())


def test_mcp(monkeypatch):
    async def run():
        async with application() as app:
            remote = Remote()
            app.ctx.httpx = httpx.AsyncClient(transport=httpx.MockTransport(remote))
            app.ctx.dl_syncer = synchronizer()
            # other modules may leave apps registered; drivers read this one
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            try:
                await store()
                reader = app.shared_ctx.sessions["reader"]
                admin = app.shared_ctx.sessions["admin"]
                pause = {"task_id": 21, "action": "pause"}

                reviewed = await call(
                    app, "review_download_task_action", {"change": pause}
                )
                data = reviewed["structuredContent"]
                arguments = {
                    "application": {
                        "request_id": data["request_id"],
                        "change": pause,
                        "revision": data["revision"],
                        "confirmed": True,
                    }
                }
                read_only = await call(app, "apply_download_task_action", arguments)
                tokens = {
                    user.id: (
                        await MCPAccessTokenService.create(
                            user.id,
                            MCPAccessTokenCreate(
                                name="tasks", scope=MCPTokenScope.READ_WRITE
                            ),
                        )
                    )["token"]
                    for user in (reader, admin)
                }
                applied = await call(
                    app,
                    "apply_download_task_action",
                    arguments,
                    token=tokens[reader.id],
                )
                replay = await call(
                    app,
                    "apply_download_task_action",
                    arguments,
                    token=tokens[reader.id],
                )
                delete = {"task_id": 23, "action": "delete", "delete_files": True}
                denied = await call(
                    app,
                    "review_download_task_action",
                    {"change": delete},
                    token=tokens[reader.id],
                )
                deleting = await call(
                    app,
                    "review_download_task_action",
                    {"change": delete},
                    token="admin",
                )
                shared = await tools.execute_tool(
                    "review_download_task_action",
                    dumps(delete).decode(),
                    admin,
                    conversation_id=1,
                    call_id="parity",
                )
                removal = deleting["structuredContent"]
                application_args = {
                    "request_id": removal["request_id"],
                    "change": delete,
                    "revision": removal["revision"],
                    "confirmed": True,
                }
                changed = await call(
                    app,
                    "apply_download_task_action",
                    {
                        "application": {
                            **application_args,
                            "change": {**delete, "delete_files": False},
                        }
                    },
                    token=tokens[admin.id],
                )
                deleted = await call(
                    app,
                    "apply_download_task_action",
                    {"application": application_args},
                    token=tokens[admin.id],
                )

                assert data["valid"] and data["dispatch"] == "immediate"
                assert read_only["isError"] and "read-only" in str(read_only)
                assert applied["structuredContent"]["outcome"] == "changed"
                assert replay["structuredContent"]["replayed"]
                assert denied["isError"] and "not allowed" in str(denied)
                assert changed["structuredContent"]["conflict"]
                assert deleted["structuredContent"]["outcome"] == "deleted"
                assert [path for path, _ in remote.requests] == ["/pause", "/delete"]
                assert remote.requests[1][1]["deleteFiles"] == "true"
                # assistant and MCP share the review apart from request identities
                removal.pop("request_id")
                shared["data"].pop("request_id")
                assert shared["data"] == removal
                for private in PRIVATE:
                    assert private not in dumps([reviewed, deleting, deleted]).decode()
            finally:
                await app.ctx.httpx.aclose()

    asyncio.run(run())
