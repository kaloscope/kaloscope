"""Unit tests for reviewed library ingestion settings changes."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.ai import history, tools
from app.core.ai.documents import tool_facts
from app.core.dl.syncer import submission_lock
from app.core.exceptions import ForbiddenException, NotFoundException
from app.core.media import organizer
from app.core.media.coordination import library_lock
from app.models.assistant import AssistantToolCall, AssistantTurn, ConversationCreate
from app.models.flow import FlowGraph, FlowTrigger, GraphCategory, GraphRef
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import (
    Language,
    LibraryConfig,
    MediaChangeReceipt,
    MediaEvent,
    MediaItem,
    MediaLib,
    MediaLibUpsert,
    NFOType,
)
from app.models.user import Permissions, UserRole
from app.services import media_changes as service
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.flow import FlowTriggerService
from app.services.mcp import MCPAccessTokenService
from app.services.media import MediaLibService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_changes import latest_request, lost
from tests.test_download_plan_ingestion import scraper
from tests.test_mcp import application, call
from tests.test_media_changes import (
    Engine,
    apply,
    media,
    monitor_for,
    page_of,
    store,
    write_nfo,
)
from tests.test_services_assistant import configure
from tests.test_tv_ingestion import tv_scraper
from tests.test_workflow_runs import converse, names, scripted

MOVIE_TEMPLATE = "{{title}} ({{year}})/{{title}} ({{year}})"
TV_TEMPLATE = "{{parent.title}}/Season {{season}}/{{code}} - {{title}}"


async def review(user, library_id, **change):
    """Review one configuration through the shared service.

    Args:
        user: Calling account.
        library_id: Library to configure.
        **change: Requested fields.

    Returns:
        Shared review.
    """
    return await service.MediaChangeService.review_config(
        LibraryConfig.model_validate({"library_id": library_id, **change}), user
    )


async def configure_library(user, library_id, **change):
    """Review a configuration and apply it at once with confirmation.

    Args:
        user: Calling account.
        library_id: Library to configure.
        **change: Requested fields.

    Returns:
        The change, its review and the outcome.
    """
    reviewed = await review(user, library_id, **change)
    assert reviewed["valid"], reviewed
    values = {"action": "configure", "library_id": library_id, **change}
    return values, reviewed, await apply(user, reviewed, values)


async def scrapers(count=2, start=20, definition=None):
    """Create published movie scrapers that are not bound to anything.

    Args:
        count: How many to create.
        start: First workflow ID.
        definition: Published definition; defaults to a movie scraper.

    Returns:
        Created workflows.
    """
    return [
        await FlowGraph.create(
            id=start + index,
            name=f"Scraper {start + index}",
            category="ingest",
            state="published",
            revision=1,
            definition=definition or scraper(),
        )
        for index in range(count)
    ]


async def stored_draft(env):
    """Bind an unpublished scraper asynchronously after the published one.

    Args:
        env: Media fixture.

    Returns:
        The draft workflow.
    """
    draft = await FlowGraph.create(
        id=15, name="Draft scraper", category="ingest", state="draft", draft=scraper()
    )
    await FlowTrigger.create(
        category="ingest",
        rel_id=env.movies.id,
        graph=draft,
        priority=2,
        asynchronous=True,
    )
    return draft


async def rows_of(lib):
    """Read a library's stored bindings in order.

    Args:
        lib: Library.

    Returns:
        Workflow, order and execution mode of each binding.
    """
    return [
        (row.graph_id, row.priority, row.asynchronous)
        for row in await FlowTrigger.filter(rel_id=lib.id).order_by("priority")
    ]


async def snapshot(root):
    """Capture files and database rows a preview must leave alone.

    Args:
        root: Library roots' parent.

    Returns:
        File contents and stored records.
    """
    files = {
        str(path): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.suffix != ".lock"
    }
    return (
        files,
        await MediaItem.all().order_by("id").values(),
        await MediaLib.all().order_by("id").values(),
        await MediaEvent.all().count(),
        await FlowTrigger.all().order_by("id").values(),
    )


def test_read(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            draft = await stored_draft(env)
            await scrapers(1)
            before = await snapshot(tmp_path)

            current = await review(env.owner, env.movies.id)

            assert not current["valid"] and "request_id" not in current
            assert [issue["path"] for issue in current["issues"]] == ["library_id"]
            assert current["bindings"] == [
                {
                    "workflow_id": env.movie_graph.id,
                    "workflow_name": "Movie scraper",
                    "executable": True,
                    "compatible": True,
                    "order": 1,
                    "asynchronous": False,
                },
                {
                    "workflow_id": draft.id,
                    "workflow_name": "Draft scraper",
                    "executable": False,
                    "compatible": False,
                    "order": 2,
                    "asynchronous": True,
                },
            ]
            assert [entry["workflow_id"] for entry in current["workflows"]] == [20]
            assert current["language"] is None and current["naming"] is None
            assert any("Unpublished bindings" in note for note in current["notes"])
            assert "every new item" in current["scope"]
            assert await snapshot(tmp_path) == before
            assert not await MediaChangeReceipt.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("change", "expected", "summary"),
    [
        (
            {"add_workflow_ids": [20]},
            [(7, False), (15, True), (20, False)],
            {"added": [20]},
        ),
        (
            {"replace_workflows": [{"workflow_id": 7, "new_workflow_id": 20}]},
            [(20, False), (15, True)],
            {"replaced": [(7, 20)]},
        ),
        (
            {"replace_workflows": [{"workflow_id": 15, "new_workflow_id": 20}]},
            [(7, False), (20, True)],
            {"replaced": [(15, 20)]},
        ),
        ({"remove_workflow_ids": [7]}, [(15, True)], {"removed": [7]}),
        ({"remove_workflow_ids": [7, 15]}, [], {"removed": [7, 15]}),
        (
            {"order_workflow_ids": [15, 7]},
            [(15, True), (7, False)],
            {"reordered": True},
        ),
        (
            {
                "replace_workflows": [{"workflow_id": 7, "new_workflow_id": 20}],
                "add_workflow_ids": [21],
                "order_workflow_ids": [21, 15, 20],
            },
            [(21, False), (15, True), (20, False)],
            {"added": [21], "replaced": [(7, 20)], "reordered": True},
        ),
    ],
)
def test_bindings(monkeypatch, tmp_path, change, expected, summary):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await stored_draft(env)
            await scrapers()
            await MediaLib.filter(id=env.movies.id).update(
                language=Language.ZH_CN, rename_template=MOVIE_TEMPLATE
            )

            _, reviewed, applied = await configure_library(
                env.owner, env.movies.id, **change
            )
            lib = await MediaLib.get(id=env.movies.id)
            stored = await rows_of(lib)

            assert applied["outcome"] == "configured" and not applied["replayed"]
            assert [(row[0], row[2]) for row in stored] == expected
            assert [row[1] for row in stored] == list(range(1, len(stored) + 1))
            # every setting the request omitted stays as it was
            assert lib.language == Language.ZH_CN
            assert lib.rename_template == MOVIE_TEMPLATE
            assert reviewed["settings"] == []
            result = applied["bindings_change"]
            assert [e["workflow_id"] for e in result["added"]] == summary.get(
                "added", []
            )
            assert [e["workflow_id"] for e in result["removed"]] == summary.get(
                "removed", []
            )
            assert [
                (e["workflow_id"], e["new_workflow_id"]) for e in result["replaced"]
            ] == summary.get("replaced", [])
            assert result["reordered"] is summary.get("reordered", False)
            assert [
                (e["workflow_id"], e["asynchronous"])
                for e in applied["proposed_bindings"]
            ] == expected

    asyncio.run(run())


@pytest.mark.parametrize(
    ("start", "change", "language", "template"),
    [
        ({}, {"language": "zh-CN"}, Language.ZH_CN, None),
        ({"language": Language.EN_US}, {"language": None}, None, None),
        ({}, {"rename_template": "{{title}}"}, None, "{{title}}"),
        ({"rename_template": "{{title}}"}, {"rename_template": None}, None, None),
        ({"rename_template": "{{title}}"}, {"rename_template": "  "}, None, None),
        (
            {"language": Language.EN_US, "rename_template": "{{title}}"},
            {"language": "zh-CN"},
            Language.ZH_CN,
            "{{title}}",
        ),
        (
            {"language": Language.EN_US, "rename_template": "{{title}}"},
            {"rename_template": None},
            Language.EN_US,
            None,
        ),
    ],
)
def test_settings(monkeypatch, tmp_path, start, change, language, template):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await MediaLib.filter(id=env.movies.id).update(
                scan_on_startup=False, danmaku_server="dm.test", danmaku_ttl=5, **start
            )
            bindings = await rows_of(env.movies)

            _, reviewed, applied = await configure_library(
                env.owner, env.movies.id, **change
            )
            lib = await MediaLib.get(id=env.movies.id)

            assert applied["outcome"] == "configured"
            assert (lib.language, lib.rename_template) == (language, template)
            assert (lib.name, lib.dir) == (env.movies.name, env.movies.dir)
            assert (lib.scan_on_startup, lib.danmaku_server, lib.danmaku_ttl) == (
                False,
                "dm.test",
                5,
            )
            assert await rows_of(lib) == bindings
            assert reviewed["bindings_change"] is None
            assert reviewed["settings"] == applied["settings"]
            assert [entry["field"] for entry in reviewed["settings"]] == list(change)

    asyncio.run(run())


def test_omitted_differs_from_cleared(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await MediaLib.filter(id=env.movies.id).update(language=Language.EN_US)
            reviewed = await review(
                env.owner, env.movies.id, rename_template="{{title}}"
            )
            values = {
                "action": "configure",
                "library_id": env.movies.id,
                "rename_template": "{{title}}",
            }

            # the same revision cannot carry an unreviewed clear of the language
            altered = await apply(env.owner, reviewed, {**values, "language": None})
            lib = await MediaLib.get(id=env.movies.id)

            assert altered["conflict"] and lib.language == Language.EN_US
            assert lib.rename_template is None
            applied = await apply(env.owner, reviewed, values)
            assert applied["outcome"] == "configured"
            assert (await MediaLib.get(id=env.movies.id)).language == Language.EN_US

    asyncio.run(run())


@pytest.mark.parametrize(
    ("change", "path", "message"),
    [
        ({"add_workflow_ids": [15]}, "add_workflow_ids", "already bound"),
        ({"add_workflow_ids": [30]}, "add_workflow_ids", "Publish"),
        ({"add_workflow_ids": [31]}, "add_workflow_ids", "movie"),
        ({"add_workflow_ids": [32]}, "add_workflow_ids", "ingest workflow"),
        ({"add_workflow_ids": [99]}, "add_workflow_ids", "does not exist"),
        ({"remove_workflow_ids": [20]}, "remove_workflow_ids", "not bound"),
        (
            {"replace_workflows": [{"workflow_id": 20, "new_workflow_id": 21}]},
            "replace_workflows",
            "not bound",
        ),
        (
            {"replace_workflows": [{"workflow_id": 7, "new_workflow_id": 15}]},
            "replace_workflows",
            "already bound",
        ),
        (
            {"replace_workflows": [{"workflow_id": 7, "new_workflow_id": 30}]},
            "replace_workflows",
            "Publish",
        ),
        ({"order_workflow_ids": [7]}, "order_workflow_ids", "exactly once"),
        ({"order_workflow_ids": [7, 7]}, "order_workflow_ids", "exactly once"),
        ({"order_workflow_ids": [7, 15, 20]}, "order_workflow_ids", "exactly once"),
        ({"rename_template": "a/b/c/d"}, "rename_template", "path components"),
        ({"rename_template": "{{season}}"}, "rename_template", "placeholder"),
        ({"rename_template": "../{{title}}"}, "rename_template", "unsafe"),
    ],
)
def test_invalid(monkeypatch, tmp_path, change, path, message):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await stored_draft(env)
            await scrapers()
            await FlowGraph.create(
                id=30,
                name="Unpublished",
                category="ingest",
                state="draft",
                draft=scraper(),
            )
            await scrapers(1, start=31, definition=tv_scraper())
            await FlowGraph.create(
                id=32,
                name="Indexer",
                category="indexer",
                state="published",
                definition=scraper(),
            )
            before = await rows_of(env.movies)

            reviewed = await review(env.owner, env.movies.id, **change)

            assert not reviewed["valid"] and "request_id" not in reviewed
            assert any(
                issue["path"] == path and message in issue["message"]
                for issue in reviewed["issues"]
            ), reviewed["issues"]
            assert await rows_of(env.movies) == before

    asyncio.run(run())


def test_reading_library(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            reviewed = await review(env.owner, env.books.id, language="zh-CN")

            assert not reviewed["valid"] and "request_id" not in reviewed
            assert reviewed["issues"][0]["path"] == "library_id"
            with pytest.raises(NotFoundException):
                await review(env.owner, 99)

    asyncio.run(run())


def test_beyond_sample(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            workflows = await scrapers(12)

            current = await review(env.owner, env.movies.id)
            _, _, applied = await configure_library(
                env.owner, env.movies.id, add_workflow_ids=[workflows[-1].id]
            )

            shown = {entry["workflow_id"] for entry in current["workflows"]}
            assert len(shown) == 10 and current["workflow_count"] == 12
            assert "list_workflows" in current["more_workflows"]
            assert workflows[-1].id not in shown
            assert applied["outcome"] == "configured"
            assert (await rows_of(env.movies))[-1][0] == workflows[-1].id

    asyncio.run(run())


def test_no_scraper_then_published(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await FlowTrigger.filter(rel_id=env.shows.id).delete()
            await FlowGraph.filter(id=env.tv_graph.id).update(state="draft")

            missing = await review(env.owner, env.shows.id)
            unpublished = await review(
                env.owner, env.shows.id, add_workflow_ids=[env.tv_graph.id]
            )
            # the user publishes outside this flow; nothing here publishes it
            still_draft = await FlowGraph.get(id=env.tv_graph.id)
            await FlowGraph.filter(id=env.tv_graph.id).update(state="published")
            _, _, applied = await configure_library(
                env.owner, env.shows.id, add_workflow_ids=[env.tv_graph.id]
            )

            assert "create_workflow_draft" in missing["next"]
            assert "tv_scraper" in missing["next"]
            assert env.tv_graph.id in [
                entry["workflow_id"] for entry in missing["unpublished_workflows"]
            ]
            assert not unpublished["valid"] and "Publish" in str(unpublished["issues"])
            assert still_draft.state == "draft"
            assert applied["outcome"] == "configured"
            assert [row[0] for row in await rows_of(env.shows)] == [env.tv_graph.id]
            admin = SimpleNamespace(id=1, role=UserRole.ADMIN)
            assert "create_workflow_draft" in names(
                {"tools": tools.tool_schemas(admin, media_change=True)}
            )

    asyncio.run(run())


def test_movie_naming(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            before = await snapshot(tmp_path)

            reviewed = await review(
                env.owner, env.movies.id, rename_template=MOVIE_TEMPLATE
            )

            naming = reviewed["naming"]
            assert await snapshot(tmp_path) == before
            assert (naming["sampled_works"], naming["total_works"]) == (1, 1)
            sample = naming["samples"][0]
            assert sample["status"] == "moves" and sample["issue"] is None
            assert sample["paths"] == [
                {
                    "current": "Old Movie/Old Movie.mkv",
                    "proposed": "Old Movie (2001)/Old Movie (2001).mkv",
                }
            ]
            assert any("Files do not move on save" in n for n in reviewed["notes"])

            # the organizer itself reaches the previewed paths once something
            # triggers it
            values = {
                "action": "configure",
                "library_id": env.movies.id,
                "rename_template": MOVIE_TEMPLATE,
            }
            await apply(env.owner, reviewed, values)
            lib = await MediaLib.get(id=env.movies.id)
            async with library_lock(lib.dir):
                await organizer.organize_items(lib, [env.movie.id])
            video = await MediaItem.filter(parent_id=env.movie.id).first()
            proposed = Path(lib.dir) / sample["paths"][0]["proposed"]
            assert proposed.is_file() and video.path == str(proposed)

    asyncio.run(run())


@pytest.mark.parametrize("split_seasons", [False, True])
def test_tv_naming(monkeypatch, tmp_path, split_seasons):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            if split_seasons:
                path = Path(env.show.dir) / "S02E01.mkv"
                path.write_bytes(b"season two")
                await write_nfo(
                    NFOType.EPISODE,
                    path.with_suffix(".nfo"),
                    {"title": "Second season", "season": 2, "episode": 1},
                )
                await MediaItem.create(
                    lib=env.shows,
                    parent=env.show,
                    path=str(path),
                    dir=str(path.parent),
                    name=path.stem,
                    season=2,
                    episode=1,
                    nfo_path=str(path.with_suffix(".nfo")),
                )
            before = await snapshot(tmp_path)

            reviewed = await review(
                env.owner, env.shows.id, rename_template=TV_TEMPLATE
            )

            sample = reviewed["naming"]["samples"][0]
            assert await snapshot(tmp_path) == before
            assert sample["status"] == "moves"
            assert sample["more_paths"] == (2 if split_seasons else 1)
            planned = await organizer.preview_organization(
                env.shows, TV_TEMPLATE, env.show
            )
            proposed = {
                Path(entry["current"]).name: Path(entry["proposed"]).relative_to(
                    env.shows.dir
                )
                for entry in planned["paths"]
            }
            # numbered metadata renames; files without metadata keep their names
            expected = {
                "S01E01.mkv": Path("Old Show/Season 01/S01E01 - Pilot.mkv"),
                "S01E02.mkv": Path("Old Show/Season 01/S01E02.mkv"),
                "Extra.mkv": Path("Old Show/Season 01/Extra.mkv"),
            }
            if split_seasons:
                expected["S02E01.mkv"] = Path(
                    "Old Show/Season 02/S02E01 - Second season.mkv"
                )
            assert proposed == expected
            assert any("never guessed" in note for note in reviewed["notes"])

            values = {
                "action": "configure",
                "library_id": env.shows.id,
                "rename_template": TV_TEMPLATE,
            }
            await apply(env.owner, reviewed, values)
            lib = await MediaLib.get(id=env.shows.id)
            async with library_lock(lib.dir):
                await organizer.organize_items(lib, [env.show.id])
            for path in proposed.values():
                assert (Path(lib.dir) / path).is_file()

    asyncio.run(run())


def test_unreadable_show(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            Path(env.episodes[0].nfo_path).write_text("<episodedetails><title>")
            before = await snapshot(tmp_path)

            planned = await organizer.preview_organization(
                env.shows, TV_TEMPLATE, env.show
            )
            monkeypatch.setattr(
                organizer, "_season_groups", AsyncMock(side_effect=OSError("denied"))
            )
            reviewed = await review(env.owner, 61, rename_template=TV_TEMPLATE)

            # a damaged episode keeps its file name instead of inventing numbers
            assert await snapshot(tmp_path) == before
            assert {Path(entry["proposed"]).name for entry in planned["paths"]} == {
                "S01E01.mkv",
                "S01E02.mkv",
                "Extra.mkv",
            }
            # a failed read is reported in the sample, not raised from the review
            sample = reviewed["naming"]["samples"][0]
            assert reviewed["valid"] and sample["paths"] == []
            assert sample["status"] == "skipped"
            assert sample["issue"] == "metadata is missing or unreadable"

    asyncio.run(run())


def test_naming_limits(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            root = Path(env.movies.dir)
            # a work that would land on an occupied destination
            taken = root / "Old Movie (2001)"
            taken.mkdir()
            (taken / "Old Movie (2001).mkv").write_bytes(b"other")
            # works without metadata and without a year
            for name, data in (
                ("No Metadata", None),
                ("Yearless", {"title": "Yearless"}),
            ):
                folder = root / name
                folder.mkdir()
                (folder / f"{name}.mkv").write_bytes(b"movie")
                nfo = None
                if data:
                    nfo = folder / f"{name}.nfo"
                    await write_nfo(NFOType.MOVIE, nfo, data)
                work = await MediaItem.create(
                    lib=env.movies,
                    path=str(folder),
                    dir=str(folder),
                    name=name,
                    nfo_path=str(nfo) if nfo else None,
                )
                await MediaItem.create(
                    lib=env.movies,
                    parent=work,
                    path=str(folder / f"{name}.mkv"),
                    dir=str(folder),
                    name=name,
                )
            await MediaEvent.create(
                lib=env.movies, src_path=str(root), event_type="organize", payload={}
            )
            for index in range(2):
                await MediaItem.create(
                    lib=env.movies,
                    path=str(root / f"Later {index}.mkv"),
                    dir=str(root),
                    name=f"Later {index}",
                )
            before = await snapshot(tmp_path)

            reviewed = await review(
                env.owner, env.movies.id, rename_template=MOVIE_TEMPLATE
            )

            naming = reviewed["naming"]
            samples = {sample["item_id"]: sample for sample in naming["samples"]}
            assert await snapshot(tmp_path) == before
            # samples are labelled as such, never complete coverage
            assert (naming["sampled_works"], naming["total_works"]) == (3, 5)
            assert naming["pending_organization"] is True
            assert reviewed["valid"]
            assert str(root) not in dumps(naming).decode()
            issues = {
                entry: (samples[entry]["status"], samples[entry]["issue"])
                for entry in samples
            }
            conflict, yearless, unread = naming["samples"]
            assert conflict["item_id"] == env.movie.id
            assert issues[env.movie.id] == (
                "blocked",
                "nonempty destination has no matching NFO identity",
            )
            assert (yearless["title"], yearless["status"]) == ("Yearless", "blocked")
            assert yearless["issue"] == "missing metadata field: year"
            assert (unread["title"], unread["status"]) == ("No Metadata", "skipped")
            assert unread["issue"] == "metadata is missing or unreadable"
            assert all(not sample["paths"] for sample in naming["samples"])

    asyncio.run(run())


def test_no_dispatch(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await scrapers()
            scan = AsyncMock()
            monkeypatch.setattr(env.watcher, "scan_directory", scan)
            organize = AsyncMock()
            monkeypatch.setattr(organizer, "organize_items", organize)
            before = await snapshot(tmp_path)

            _, _, applied = await configure_library(
                env.owner,
                env.movies.id,
                add_workflow_ids=[20],
                language="zh-CN",
                rename_template=MOVIE_TEMPLATE,
            )

            # saving starts nothing; later work picks the settings up on its own
            assert applied["outcome"] == "configured"
            assert "nothing was scanned" in applied["message"]
            scan.assert_not_awaited()
            organize.assert_not_awaited()
            assert env.engine.calls == []
            assert not await MediaEvent.exists()
            # only the library row and its bindings changed; items and files did not
            after = await snapshot(tmp_path)
            assert (after[0], after[1], after[3]) == (
                before[0],
                before[1],
                before[3],
            )

    asyncio.run(run())


def test_confirmation(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            values = {
                "action": "configure",
                "library_id": env.movies.id,
                "rename_template": MOVIE_TEMPLATE,
            }
            reviewed = await review(
                env.owner, env.movies.id, rename_template=MOVIE_TEMPLATE
            )

            unconfirmed = await apply(env.owner, reviewed, values, confirmed=False)

            assert unconfirmed["confirmation_required"] and not unconfirmed["applied"]
            assert unconfirmed["request_id"] == reviewed["request_id"]
            assert unconfirmed["naming"]["samples"]
            assert (await MediaLib.get(id=env.movies.id)).rename_template is None
            assert not await MediaChangeReceipt.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "edit",
    [
        "language",
        "template",
        "order",
        "removed",
        "bound_published",
        "new_republished",
        "new_unpublished",
        "new_deleted",
        "mode",
    ],
)
def test_stale(monkeypatch, tmp_path, edit):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await stored_draft(env)
            await scrapers()
            values = {
                "action": "configure",
                "library_id": env.movies.id,
                "add_workflow_ids": [20],
            }
            reviewed = await review(env.owner, env.movies.id, add_workflow_ids=[20])
            if edit == "language":
                await MediaLib.filter(id=env.movies.id).update(language=Language.ZH_CN)
            elif edit == "template":
                await MediaLib.filter(id=env.movies.id).update(
                    rename_template="{{title}}"
                )
            elif edit == "order":
                await FlowTriggerService.bind_triggers(
                    "ingest",
                    env.movies.id,
                    [
                        SimpleNamespace(graph_id=15, asynchronous=True),
                        SimpleNamespace(graph_id=7, asynchronous=False),
                    ],
                )
            elif edit == "removed":
                await FlowTrigger.filter(rel_id=env.movies.id, graph_id=15).delete()
            elif edit == "mode":
                await FlowTrigger.filter(rel_id=env.movies.id, graph_id=7).update(
                    asynchronous=True
                )
            elif edit == "bound_published":
                await FlowGraph.filter(id=7).update(revision=2)
            elif edit == "new_republished":
                await FlowGraph.filter(id=20).update(revision=2)
            elif edit == "new_deleted":
                await FlowGraph.filter(id=20).delete()
            else:
                await FlowGraph.filter(id=20).update(state="draft")
            before = await rows_of(env.movies)

            result = await apply(env.owner, reviewed, values)

            assert result["stale"] and not result["applied"]
            assert await rows_of(env.movies) == before
            assert not await MediaChangeReceipt.exists()

    asyncio.run(run())


def test_edit_during_apply(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            values = {"action": "configure", "library_id": 60, "language": "zh-CN"}
            reviewed = await review(env.owner, 60, language="zh-CN")
            checked = service.MediaChangeService._checked
            calls = []

            async def edited(body, current, base):
                refused = await checked(body, current, base)
                calls.append(refused)
                # the library editor saves right after the first recheck
                if len(calls) == 1:
                    await MediaLib.filter(id=60).update(language=Language.EN_US)
                return refused

            monkeypatch.setattr(service.MediaChangeService, "_checked", edited)

            result = await apply(env.owner, reviewed, values)

            assert result["stale"] and len(calls) == 2
            assert (await MediaLib.get(id=60)).language == Language.EN_US
            assert not await MediaChangeReceipt.exists()

    asyncio.run(run())


def test_deleted_library(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            values = {"action": "configure", "library_id": 60, "language": "zh-CN"}
            reviewed = await review(env.owner, 60, language="zh-CN")
            other = await review(env.owner, 61, language="zh-CN")
            await apply(
                env.owner,
                other,
                {"action": "configure", "library_id": 61, "language": "zh-CN"},
            )
            await MediaItem.filter(lib_id=60).delete()
            await MediaLib.filter(id=60).delete()

            result = await apply(env.owner, reviewed, values)
            await MediaItem.filter(lib_id=61).delete()
            await MediaLib.filter(id=61).delete()
            replay = await apply(
                env.owner,
                other,
                {"action": "configure", "library_id": 61, "language": "zh-CN"},
            )

            assert result["outcome"] == "absent" and not result["applied"]
            assert replay["replayed"] and replay["current"] is None
            assert replay["outcome"] == "configured"

    asyncio.run(run())


def test_unrelated_edit_survives(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            values = {"action": "configure", "library_id": 60, "language": "zh-CN"}
            reviewed = await review(env.owner, 60, language="zh-CN")
            # other library settings are outside this review
            await MediaLib.filter(id=60).update(name="Films", danmaku_ttl=7)

            applied = await apply(env.owner, reviewed, values)
            lib = await MediaLib.get(id=60)

            assert applied["outcome"] == "configured"
            assert (lib.name, lib.danmaku_ttl, lib.language) == (
                "Films",
                7,
                Language.ZH_CN,
            )

    asyncio.run(run())


@pytest.mark.parametrize("field", ["library", "language", "add", "action"])
def test_conflict(monkeypatch, tmp_path, field):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            values = {"action": "configure", "library_id": 60, "language": "zh-CN"}
            reviewed = await review(env.owner, 60, language="zh-CN")
            altered = {
                "library": {**values, "library_id": 61},
                "language": {**values, "language": "en-US"},
                "add": {**values, "add_workflow_ids": [8]},
                "action": {"action": "scan", "library_id": 60},
            }[field]

            result = await apply(env.owner, reviewed, altered)

            assert result["conflict"] and not result["applied"]
            assert (await MediaLib.get(id=60)).language is None

    asyncio.run(run())


def test_concurrent(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await scrapers()
            values = {
                "action": "configure",
                "library_id": 60,
                "add_workflow_ids": [20],
            }
            reviewed = await review(env.owner, 60, add_workflow_ids=[20])

            results = await asyncio.gather(
                *(apply(env.owner, reviewed, values) for _ in range(3))
            )

            fresh = [result for result in results if result.get("replayed") is False]
            assert len(fresh) == 1 and fresh[0]["outcome"] == "configured"
            assert all(
                result.get("replayed") or result.get("pending")
                for result in results
                if result is not fresh[0]
            )
            assert [row[0] for row in await rows_of(env.movies)] == [7, 20]
            assert await MediaChangeReceipt.all().count() == 1

    asyncio.run(run())


def test_competing_reviews(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await scrapers()
            first = await review(env.owner, 60, add_workflow_ids=[20])
            second = await review(env.owner, 60, add_workflow_ids=[21])

            results = await asyncio.gather(
                apply(
                    env.owner,
                    first,
                    {"action": "configure", "library_id": 60, "add_workflow_ids": [20]},
                ),
                apply(
                    env.owner,
                    second,
                    {"action": "configure", "library_id": 60, "add_workflow_ids": [21]},
                ),
            )

            # the second recheck sees the first save instead of overwriting it
            assert sorted(bool(result.get("applied")) for result in results) == [
                False,
                True,
            ]
            refused = next(result for result in results if not result.get("applied"))
            assert refused.get("stale") or refused.get("pending")
            assert len(await rows_of(env.movies)) == 2

    asyncio.run(run())


def test_library_lock(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            values = {"action": "configure", "library_id": 60, "language": "zh-CN"}
            reviewed = await review(env.owner, 60, language="zh-CN")

            # another worker is saving a different review of this library
            with submission_lock("media-library:60"):
                held = await apply(env.owner, reviewed, values)
            unchanged = await MediaLib.get(id=60)
            applied = await apply(env.owner, reviewed, values)

            assert held["pending"] and unchanged.language is None
            assert applied["outcome"] == "configured"

    asyncio.run(run())


def test_replay_after_later_edit(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await scrapers()
            values, reviewed, applied = await configure_library(
                env.owner, 60, language="zh-CN", add_workflow_ids=[20]
            )
            # the library editor saves over the configuration afterwards
            await MediaLibService.upsert(
                MediaLibUpsert(
                    id=60,
                    name="Movies",
                    language="en-US",
                    triggers=[{"graph_id": 21, "asynchronous": True}],
                )
            )

            replay = await apply(env.owner, reviewed, values)
            lib = await MediaLib.get(id=60)

            assert applied["outcome"] == "configured"
            assert replay["replayed"] and replay["outcome"] == "configured"
            assert replay["settings"] == applied["settings"]
            # the later edit is reported and left in place
            assert replay["current"] == {
                "language": "en-US",
                "rename_template": None,
                "workflow_ids": [21],
            }
            assert lib.language == Language.EN_US
            assert await rows_of(lib) == [(21, 1, True)]

    asyncio.run(run())


def test_rollback(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await scrapers()
            values = {
                "action": "configure",
                "library_id": 60,
                "language": "zh-CN",
                "add_workflow_ids": [20],
            }
            reviewed = await review(
                env.owner, 60, language="zh-CN", add_workflow_ids=[20]
            )
            create = MediaChangeReceipt.create
            monkeypatch.setattr(
                MediaChangeReceipt, "create", AsyncMock(side_effect=OSError("disk"))
            )

            failed = await apply(env.owner, reviewed, values)
            lib = await MediaLib.get(id=60)
            kept = await rows_of(lib)
            monkeypatch.setattr(MediaChangeReceipt, "create", create)
            repeated = await apply(env.owner, reviewed, values)

            # settings, bindings and receipt commit together or not at all
            assert failed["failed"] and failed["applied"] is False
            assert lib.language is None and kept == [(7, 1, False)]
            assert repeated["outcome"] == "configured" and not repeated["replayed"]
            assert (await MediaLib.get(id=60)).language == Language.ZH_CN

    asyncio.run(run())


def test_uncertain(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            values = {"action": "configure", "library_id": 60, "language": "zh-CN"}
            reviewed = await review(env.owner, 60, language="zh-CN")
            original = MediaChangeReceipt.get_or_none
            create = MediaChangeReceipt.create
            calls = []

            async def unreadable(**fields):
                calls.append(fields)
                if len(calls) > 1:
                    raise OSError("database unavailable")
                return await original(**fields)

            monkeypatch.setattr(
                MediaChangeReceipt, "create", AsyncMock(side_effect=OSError("lost"))
            )
            monkeypatch.setattr(MediaChangeReceipt, "get_or_none", unreadable)

            uncertain = await apply(env.owner, reviewed, values)

            assert uncertain["outcome"] == "uncertain" and uncertain["applied"] is None
            assert "rolled-back save can be retried" in uncertain["message"]
            assert (await MediaLib.get(id=60)).language is None
            monkeypatch.setattr(MediaChangeReceipt, "get_or_none", original)
            monkeypatch.setattr(MediaChangeReceipt, "create", create)

            recovered = await apply(env.owner, reviewed, values)
            repeated = await apply(env.owner, reviewed, values)

            assert recovered["outcome"] == "configured" and not recovered["replayed"]
            assert repeated["replayed"] and repeated["current"]["language"] == "zh-CN"
            assert await MediaChangeReceipt.all().count() == 1

    asyncio.run(run())


def test_cancelled(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await scrapers()
            values = {
                "action": "configure",
                "library_id": 60,
                "add_workflow_ids": [20],
            }
            reviewed = await review(env.owner, 60, add_workflow_ids=[20])
            entered, release = asyncio.Event(), asyncio.Event()
            bind = FlowTriggerService.bind_triggers

            async def slow(*args):
                entered.set()
                await release.wait()
                await bind(*args)

            monkeypatch.setattr(FlowTriggerService, "bind_triggers", slow)

            caller = asyncio.create_task(apply(env.owner, reviewed, values))
            await entered.wait()
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            while service._RUNNING:
                await asyncio.sleep(0.01)
            # the lost response is read back instead of saving again
            repeated = await apply(env.owner, reviewed, values)

            assert repeated["replayed"] and repeated["outcome"] == "configured"
            assert [row[0] for row in await rows_of(env.movies)] == [7, 20]

    asyncio.run(run())


def test_permissions(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            values = {"action": "configure", "library_id": 60, "language": "zh-CN"}
            reviewed = await review(env.owner, 60, language="zh-CN")
            demoted = SimpleNamespace(
                id=env.owner.id,
                role=UserRole.USER,
                perms=Permissions(media_lib_ids=[60]),
            )
            with pytest.raises(ForbiddenException):
                await review(env.reader, 60)
            with pytest.raises(ForbiddenException):
                await apply(demoted, reviewed, values)
            applied = await apply(env.owner, reviewed, values)
            receipt = await MediaChangeReceipt.get(claimed=True)
            with pytest.raises(ForbiddenException):
                await service.MediaChangeService._replay(receipt, demoted)

            assert applied["outcome"] == "configured"

    asyncio.run(run())


def test_arguments():
    for values in (
        {"library_id": 0},
        {"library_id": 60, "dir": "/tmp"},
        {"library_id": 60, "language": "fr-FR"},
        {"library_id": 60, "add_workflow_ids": [0]},
        {"library_id": 60, "rename_template": "x" * 1025},
        {"library_id": 60, "replace_workflows": [{"workflow_id": 7}]},
    ):
        with pytest.raises(ValidationError):
            LibraryConfig.model_validate(values)


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_conversation(monkeypatch, tmp_path, verbose):
    streams, offered, seen = [], [], {}

    def offer(body, *calls):
        offered.append(names(body) & tools.MEDIA_CHANGE_TOOLS)
        return list(calls)

    def confirm(body):
        return offer(
            body,
            (
                "apply_media_change",
                {"request_id": latest_request(body), "confirmed": True},
            ),
        )

    def first(body):
        # the library page identifies the target
        seen.update(page_of(body))
        return offer(body, ("list_media_libraries", {}))

    steps = [
        first,
        lambda body: offer(
            body, ("review_library_config", {"library_id": seen["resource_id"]})
        ),
        lambda body: offer(body),
        # the user chose a workflow and template in the next turn
        lambda body: offer(
            body,
            (
                "review_library_config",
                {
                    "library_id": seen["resource_id"],
                    "add_workflow_ids": [20],
                    "rename_template": MOVIE_TEMPLATE,
                },
            ),
        ),
        confirm,
        lambda body: offer(body),
        # the user confirms, but the response after saving is lost
        confirm,
        lost,
        # repeating reports the outcome without saving again
        confirm,
        lambda body: offer(body),
    ]
    configure(monkeypatch, scripted(steps, streams, verbose=verbose))
    context = {
        "page_type": "media_library",
        "route": "/medialibs/60",
        "resource_id": 60,
    }

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await scrapers()
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            turns = [
                await converse(
                    conversation["id"], env.owner, prompt, context=page, key=key
                )
                for key, prompt, page in (
                    ("a", "Change this library's scraper settings.", context),
                    ("b", "Add Scraper 00 and rename by title and year.", None),
                    ("c", "Yes, save it.", None),
                    ("d", "Did that work? Save it.", None),
                )
            ]
            stored = await AssistantTurn.filter(conversation_id=conversation["id"])
            lib = await MediaLib.get(id=60)
            bound = await rows_of(lib)
            events = await MediaEvent.all().count()
            return turns, stored, lib, bound, events

    turns, stored, lib, bound, events = asyncio.run(run())
    (_, read), (_, reviewed), (_, saved), (_, repeated) = turns

    assert [done.event for done, _ in turns] == ["done", "done", "error", "done"]
    # configuration appears after a media read, then only its own kind continues
    assert offered[0] == set() and offered[1] == tools.MEDIA_CHANGE_TOOLS
    assert offered[3] == {"review_library_config", "apply_media_change"}
    assert offered[6] == {"review_library_config", "apply_media_change"}
    assert not read[1]["data"]["valid"] and read[1]["data"]["bindings"]
    preview, same_turn = reviewed[0]["data"], reviewed[1]["data"]
    assert preview["valid"] and preview["naming"]["samples"]
    assert same_turn["confirmation_required"] and not same_turn["applied"]
    assert saved[0]["data"]["outcome"] == "configured"
    assert repeated[0]["data"]["replayed"]
    assert lib.rename_template == MOVIE_TEMPLATE
    assert [row[0] for row in bound] == [7, 20] and events == 0
    # the library and workflows are recorded as sources
    for turn in stored:
        assert "media_lib:60" in turn.resource_ids
    assert any(20 in turn.resource_ids for turn in stored)
    assert all(500 not in turn.resource_ids for turn in stored)


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_missing_scraper_conversation(monkeypatch, tmp_path, verbose):
    streams, created = [], {}

    def review_new(body):
        return [
            (
                "review_library_config",
                {"library_id": 61, "add_workflow_ids": [created["id"]]},
            )
        ]

    steps = [
        lambda body: [("list_media_libraries", {})],
        lambda body: [("review_library_config", {"library_id": 61})],
        lambda body: [("get_workflow_reference", {"topic": "tv_scraper"})],
        lambda body: [
            (
                "create_workflow_draft",
                {
                    "name": "Requested TV scraper",
                    "category": "ingest",
                    "description": "Read show metadata from the supplied HTML source.",
                },
            )
        ],
        lambda body: None,
        # the same review refuses the draft until the user publishes it
        lambda body: [("list_media_libraries", {})],
        review_new,
        lambda body: None,
        lambda body: [("list_media_libraries", {})],
        review_new,
        lambda body: None,
        lambda body: [
            (
                "apply_media_change",
                {"request_id": latest_request(body), "confirmed": True},
            )
        ],
        lambda body: None,
    ]
    configure(monkeypatch, scripted(steps, streams, verbose=verbose))

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await FlowTrigger.filter(rel_id=61).delete()
            await FlowGraph.filter(id=8).update(state="draft")
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            _, generated = await converse(
                conversation["id"], env.owner, "Set up a scraper for Shows.", key="a"
            )
            draft = generated[-1]["data"]
            created["id"] = draft["workflow_id"]
            stored_draft = await FlowGraph.get(id=created["id"])
            untouched = await rows_of(env.shows)
            _, early = await converse(
                conversation["id"], env.owner, "Bind that draft.", key="b"
            )
            # publication stays with the user, outside the assistant
            await FlowGraph.filter(id=created["id"]).update(
                state="published", revision=1, definition=tv_scraper()
            )
            _, rechecked = await converse(
                conversation["id"], env.owner, "I published it. Bind it.", key="c"
            )
            done, saved = await converse(
                conversation["id"], env.owner, "Yes, bind it.", key="d"
            )
            return (
                generated,
                stored_draft,
                untouched,
                early,
                rechecked,
                done,
                saved,
                await rows_of(env.shows),
                created["id"],
            )

    generated, draft, untouched, early, rechecked, done, saved, bound, graph_id = (
        asyncio.run(run())
    )

    assert "create_workflow_draft" in generated[1]["data"]["next"]
    assert draft.state == "draft" and draft.definition is None
    assert untouched == []
    assert not early[-1]["data"]["valid"]
    assert "Publish" in str(early[-1]["data"]["issues"])
    assert "request_id" not in early[-1]["data"]
    assert rechecked[-1]["data"]["valid"]
    assert done.event == "done" and saved[0]["data"]["outcome"] == "configured"
    assert [row[0] for row in bound] == [graph_id]


def test_superseded(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            turn = await AssistantTurn.create(
                conversation_id=conversation["id"],
                request_id="reviews",
                status="complete",
                prompt="Configure, then scan.",
            )
            valid = await review(env.owner, 60, language="zh-CN")
            records = [
                (
                    "review_library_config",
                    {"library_id": 60, "language": "zh-CN"},
                    {"ok": True, "data": valid},
                ),
                (
                    "review_library_scan",
                    {"library_id": 60},
                    {"ok": False, "error": {"code": "ai_timeout"}},
                ),
            ]
            for index, (name, args, result) in enumerate(records):
                await AssistantToolCall.create(
                    turn_id=turn.id,
                    call_id=f"review-{index}",
                    name=name,
                    arguments=dumps(args).decode(),
                    status="complete" if result["ok"] else "error",
                    result=result,
                )

            confirmed = await tools.apply_media_change(
                tools.MediaConfirm(request_id=valid["request_id"], confirmed=True),
                conversation["id"],
                env.owner,
            )

            assert confirmed["confirmation_required"] and not confirmed["applied"]
            assert "newer review" in confirmed["message"]
            assert (await MediaLib.get(id=60)).language is None

    asyncio.run(run())


def test_provenance(monkeypatch, tmp_path):
    steps = [
        lambda body: [("review_library_config", {"library_id": 61})],
        lambda body: None,
    ]
    configure(monkeypatch, scripted(steps, []))

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            owner = env.owner
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            done, results = await converse(
                conversation["id"], owner, "Show the shows library settings.", key="a"
            )
            turn = await AssistantTurn.get(request_id="a")

            assert done.event == "done" and results[0]["ok"]
            assert set(turn.resource_ids) == {"media_lib:61", env.tv_graph.id}
            # the turn's history follows current permissions
            owner.role = UserRole.USER
            owner.perms = Permissions(media_lib_ids=[60])
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": turn.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="history",
            )
            refused = await tools.execute_tool(
                "review_library_config",
                dumps({"library_id": 60}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="review",
            )
            assert denied["error"]["code"] == "permission_denied"
            assert refused["error"]["code"] == "forbidden"

    asyncio.run(run())


def test_mcp(monkeypatch, tmp_path):
    async def run():
        async with application() as app:
            app.ctx.flow_engine = Engine()
            # other modules may leave apps registered; services read this one
            monkeypatch.setattr("sanic.Sanic.get_app", lambda *args, **kwargs: app)
            stored = await store(tmp_path)
            app.ctx.lib_watcher = monitor_for(stored.movies, stored.shows)
            await scrapers()
            admin = app.shared_ctx.sessions["admin"]
            change = {
                "action": "configure",
                "library_id": 60,
                "add_workflow_ids": [20],
                "language": "zh-CN",
                "rename_template": MOVIE_TEMPLATE,
            }

            denied = await call(app, "review_library_config", {"change": change})
            reviewed = await call(
                app, "review_library_config", {"change": change}, token="admin"
            )
            data = reviewed["structuredContent"]
            shared = await tools.execute_tool(
                "review_library_config",
                dumps({k: v for k, v in change.items() if k != "action"}).decode(),
                admin,
                conversation_id=1,
                call_id="parity",
            )
            arguments = {
                "application": {
                    "request_id": data["request_id"],
                    "change": change,
                    "revision": data["revision"],
                    "confirmed": True,
                }
            }
            read_only = await call(app, "apply_media_change", arguments, token="admin")
            write = (
                await MCPAccessTokenService.create(
                    admin.id,
                    MCPAccessTokenCreate(
                        name="libraries", scope=MCPTokenScope.READ_WRITE
                    ),
                )
            )["token"]
            altered = await call(
                app,
                "apply_media_change",
                {
                    "application": {
                        **arguments["application"],
                        "change": {**change, "language": None},
                    }
                },
                token=write,
            )
            saved = await call(app, "apply_media_change", arguments, token=write)
            replay = await call(app, "apply_media_change", arguments, token=write)

            assert denied["isError"] and "not allowed" in str(denied)
            assert data["valid"] and data["naming"]["samples"]
            assert read_only["isError"] and "read-only" in str(read_only)
            assert altered["structuredContent"]["conflict"]
            assert saved["structuredContent"]["outcome"] == "configured"
            assert replay["structuredContent"]["replayed"]
            # assistant and MCP share the review apart from request identities
            data.pop("request_id")
            shared["data"].pop("request_id")
            assert shared["data"] == data
            lib = await MediaLib.get(id=60)
            assert lib.language == Language.ZH_CN
            assert [row[0] for row in await rows_of(lib)] == [7, 20]

    asyncio.run(run())


def test_sizes_and_compaction(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await scrapers(12)
            for index in range(8):
                await FlowTrigger.create(
                    category="ingest",
                    rel_id=env.movies.id,
                    graph_id=20 + index,
                    priority=10 + index,
                )
            full = await review(
                env.owner,
                env.movies.id,
                add_workflow_ids=[28, 29],
                language="zh-CN",
                rename_template=MOVIE_TEMPLATE,
            )
            shows = await review(env.owner, env.shows.id, rename_template=TV_TEMPLATE)
            return full, shows

    full, shows = asyncio.run(run())

    # results stay bounded with many workflows and naming samples
    assert len(dumps(full)) < 6000 and len(dumps(shows)) < 3500
    for data in (full, shows):
        source = {
            "id": "review",
            "name": "review_library_config",
            "ok": True,
            "data": data,
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
        compacted = history.compact_exchanges(messages, 1, 11000)
        fact = loads(compacted[0]["content"].split("\n", 1)[1])["tools"][0]
        # the active review keeps what the reply must explain before confirmation
        for key in ("notes", "naming", "proposed_bindings", "bindings", "scope"):
            if data.get(key) is not None:
                assert fact["data"][key] == data[key], key
        assert fact["data"]["request_id"] == data["request_id"]
        assert len(dumps(compacted)) < 11000
    # an earlier turn's summary keeps the identities and the requested settings
    summary = tool_facts(
        {"id": "review", "name": "review_library_config", "ok": True, "data": full}
    )["data"]
    assert summary["request_id"] == full["request_id"]
    assert summary["settings"] == full["settings"]


@pytest.mark.parametrize("replay", [False, True])
def test_compacted_details(monkeypatch, tmp_path, replay):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            for graph in await scrapers(4):
                await FlowGraph.filter(id=graph.id).update(
                    name="名称" * 25 + str(graph.id)
                )
            await MediaLib.filter(id=60).update(rename_template="{{title}}" * 79)
            values = {
                "action": "configure",
                "library_id": 60,
                "add_workflow_ids": [20, 21, 22, 23],
                "rename_template": "{{title}}" * 80,
            }
            reviewed = await review(env.owner, **values)
            assert reviewed["valid"]
            if replay:
                await apply(env.owner, reviewed, values)
                return await apply(env.owner, reviewed, values)
            return reviewed

    reviewed = asyncio.run(run())
    source = {
        "id": "review",
        "name": "apply_media_change" if replay else "review_library_config",
        "ok": True,
        "data": reviewed,
    }
    compacted = history.compact_exchanges(
        [
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
        ],
        1,
        11000,
    )
    data = loads(compacted[0]["content"].split("\n", 1)[1])["tools"][0]["data"]
    assert data["settings"] == reviewed["settings"]
    assert data["bindings_change"] == reviewed["bindings_change"]
    if replay:
        assert data["current"] == reviewed["current"]
    else:
        assert data["language"] == reviewed["language"]
        assert data["rename_template"] == reviewed["rename_template"]
    assert data["proposed_binding_order"] == reviewed["proposed_binding_order"]
    assert len(dumps(compacted)) < 11000


def test_empty_order_conflicts(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed, saved = await configure_library(
                env.owner, 60, language="zh-CN"
            )
            replayed = await apply(
                env.owner, reviewed, {**change, "order_workflow_ids": []}
            )

            assert saved["applied"]
            assert replayed["conflict"] and replayed["applied"] is False
            assert await rows_of(env.movies) == [(7, 1, False)]

    asyncio.run(run())


@pytest.mark.parametrize("count", [25, 55])
def test_binding_order_sample(monkeypatch, tmp_path, count):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            graphs = await scrapers(count - 1)
            original = [7, *[graph.id for graph in graphs]]
            await FlowTriggerService.bind_triggers(
                GraphCategory.INGEST,
                60,
                [GraphRef(graph_id=entry) for entry in original],
            )
            if count <= 50:
                order = list(reversed(original))
                reviewed = await review(env.owner, 60, order_workflow_ids=order)
            else:
                order = original[1:]
                reviewed = await review(env.owner, 60, remove_workflow_ids=[7])

            assert reviewed["valid"]
            assert reviewed["binding_order"] == original[:50]
            assert reviewed["proposed_binding_order"] == order[:50]
            assert reviewed["binding_count"] == count
            assert reviewed["proposed_binding_count"] == len(order)
            assert ("more_bindings" in reviewed) == (count > 50)
            assert len(reviewed["proposed_bindings"]) == 20
            source_ids = tools.media_change_workflows(reviewed)
            assert set(order[:50]) <= source_ids
            assert tools.media_change_workflows(
                {"current": {"workflow_ids": [47]}}
            ) == {47}

    asyncio.run(run())
