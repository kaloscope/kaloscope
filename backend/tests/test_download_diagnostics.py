"""Tests for persisted subscription, transfer and library-index diagnostics."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.ai import tools
from app.core.dl import syncer
from app.core.dl.driver import DownloadState
from app.core.exceptions import ForbiddenException, NotFoundException
from app.models.download import (
    DownloadPlan,
    DownloadPlanAttempt,
    DownloadTask,
    OfflineDownloadJob,
)
from app.models.media import MediaEvent, MediaItem, MediaLib
from app.services.download import DownloadPlanService
from app.services.download_diagnostics import IngestionQuery, diagnose_ingestion
from app.utils.json import dumps
from tests.test_assistant_persistence import database
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_mcp import application, call
from tests.test_services_download import DraftDriver

pytestmark = pytest.mark.usefixtures("isolate")


def test_trace(monkeypatch):
    async def run():
        async with database() as (owner, other):
            graph, downloader, creation = await fixture_plan(batch_limit=1)
            made = await DownloadPlanService.create_once(creation, owner)
            plan = await DownloadPlan.get(id=made["plan_id"])
            query = IngestionQuery(plan_id=plan.id)
            assert (await diagnose_ingestion(query, owner))[
                "stage"
            ] == "first_check_queued"
            engine = SimpleNamespace(
                execute=AsyncMock(
                    side_effect=[{"items": []}, {"items": [{"link": "a" * 40}]}]
                )
            )
            monkeypatch.setattr(
                syncer.Sanic,
                "get_app",
                lambda: SimpleNamespace(ctx=SimpleNamespace(flow_engine=engine)),
            )

            await syncer.execute_download_plan(plan, DraftDriver())
            await syncer.execute_download_plan(plan, DraftDriver())
            attempts = (await diagnose_ingestion(query, owner))["attempts"]
            assert [a["stage"] for a in attempts] == [
                "tasks_submitted",
                "no_search_results",
            ]
            attempt = await diagnose_ingestion(
                IngestionQuery(attempt_id=attempts[0]["id"]), owner
            )
            assert attempt["results_seen"] == attempt["linked_task_count"] == 1
            task = await DownloadTask.get(id=attempt["tasks"][0]["id"])
            task_query = IngestionQuery(task_id=task.id)
            initial = await diagnose_ingestion(task_query, owner)
            assert initial["plan_id"] == plan.id
            assert initial["attempt_id"] == attempts[0]["id"]
            assert initial["stage"] == "download_in_progress"

            first = await MediaLib.create(
                dir="/synthetic/shows", name="Shows", priority=1, lib_type="movie"
            )
            second = await MediaLib.create(
                dir="/synthetic/other", name="Other", priority=2, lib_type="movie"
            )
            paths = [f"/synthetic/shows/{i}.mkv" for i in range(26)]
            await DownloadTask.filter(id=task.id).update(
                state=DownloadState.COMPLETED,
                transfer_method="copy",
                transfer_lib_id=first.id,
                transfer_targets={str(i): path for i, path in enumerate(paths)},
            )
            await MediaItem.create(
                lib=first, dir=first.dir, path=paths[0], name="Known"
            )
            await MediaItem.create(
                lib=first, dir=first.dir, path=paths[1], name="Hidden", visible=False
            )
            await MediaItem.create(
                lib=second, dir=second.dir, path=paths[2], name="Other"
            )
            await MediaEvent.create(lib=first, src_path=paths[3], event_type="created")
            facts = await diagnose_ingestion(task_query, owner)
            assert facts["stage"] == "transfer_recorded" and facts["next_page"] == 2
            assert len(facts["targets"]) == 25
            assert [dest["state"] for dest in facts["targets"][:4]] == [
                "indexed",
                "hidden",
                "no_index_record",
                "event_pending",
            ]
            assert facts["targets"][3]["pending_events"] == ["created"]
            assert b"/synthetic/" not in dumps(facts) and b"magnet:" not in dumps(facts)
            assert (
                await diagnose_ingestion(IngestionQuery(task_id=task.id, page=2), owner)
            )["targets"][0]["filename"] == "25.mkv"
            await DownloadTask.filter(id=task.id).update(last_transfer_failed=True)
            assert (await diagnose_ingestion(task_query, owner))[
                "stage"
            ] == "transfer_failed"
            await plan.delete()
            assert (await diagnose_ingestion(query, owner))["stage"] == "plan_removed"

            legacy = await DownloadTask.create(
                dir="/synthetic", downloader=downloader, name="Known", state="completed"
            )
            facts = await diagnose_ingestion(IngestionQuery(task_id=legacy.id), owner)
            assert facts["attempt_id"] is None and "plan_id" not in facts
            other.role = "user"
            with pytest.raises(ForbiddenException):
                await diagnose_ingestion(task_query, other)
            with pytest.raises(NotFoundException):
                await diagnose_ingestion(IngestionQuery(plan_id=999), owner)
            assert "diagnose_download_ingestion" not in tools.available_tools(other)

    asyncio.run(run())


def test_completion_queue():
    async def run():
        async with database() as (owner, _):
            _, downloader, _ = await fixture_plan()
            lib = await MediaLib.create(
                dir="/synthetic/library", name="Library", lib_type="movie", priority=1
            )
            task = await DownloadTask.create(
                dir="/synthetic",
                name="Media",
                downloader=downloader,
                state="completed",
                transfer_lib=lib,
            )
            query = IngestionQuery(task_id=task.id)
            facts = await diagnose_ingestion(query, owner)
            assert facts["stage"] == "no_transfer_configured"
            assert "transfer_destinations" not in facts["missing_evidence"]
            await DownloadTask.filter(id=task.id).update(transfer_method="hardlink")
            await OfflineDownloadJob.create(
                download_id=task.id,
                job_uuid="a" * 32,
                source_fingerprint="b" * 64,
                remote_dir="/off",
                completion_due_at=datetime.now(UTC),
            )
            facts = await diagnose_ingestion(query, owner)
            assert facts["transfer_pending"] and facts["stage"] == "transfer_pending"

    asyncio.run(run())


def test_offline(monkeypatch):
    async def run():
        async with database() as (owner, _):
            _, _, creation = await fixture_plan()
            made = await DownloadPlanService.create_once(creation, owner)
            driver = SimpleNamespace(
                supports_version=True,
                version=AsyncMock(side_effect=RuntimeError("offline")),
                close=AsyncMock(),
            )
            monkeypatch.setattr(syncer, "load_driver", lambda _: driver)
            await syncer.check_download_plans(first_only=True)
            attempt = await DownloadPlanAttempt.get(plan_id=made["plan_id"])
            facts = await diagnose_ingestion(
                IngestionQuery(attempt_id=attempt.id), owner
            )
            assert facts["stage"] == "driver_offline" and not facts["tasks"]

    asyncio.run(run())


def test_diagnostic_scope():
    async def run():
        async with application() as app:
            graph, _, _ = await fixture_plan()
            attempt = await DownloadPlanAttempt.create(
                plan_id=15, workflow_id=graph.id, status="unconfirmed"
            )
            query = {"query": {"attempt_id": attempt.id}}
            reader = await call(app, "diagnose_download_ingestion", query)
            assert reader["isError"]
            admin = await call(app, "diagnose_download_ingestion", query, token="admin")
            assert admin["structuredContent"]["stage"] == "unconfirmed"
            assert admin["structuredContent"]["linked_task_count"] == 0

    asyncio.run(run())
