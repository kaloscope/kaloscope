"""Unit tests for reviewed library scans and movie or TV rescrapes."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sanic import Sanic

from app.core.ai import history, tools
from app.core.ai.documents import tool_facts
from app.core.exceptions import (
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.media import shelver, watcher
from app.core.media.common import ContentError
from app.models.assistant import AssistantToolCall, AssistantTurn, ConversationCreate
from app.models.flow import FlowGraph, FlowTrigger
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import (
    LibraryScan,
    LibType,
    MediaChangeApply,
    MediaChangeReceipt,
    MediaEvent,
    MediaItem,
    MediaLib,
    MediaRescrape,
    NFOType,
)
from app.models.user import Permissions, UserRole
from app.services import media_changes as service
from app.services.assistant_sessions import AssistantSessionService as Sessions
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import MODEL, database
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_changes import latest_request, lost
from tests.test_download_plan_ingestion import scraper
from tests.test_mcp import application, call
from tests.test_services_assistant import configure
from tests.test_tv_ingestion import tv_scraper
from tests.test_workflow_runs import converse, names, scripted

MOVIES = [
    {
        "title": "New Movie",
        "year": 2002,
        "site": "tmdb",
        "id": "201",
        "rating": 8.1,
        "plot": "A replacement plot that is long enough to be shortened. " * 4,
        "poster": "https://images.example.test/201.jpg",
    },
    {
        "title": "New Movie",
        "year": 1999,
        "site": "tmdb",
        "id": "202",
        "plot": "Remake.",
    },
    {"title": "", "year": 2002},
]
SHOWS = [
    {"title": "New Show", "year": 2020, "site": "tmdb", "id": "300", "season": 1},
    {"title": "Other Show", "year": 2021, "site": "tmdb", "id": "301", "season": 1},
]


class Engine:
    """Answer manual scraper searches like published ingest workflows."""

    def __init__(self):
        self.calls: list[tuple[int, dict]] = []
        self.fail_episode: int | None = None
        self.release: asyncio.Event | None = None

    async def execute(self, graph_id, bootparams=None, **kwargs):
        """Return candidates for the requested NFO type.

        Args:
            graph_id: Workflow being run.
            bootparams: Manual search parameters.
            **kwargs: Unused execution options.

        Returns:
            Candidate metadata like the end node's manual output.

        Raises:
            RuntimeError: For the configured failing episode.
        """
        self.calls.append((graph_id, dict(bootparams)))
        kind = bootparams["nfo_type"]
        if kind == NFOType.MOVIE:
            return [dict(candidate) for candidate in MOVIES]
        if kind == NFOType.TV_SHOW:
            return [dict(candidate) for candidate in SHOWS]
        if self.release is not None:
            await self.release.wait()
        episode = bootparams.get("episode")
        if episode is not None and episode == self.fail_episode:
            raise RuntimeError("episode source unavailable")
        if episode is None:
            return []
        season = bootparams.get("season")
        return [{"title": f"Episode {episode}", "season": season, "episode": episode}]


async def write_nfo(kind, path, data):
    """Write an NFO through the real renderer.

    Args:
        kind: NFO type.
        path: Destination file.
        data: Candidate fields.
    """
    assert await shelver.gen_nfo(kind, str(path), data, overwrite=True)


async def store(root: Path):
    """Store movie, TV and reading libraries with bound scrapers and items.

    Args:
        root: Isolated directory for library roots.

    Returns:
        Libraries, workflows and items.
    """
    for name in ("movies", "shows", "books"):
        (root / name).mkdir()
    movies = await MediaLib.create(
        id=60,
        lib_type=LibType.MOVIE,
        dir=str(root / "movies"),
        name="Movies",
        priority=1,
    )
    shows = await MediaLib.create(
        id=61,
        lib_type=LibType.TV_SHOW,
        dir=str(root / "shows"),
        name="Shows",
        priority=2,
    )
    books = await MediaLib.create(
        id=62, lib_type=LibType.NOVEL, dir=str(root / "books"), name="Books", priority=3
    )
    movie_graph = await FlowGraph.create(
        id=7,
        name="Movie scraper",
        category="ingest",
        state="published",
        definition=scraper(),
        revision=1,
    )
    tv_graph = await FlowGraph.create(
        id=8,
        name="TV scraper",
        category="ingest",
        state="published",
        definition=tv_scraper(),
        revision=1,
    )
    await FlowTrigger.create(
        category="ingest", rel_id=movies.id, graph=movie_graph, priority=1
    )
    await FlowTrigger.create(
        category="ingest", rel_id=shows.id, graph=tv_graph, priority=2
    )

    folder = root / "movies" / "Old Movie"
    folder.mkdir()
    (folder / "Old Movie.mkv").write_bytes(b"movie")
    await write_nfo(
        NFOType.MOVIE,
        folder / "Old Movie.nfo",
        {
            "title": "Old Movie",
            "year": 2001,
            "site": "tmdb",
            "id": "101",
            "plot": "Old.",
        },
    )
    movie = await MediaItem.create(
        id=500,
        lib=movies,
        path=str(folder),
        dir=str(folder),
        name="Old Movie",
        nfo_path=str(folder / "Old Movie.nfo"),
        title="Old Movie",
        year=2001,
        unique_id="101",
        nfo_source="tmdb",
    )
    await MediaItem.create(
        id=501,
        lib=movies,
        parent=movie,
        path=str(folder / "Old Movie.mkv"),
        dir=str(folder),
        name="Old Movie",
    )

    show_dir = root / "shows" / "Old Show"
    show_dir.mkdir()
    await write_nfo(
        NFOType.TV_SHOW,
        show_dir / "Old Show.nfo",
        {"title": "Old Show", "year": 2019, "site": "tmdb", "id": "300", "season": 1},
    )
    show = await MediaItem.create(
        id=600,
        lib=shows,
        path=str(show_dir),
        dir=str(show_dir),
        name="Old Show",
        nfo_path=str(show_dir / "Old Show.nfo"),
        title="Old Show",
        year=2019,
        season=1,
        unique_id="300",
        nfo_source="tmdb",
    )
    episodes = []
    for index, (name, season, episode) in enumerate(
        [("S01E01", 1, 1), ("S01E02", 1, 2), ("Extra", None, None)]
    ):
        video = show_dir / f"{name}.mkv"
        video.write_bytes(b"episode")
        nfo = None
        if name == "S01E01":
            nfo = show_dir / f"{name}.nfo"
            await write_nfo(
                NFOType.EPISODE, nfo, {"title": "Pilot", "season": 1, "episode": 1}
            )
        episodes.append(
            await MediaItem.create(
                id=601 + index,
                lib=shows,
                parent=show,
                path=str(video),
                dir=str(show_dir),
                name=name,
                season=season,
                episode=episode,
                nfo_path=str(nfo) if nfo else None,
            )
        )
    return SimpleNamespace(
        movies=movies,
        shows=shows,
        books=books,
        movie_graph=movie_graph,
        tv_graph=tv_graph,
        movie=movie,
        show=show,
        episodes=episodes,
    )


def monitor_for(*libraries):
    """Build a watcher that observes the given libraries in this process.

    Args:
        *libraries: Libraries this worker observes.

    Returns:
        Watcher with in-process shared state.
    """
    monitor = watcher.LibWatcher(Mock(spec=Sanic))
    monitor.__dict__["_observers"] = {lib.dir: (None, Queue()) for lib in libraries}
    monitor.__dict__["_scanning_paths"] = []
    monitor.__dict__["_watcher_actions"] = {}
    return monitor


@asynccontextmanager
async def media(monkeypatch, tmp_path):
    """Open libraries behind a fake scraper engine and a real watcher.

    Args:
        monkeypatch: Pytest patch helper.
        tmp_path: Isolated directory.

    Yields:
        Accounts, engine, watcher and stored media.
    """
    engine = Engine()
    app = SimpleNamespace(config={}, ctx=SimpleNamespace(flow_engine=engine))
    monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
    async with database() as (owner, other):
        stored = await store(tmp_path)
        app.ctx.lib_watcher = monitor_for(stored.movies, stored.shows)
        reader = SimpleNamespace(
            id=other.id, role=UserRole.USER, perms=Permissions(media_lib_ids=[60, 61])
        )
        yield SimpleNamespace(
            owner=owner,
            reader=reader,
            engine=engine,
            watcher=app.ctx.lib_watcher,
            **vars(stored),
        )


async def scan_review(user, library_id):
    """Review one scan through the shared service.

    Args:
        user: Calling account.
        library_id: Library to scan.

    Returns:
        Shared review.
    """
    return await service.MediaChangeService.review_scan(
        LibraryScan(library_id=library_id), user
    )


async def rescrape_review(user, **change):
    """Review one rescrape through the shared service.

    Args:
        user: Calling account.
        **change: Rescrape fields.

    Returns:
        Shared review.
    """
    return await service.MediaChangeService.review_rescrape(
        MediaRescrape.model_validate(change), user
    )


async def apply(user, reviewed, change, *, confirmed=True):
    """Apply a review through the shared service.

    Args:
        user: Calling account.
        reviewed: Review whose request ID and revision are confirmed.
        change: Reviewed change, possibly altered by the caller.
        confirmed: The user's approval.

    Returns:
        Shared outcome.
    """
    return await service.MediaChangeService.apply_change(
        MediaChangeApply(
            request_id=reviewed["request_id"],
            change=change,
            revision=reviewed["revision"],
            confirmed=confirmed,
        ),
        user,
    )


async def chosen(user, item_id, index=0, **values):
    """List candidates, then review the one at `index`.

    Args:
        user: Calling account.
        item_id: Item to rescrape.
        index: Position of the chosen candidate.
        **values: Other search inputs.

    Returns:
        The reviewed change and its review.
    """
    listed = await rescrape_review(user, item_id=item_id, **values)
    change = {
        "action": "rescrape",
        "item_id": item_id,
        **values,
        "candidate": listed["candidates"][index]["key"],
    }
    return change, await rescrape_review(user, **change)


def key_of(user, reviewed):
    """Derive the account-scoped receipt key of a reviewed request.

    Args:
        user: Reviewing account.
        reviewed: Review with its request ID.

    Returns:
        Receipt key.
    """
    return hashlib.sha256(f"{user.id}:{reviewed['request_id']}".encode()).hexdigest()


def title_in(path):
    """Read the title stored in an NFO file.

    Args:
        path: NFO path.

    Returns:
        Its title, or `None` when missing.
    """
    lib_type = LibType.MOVIE if str(path).endswith("Old Movie.nfo") else LibType.TV_SHOW
    meta = shelver.read_nfo(lib_type, path)[0]
    return meta.title if meta else None


def test_scan_review(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            reviewed = await scan_review(env.owner, env.movies.id)
            books = await scan_review(env.owner, env.books.id)
            with pytest.raises(NotFoundException):
                await scan_review(env.owner, 99)
            with pytest.raises(ForbiddenException):
                await scan_review(env.reader, env.movies.id)

            assert reviewed["valid"] and reviewed["request_id"]
            assert reviewed["bindings"] == [
                {"workflow_id": env.movie_graph.id, "workflow_name": "Movie scraper"}
            ]
            assert not reviewed["scanning"] and reviewed["queued_events"] == 0
            assert any("no files are deleted" in note for note in reviewed["notes"])
            assert any("unknown until it finishes" in n for n in reviewed["notes"])
            assert not books["valid"] and "request_id" not in books
            # reviewing starts nothing
            assert not await MediaEvent.exists()
            assert not await MediaChangeReceipt.exists()

    asyncio.run(run())


@pytest.mark.parametrize("observed", [True, False], ids=["inline", "delegated"])
def test_scan(monkeypatch, tmp_path, observed):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            new = Path(env.movies.dir) / "New Film (2024)"
            new.mkdir()
            (new / "New Film (2024).mkv").write_bytes(b"film")
            if not observed:
                env.watcher._observers.pop(env.movies.dir)
            reviewed = await scan_review(env.owner, env.movies.id)
            change = {"action": "scan", "library_id": env.movies.id}

            applied = await apply(env.owner, reviewed, change)
            events = await MediaEvent.filter(lib_id=env.movies.id).values_list(
                "src_path", flat=True
            )
            repeated = await apply(env.owner, reviewed, change)

            assert applied["applied"] and not applied["replayed"]
            assert applied["path"] == f"/medialibs/{env.movies.id}"
            if observed:
                # files were compared and the new film queued for ingestion
                assert applied["outcome"] == "scanned"
                assert str(new / "New Film (2024).mkv") in events
                assert not env.watcher.is_scanning(env.movies.dir)
            else:
                # the observing worker scans it later
                assert applied["outcome"] == "submitted" and not events
                assert env.watcher._watcher_actions == {
                    env.movies.dir: watcher.LibAction.SCAN
                }
            assert repeated["replayed"] and repeated["outcome"] == applied["outcome"]
            assert await MediaEvent.filter(lib_id=env.movies.id).count() == len(events)
            assert (new / "New Film (2024).mkv").exists()

    asyncio.run(run())


def test_duplicate_scan(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            reviewed = await scan_review(env.owner, env.movies.id)
            env.watcher._scanning_paths.append(env.movies.dir)
            change = {"action": "scan", "library_id": env.movies.id}

            refused = await apply(env.owner, reviewed, change)
            env.watcher._scanning_paths.clear()
            repeated = await apply(env.owner, reviewed, change)

            assert refused["outcome"] == "already_scanning"
            assert refused["applied"] is False
            # the recorded refusal is replayed; a new scan needs a new review
            assert repeated["replayed"] and repeated["outcome"] == "already_scanning"
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ContentError("media_source_unavailable"), "uncertain"),
        (KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED), "uncertain"),
        (RuntimeError("enumeration stopped"), "uncertain"),
    ],
)
def test_scan_failure(monkeypatch, tmp_path, error, outcome):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            reviewed = await scan_review(env.owner, env.movies.id)
            change = {"action": "scan", "library_id": env.movies.id}
            scan = AsyncMock(side_effect=error)
            monkeypatch.setattr(env.watcher, "scan_directory", scan)

            first = await apply(env.owner, reviewed, change)
            scan.side_effect = None
            second = await apply(env.owner, reviewed, change)

            # the exception type cannot prove that enumeration or recovery did nothing
            assert first["outcome"] == outcome and first["applied"] is None
            assert second["replayed"] and second["outcome"] == outcome
            assert scan.await_count == 1

    asyncio.run(run())


@pytest.mark.parametrize("edit", ["workflow", "definition", "language"])
def test_scan_stale(monkeypatch, tmp_path, edit):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            reviewed = await scan_review(env.owner, env.movies.id)
            if edit == "workflow":
                await FlowGraph.filter(id=env.movie_graph.id).update(revision=2)
            elif edit == "definition":
                await FlowGraph.filter(id=env.movie_graph.id).update(
                    definition={**scraper(), "review_test": True}
                )
            else:
                await MediaLib.filter(id=env.movies.id).update(language="zh-CN")

            result = await apply(
                env.owner, reviewed, {"action": "scan", "library_id": env.movies.id}
            )

            assert result.get("stale") and not result["applied"]
            assert not await MediaChangeReceipt.filter(claimed=True).exists()
            assert not await MediaEvent.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "error",
    [
        ContentError("media_source_unavailable"),
        KaloscopeException(ErrorCode.HTTP_REQUEST_FAILED),
    ],
)
def test_scan_partial_failure(monkeypatch, tmp_path, error):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            reviewed = await scan_review(env.owner, env.movies.id)
            scan = env.watcher.scan_directory

            async def interrupted(*args, **kwargs):
                await scan(*args, **kwargs)
                raise error

            attempted = AsyncMock(side_effect=interrupted)
            monkeypatch.setattr(env.watcher, "scan_directory", attempted)
            change = {"action": "scan", "library_id": env.movies.id}

            first = await apply(env.owner, reviewed, change)
            second = await apply(env.owner, reviewed, change)

            assert first.get("outcome") == "uncertain"
            assert second["replayed"] and second["outcome"] == "uncertain"
            assert attempted.await_count == 1

    asyncio.run(run())


def test_explicit_workflow_beyond_list(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            for index in range(11):
                graph = await FlowGraph.create(
                    name=f"Additional scraper {index:02}",
                    category="ingest",
                    state="published",
                    definition=scraper(),
                )

            result = await rescrape_review(
                env.owner, item_id=env.movie.id, workflow_id=graph.id
            )

            assert result.get("workflow_id") == graph.id
            assert result.get("candidates")
            assert len(result["workflows"]) <= 10

    asyncio.run(run())


def test_candidates(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            before = Path(env.movie.nfo_path).read_bytes()
            listed = await rescrape_review(env.owner, item_id=env.movie.id)
            untitled = await MediaItem.filter(id=env.movie.id).update(title=None)

            assert untitled == 1
            assert not listed["valid"] and "request_id" not in listed
            assert [issue["path"] for issue in listed["issues"]] == ["candidate"]
            assert listed["workflow_id"] == env.movie_graph.id
            assert listed["query"] == {"title": "Old Movie", "year": 2001, "season": 1}
            keys = [candidate["key"] for candidate in listed["candidates"]]
            assert len(set(keys)) == 3
            first, second, unusable = listed["candidates"]
            assert (first["title"], first["year"], first["id"]) == (
                "New Movie",
                2002,
                "201",
            )
            assert first["plot"].endswith("…") and len(first["plot"]) <= 100
            assert second["year"] == 1999 and unusable["usable"] is False
            graph_id, params = env.engine.calls[-1]
            assert graph_id == env.movie_graph.id
            assert params["$manual"] is True and params["nfo_type"] == "movie"
            assert params["path"] == env.movie.path and params["page_size"] == 5
            # listing saves nothing
            assert Path(env.movie.nfo_path).read_bytes() == before
            assert not await MediaChangeReceipt.exists()
            # a missing title falls back to the name, like the metadata dialog
            again = await rescrape_review(env.owner, item_id=env.movie.id)
            assert again["query"]["title"] == "Old Movie"

    asyncio.run(run())


@pytest.mark.parametrize(
    ("values", "path"),
    [
        ({"item_id": 501}, "item_id"),
        ({"item_id": 500, "workflow_id": 8}, "workflow_id"),
        ({"item_id": 500, "candidate": "000000000000"}, "candidate"),
        ({"item_id": 500, "candidate": "unusable"}, "candidate"),
    ],
)
def test_rescrape_issues(monkeypatch, tmp_path, values, path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            if values.get("candidate") == "unusable":
                listed = await rescrape_review(env.owner, item_id=500)
                values["candidate"] = listed["candidates"][2]["key"]
            reviewed = await rescrape_review(env.owner, **values)

            assert not reviewed["valid"] and "request_id" not in reviewed
            assert path in {issue["path"] for issue in reviewed["issues"]}
            if values["item_id"] == 501:
                # an episode or part is never silently widened to its parent
                assert reviewed["parent_id"] == 500
            assert not await MediaChangeReceipt.exists()

    asyncio.run(run())


def test_movie_preview(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            before = Path(env.movie.nfo_path).read_bytes()
            change, reviewed = await chosen(env.owner, env.movie.id)
            await MediaLib.filter(id=env.movies.id).update(
                rename_template="{{title}} ({{year}})/{{title}} ({{year}})"
            )
            _, organized = await chosen(env.owner, env.movie.id)

            assert reviewed["valid"] and reviewed["request_id"]
            changes = {entry["field"]: entry for entry in reviewed["changes"]}
            assert (changes["title"]["before"], changes["title"]["after"]) == (
                "Old Movie",
                "New Movie",
            )
            assert changes["year"]["after"] == 2002 and changes["id"]["after"] == "201"
            assert changes["poster"]["after"] == "https://images.example.test/201.jpg"
            assert reviewed["nfo"] == {
                "path": "Old Movie/Old Movie.nfo",
                "exists": True,
            }
            assert reviewed["episodes"] is None
            assert reviewed["organization"] == {"template": False}
            assert any("no files are renamed" in note for note in reviewed["notes"])
            assert organized["organization"] == {
                "template": True,
                "current": "Old Movie",
                "expected": "New Movie (2002)",
                "expected_name": "New Movie (2002)",
            }
            # the review records its candidate but writes no metadata
            stored = await MediaChangeReceipt.get(key=key_of(env.owner, reviewed))
            assert stored.candidate["id"] == "201" and not stored.claimed
            assert Path(env.movie.nfo_path).read_bytes() == before
            assert change["candidate"] == reviewed["candidate"]["key"]

    asyncio.run(run())


def test_tv_preview(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            _, same = await chosen(env.owner, env.show.id)
            _, other = await chosen(env.owner, env.show.id, index=1)

            # the same series keeps an episode whose NFO matches its season
            assert same["episodes"] == {
                "total": 3,
                "refresh": 2,
                "keep": 1,
                "season": 1,
                "unnumbered": 1,
            }
            assert other["episodes"]["refresh"] == 3
            assert any("not guessed" in note for note in same["notes"])

    asyncio.run(run())


def test_rescrape_movie(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            searches = len(env.engine.calls)

            applied = await apply(env.owner, reviewed, change)
            stored = await MediaItem.get(id=env.movie.id)
            repeated = await apply(env.owner, reviewed, change)

            assert applied["outcome"] == "rescraped" and not applied["replayed"]
            assert applied["organization"] == "none" and applied["episodes"] is None
            assert applied["path"] == f"/medialibs/{env.movies.id}/{env.movie.id}"
            assert (stored.title, stored.year, stored.unique_id) == (
                "New Movie",
                2002,
                "201",
            )
            assert title_in(env.movie.nfo_path) == "New Movie"
            # the confirmed candidate is saved without searching again
            assert len(env.engine.calls) == searches
            assert repeated["replayed"] and repeated["outcome"] == "rescraped"
            assert repeated["current"] == {"title": "New Movie", "year": 2002}
            assert not await MediaEvent.exists()

    asyncio.run(run())


def test_organization_queued(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await MediaLib.filter(id=env.movies.id).update(
                rename_template="{{title}} ({{year}})/{{title}} ({{year}})"
            )
            change, reviewed = await chosen(env.owner, env.movie.id)

            applied = await apply(env.owner, reviewed, change)
            event = await MediaEvent.get(lib_id=env.movies.id)

            assert applied["organization"] == "queued"
            assert (
                event.event_type == "ingest"
                and env.movie.id in event.payload["organize_ids"]
            )
            # moving files is left to the background organizer
            assert Path(env.movie.path).is_dir()

    asyncio.run(run())


@pytest.mark.parametrize("site", ["tmdb", "tvdb"])
def test_rescrape_tv(monkeypatch, tmp_path, site):
    monkeypatch.setitem(SHOWS[0], "site", site)

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            pilot = Path(env.episodes[0].nfo_path).read_bytes()
            change, reviewed = await chosen(env.owner, env.show.id)
            assert reviewed["episodes"]["keep"] == (1 if site == "tmdb" else 0)
            env.engine.calls.clear()

            applied = await apply(env.owner, reviewed, change)

            assert applied["outcome"] == "rescraped"
            assert applied["episodes"] == {"attempted": 3}
            assert title_in(env.show.nfo_path) == "New Show"
            # only episodes outside the kept rule are searched, with their numbers
            assert sorted(
                (params["season"], params["episode"] or 0)
                for _, params in env.engine.calls
            ) == ([(1, 0), (1, 2)] if site == "tmdb" else [(1, 0), (1, 1), (1, 2)])
            if site == "tmdb":
                assert Path(env.episodes[0].nfo_path).read_bytes() == pilot
            else:
                assert title_in(env.episodes[0].nfo_path) == "Episode 1"
            written = Path(env.show.path) / "S01E02.nfo"
            episode = shelver.read_nfo(LibType.TV_SHOW, written)[0]
            assert (episode.title, episode.season, episode.episode) == (
                "Episode 2",
                1,
                2,
            )
            assert not (Path(env.show.path) / "Extra.nfo").exists()

    asyncio.run(run())


def test_partial(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.show.id)
            env.engine.fail_episode = 2

            partial = await apply(env.owner, reviewed, change)
            calls = len(env.engine.calls)
            repeated = await apply(env.owner, reviewed, change)

            # the saved show stays visible and nothing is repeated
            assert partial["outcome"] == "partial" and partial["applied"] is True
            assert partial["step"] == "episodes"
            assert partial["episodes"] == {
                "attempted": 2,
                "failed_episode_id": env.episodes[1].id,
                "not_attempted": 1,
            }
            assert title_in(env.show.nfo_path) == "New Show"
            assert repeated["replayed"] and repeated["outcome"] == "partial"
            assert len(env.engine.calls) == calls

    asyncio.run(run())


@pytest.mark.parametrize("edit", ["nfo", "title", "workflow", "binding", "episode"])
def test_stale(monkeypatch, tmp_path, edit):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            item = env.show if edit == "episode" else env.movie
            change, reviewed = await chosen(env.owner, item.id)
            if edit == "nfo":
                await write_nfo(
                    NFOType.MOVIE, env.movie.nfo_path, {"title": "Edited by hand"}
                )
            elif edit == "title":
                await MediaItem.filter(id=item.id).update(title="Renamed")
            elif edit == "workflow":
                await FlowGraph.filter(id=7).update(revision=2)
            elif edit == "binding":
                other = await FlowGraph.create(
                    name="Other movie scraper",
                    category="ingest",
                    state="published",
                    definition=scraper(),
                )
                await FlowTrigger.filter(rel_id=env.movies.id).update(graph_id=other.id)
            else:
                await MediaItem.create(
                    lib=env.shows,
                    parent=env.show,
                    path=str(Path(env.show.path) / "S01E03.mkv"),
                    dir=env.show.path,
                    name="S01E03",
                    season=1,
                    episode=3,
                )
            before = Path(item.nfo_path).read_bytes()

            result = await apply(env.owner, reviewed, change)

            assert result["stale"] and not result["applied"]
            assert Path(item.nfo_path).read_bytes() == before
            assert not await MediaChangeReceipt.filter(claimed=True).exists()

    asyncio.run(run())


@pytest.mark.parametrize("existing", [False, True])
def test_episode_nfo_changed(monkeypatch, tmp_path, existing):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.show.id, index=1)
            episode = env.episodes[0 if existing else 1]
            path = episode.nfo_path or shelver.get_nfo_path(episode.path)
            await write_nfo(
                NFOType.EPISODE,
                path,
                {"title": "Edited by hand", "season": 1, "episode": episode.episode},
            )
            before = Path(path).read_bytes()

            result = await apply(env.owner, reviewed, change)

            assert result.get("stale") and not result["applied"]
            assert Path(path).read_bytes() == before
            assert title_in(env.show.nfo_path) == "Old Show"

    asyncio.run(run())


def test_confirmation(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            before = Path(env.movie.nfo_path).read_bytes()

            unconfirmed = await apply(env.owner, reviewed, change, confirmed=False)

            assert unconfirmed["confirmation_required"] and not unconfirmed["applied"]
            assert unconfirmed["changes"] and unconfirmed["request_id"]
            assert Path(env.movie.nfo_path).read_bytes() == before
            assert not await MediaChangeReceipt.filter(claimed=True).exists()

    asyncio.run(run())


@pytest.mark.parametrize("field", ["candidate", "item_id", "title", "action"])
def test_conflict(monkeypatch, tmp_path, field):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            listed = await rescrape_review(env.owner, item_id=env.movie.id)
            altered = {
                "candidate": {**change, "candidate": listed["candidates"][1]["key"]},
                "item_id": {**change, "item_id": env.show.id},
                "title": {**change, "title": "Other"},
                "action": {"action": "scan", "library_id": env.movies.id},
            }[field]

            result = await apply(env.owner, reviewed, altered)

            assert result["conflict"] and not result["applied"]
            assert title_in(env.movie.nfo_path) == "Old Movie"

    asyncio.run(run())


def test_concurrent(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            writes = AsyncMock(wraps=shelver.gen_nfo)
            monkeypatch.setattr(shelver, "gen_nfo", writes)

            results = await asyncio.gather(
                *(apply(env.owner, reviewed, change) for _ in range(3))
            )

            fresh = [result for result in results if result.get("replayed") is False]
            assert len(fresh) == 1 and fresh[0]["outcome"] == "rescraped"
            assert all(
                result.get("replayed") or result.get("pending")
                for result in results
                if result is not fresh[0]
            )
            assert writes.await_count == 1

    asyncio.run(run())


def test_running(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.show.id)
            monkeypatch.setattr(service, "APPLY_WAIT", 0.05)
            env.engine.release = asyncio.Event()

            running = await apply(env.owner, reviewed, change)
            pending = await apply(env.owner, reviewed, change)
            env.engine.release.set()
            while service._RUNNING:
                await asyncio.sleep(0.01)
            finished = await apply(env.owner, reviewed, change)

            # the caller is answered at once; the episodes finish in the background
            assert running["outcome"] == "running" and running["applied"] is None
            assert running.get("item_id") == env.show.id
            assert running.get("library_id") == env.shows.id
            assert running.get("path") == f"/medialibs/{env.shows.id}/{env.show.id}"
            assert pending["pending"]
            assert finished["replayed"] and finished["outcome"] == "rescraped"

    asyncio.run(run())


def test_restart(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            key = key_of(env.owner, reviewed)
            # a process stopped after claiming, before recording the outcome
            await MediaChangeReceipt.filter(key=key).update(claimed=True)

            replay = await apply(env.owner, reviewed, change)

            assert replay["replayed"] and replay["outcome"] == "uncertain"
            assert replay["applied"] is None
            assert "only reads its recorded outcome" in replay["message"]
            assert title_in(env.movie.nfo_path) == "Old Movie"

    asyncio.run(run())


def test_failed(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            original = shelver.gen_nfo
            monkeypatch.setattr(
                shelver,
                "gen_nfo",
                AsyncMock(side_effect=ContentError("invalid_metadata")),
            )

            failed = await apply(env.owner, reviewed, change)
            monkeypatch.setattr(shelver, "gen_nfo", original)
            repeated = await apply(env.owner, reviewed, change)

            # unchanged bytes alone cannot prove that nothing took effect
            assert (
                failed["outcome"] == "uncertain"
                and failed["error"] == "invalid_metadata"
            )
            assert repeated["outcome"] == "uncertain" and repeated["replayed"]
            assert title_in(env.movie.nfo_path) == "Old Movie"

    asyncio.run(run())


def test_unchanged_nfo_partial_failure(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await write_nfo(NFOType.MOVIE, env.movie.nfo_path, MOVIES[0])
            await MediaLib.filter(id=env.movies.id).update(
                rename_template="{{title}} ({{year}})/{{title}} ({{year}})"
            )
            change, reviewed = await chosen(env.owner, env.movie.id)
            monkeypatch.setattr(
                shelver,
                "notify_media_events",
                Mock(side_effect=OSError("lost notification")),
            )

            first = await apply(env.owner, reviewed, change)
            second = await apply(env.owner, reviewed, change)

            assert (await MediaItem.get(id=env.movie.id)).title == "New Movie"
            assert await MediaEvent.exists(lib_id=env.movies.id)
            assert first.get("outcome") == "uncertain"
            assert second.get("replayed") and second["outcome"] == "uncertain"

    asyncio.run(run())


def test_cancelled(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.show.id)
            env.engine.release = asyncio.Event()

            caller = asyncio.create_task(apply(env.owner, reviewed, change))
            await asyncio.sleep(0.05)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            env.engine.release.set()
            while service._RUNNING:
                await asyncio.sleep(0.01)
            # the lost response is read back instead of saving again
            repeated = await apply(env.owner, reviewed, change)

            assert repeated["replayed"] and repeated["outcome"] == "rescraped"
            assert title_in(env.show.nfo_path) == "New Show"

    asyncio.run(run())


def test_deleted_item(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            await MediaItem.filter(id__in=[500, 501]).delete()

            absent = await apply(env.owner, reviewed, change)

            assert absent["outcome"] == "absent" and not absent["applied"]
            assert title_in(env.movie.nfo_path) == "Old Movie"

    asyncio.run(run())


def test_permissions(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            demoted = SimpleNamespace(
                id=env.owner.id,
                role=UserRole.USER,
                perms=Permissions(media_lib_ids=[60]),
            )
            with pytest.raises(ForbiddenException):
                await rescrape_review(env.reader, item_id=env.movie.id)
            with pytest.raises(ForbiddenException):
                await apply(demoted, reviewed, change)
            applied = await apply(env.owner, reviewed, change)
            receipt = await MediaChangeReceipt.get(claimed=True)
            with pytest.raises(ForbiddenException):
                await service.MediaChangeService._replay(receipt, demoted)

            assert applied["outcome"] == "rescraped"

    asyncio.run(run())


def test_arguments():
    for values in (
        {"item_id": 500, "season": -1},
        {"item_id": 500, "path": "/etc"},
        {"item_id": 0},
        {"item_id": 500, "title": ""},
    ):
        with pytest.raises(ValidationError):
            MediaRescrape.model_validate(values)
    with pytest.raises(ValidationError):
        LibraryScan.model_validate({"library_id": 60, "dir": "/tmp"})


def seen_candidates(body):
    """Collect every candidate the model can still see, including summaries.

    Args:
        body: Model request.

    Returns:
        Candidate descriptions in conversation order.
    """
    found = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("candidates"), list):
                found.extend(value["candidates"])
            for entry in value.values():
                walk(entry)
        elif isinstance(value, list):
            for entry in value:
                walk(entry)
        elif isinstance(value, str) and "candidates" in value:
            for start in (0, value.find("\n") + 1):
                try:
                    walk(loads(value[start:]))
                    break
                except ValueError:
                    continue

    walk(body["messages"])
    return found


def page_of(body):
    """Read the page context sent with the current prompt.

    Args:
        body: Model request.

    Returns:
        Context captured when the message was sent.
    """
    return loads(body["messages"][-2]["content"].split("\n", 1)[1])


@pytest.mark.parametrize("verbose", [False, True], ids=["short", "compacted"])
def test_rescrape_conversation(monkeypatch, tmp_path, verbose):
    streams, offered = [], []

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

    def pick(body):
        # the user chose by year; the key comes from what the model still sees
        key = next(
            entry["key"]
            for entry in reversed(seen_candidates(body))
            if entry.get("year") == 2002
        )
        return offer(
            body, ("review_media_rescrape", {"item_id": 500, "candidate": key})
        )

    steps = [
        # turn 1: the item page selects the movie; candidates are listed, not chosen
        lambda body: offer(
            body, ("get_media_item", {"item_id": page_of(body)["resource_id"]})
        ),
        lambda body: offer(body, ("review_media_rescrape", {"item_id": 500})),
        lambda body: offer(body),
        # turn 2: the chosen candidate is previewed; same-turn confirmation waits
        pick,
        confirm,
        lambda body: offer(body),
        # turn 3: the user confirms, but the response after saving is lost
        confirm,
        lost,
        # turn 4: rereading the item and repeating the request reports the outcome
        lambda body: offer(body, ("get_media_item", {"item_id": 500})),
        confirm,
        lambda body: offer(body),
    ]
    configure(monkeypatch, scripted(steps, streams, verbose=verbose))
    context = {
        "page_type": "media_item",
        "route": "/medialibs/60/500",
        "resource_id": 500,
        "library_id": 60,
    }

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            turns = [
                await converse(
                    conversation["id"], env.owner, prompt, context=page, key=key
                )
                for key, prompt, page in (
                    ("turn-1", "Rescrape this movie.", context),
                    ("turn-2", "The 2002 one.", None),
                    ("turn-3", "Yes, save it.", None),
                    ("turn-4", "Did it save? Save it.", None),
                )
            ]
            stored = await AssistantTurn.filter(conversation_id=conversation["id"])
            item = await MediaItem.get(id=500)
            return turns, stored, item

    turns, stored, item = asyncio.run(run())
    (_, listed), (_, reviewed), (_, saved), (_, repeated) = turns

    assert [done.event for done, _ in turns] == ["done", "done", "error", "done"]
    # media changes appear after reading media, or after a shown review or list
    assert offered[0] == set()
    assert offered[1] == offered[3] == offered[6] == tools.MEDIA_CHANGE_TOOLS
    candidates = listed[1]["data"]["candidates"]
    assert len(candidates) == 3 and "request_id" not in listed[1]["data"]
    preview, same_turn = reviewed[0]["data"], reviewed[1]["data"]
    assert preview["valid"] and preview["candidate"]["year"] == 2002
    assert same_turn["confirmation_required"] and not same_turn["applied"]
    assert saved[0]["data"]["outcome"] == "rescraped"
    assert repeated[1]["data"]["replayed"]
    assert (item.title, item.year) == ("New Movie", 2002)
    # libraries and workflows are recorded as sources; item IDs never are
    for turn in stored:
        assert "media_lib:60" in turn.resource_ids
        assert 500 not in turn.resource_ids
    assert any(7 in turn.resource_ids for turn in stored)


def test_scan_conversation(monkeypatch, tmp_path):
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

    def listing(body):
        seen.update(page_of(body))
        return offer(body, ("list_media_libraries", {}))

    steps = [
        listing,
        lambda body: offer(
            body, ("review_library_scan", {"library_id": seen["resource_id"]})
        ),
        lambda body: offer(body),
        confirm,
        lambda body: offer(body),
        confirm,
        lambda body: offer(body),
    ]
    configure(monkeypatch, scripted(steps, streams))
    context = {
        "page_type": "media_library",
        "route": "/medialibs/60",
        "resource_id": 60,
    }

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            owner = env.owner
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), owner
            )
            _, reviewed = await converse(
                conversation["id"],
                owner,
                "Scan this library.",
                context=context,
                key="a",
            )
            _, scanned = await converse(conversation["id"], owner, "Yes.", key="b")
            _, repeated = await converse(conversation["id"], owner, "Again?", key="c")
            selection = await AssistantTurn.get(request_id="a")

            assert offered[0] == set() and offered[1] == tools.MEDIA_CHANGE_TOOLS
            assert reviewed[1]["data"]["library_id"] == 60
            assert scanned[0]["data"]["outcome"] == "scanned"
            assert repeated[0]["data"]["replayed"]
            assert "media_lib:60" in selection.resource_ids

            # history naming the library follows current permissions
            owner.role = UserRole.USER
            owner.perms = Permissions(media_lib_ids=[61])
            denied = await tools.execute_tool(
                "read_conversation_history",
                dumps({"turn_id": selection.id}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="history",
            )
            refused = await tools.execute_tool(
                "review_library_scan",
                dumps({"library_id": 61}).decode(),
                owner,
                conversation_id=conversation["id"],
                call_id="review",
            )
            assert denied["error"]["code"] == "permission_denied"
            assert refused["error"]["code"] == "forbidden"

    asyncio.run(run())


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
                prompt="Scan, then rescrape.",
            )
            scan = await scan_review(env.owner, 60)
            records = [
                ("review_library_scan", {"library_id": 60}, {"ok": True, "data": scan}),
                (
                    "review_media_rescrape",
                    {"item_id": 500},
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
                tools.MediaConfirm(request_id=scan["request_id"], confirmed=True),
                conversation["id"],
                env.owner,
            )

            # a newer review, even a failed one, replaces the shown scan
            assert confirmed["confirmation_required"] and not confirmed["applied"]
            assert "newer review" in confirmed["message"]
            assert not await MediaEvent.exists()

    asyncio.run(run())


def test_mcp(monkeypatch, tmp_path):
    async def run():
        async with application() as app:
            engine = Engine()
            app.ctx.flow_engine = engine
            # other modules may leave apps registered; services read this one
            monkeypatch.setattr(Sanic, "get_app", lambda *args, **kwargs: app)
            stored = await store(tmp_path)
            app.ctx.lib_watcher = monitor_for(stored.movies, stored.shows)
            admin = app.shared_ctx.sessions["admin"]
            scan = {"action": "scan", "library_id": 60}

            denied = await call(app, "review_library_scan", {"change": scan})
            reviewed = await call(
                app, "review_library_scan", {"change": scan}, token="admin"
            )
            data = reviewed["structuredContent"]
            arguments = {
                "application": {
                    "request_id": data["request_id"],
                    "change": scan,
                    "revision": data["revision"],
                    "confirmed": True,
                }
            }
            shared = await tools.execute_tool(
                "review_library_scan",
                dumps({"library_id": 60}).decode(),
                admin,
                conversation_id=1,
                call_id="parity",
            )
            read_only = await call(app, "apply_media_change", arguments, token="admin")
            write = (
                await MCPAccessTokenService.create(
                    admin.id,
                    MCPAccessTokenCreate(name="media", scope=MCPTokenScope.READ_WRITE),
                )
            )["token"]
            scanned = await call(app, "apply_media_change", arguments, token=write)
            replay = await call(app, "apply_media_change", arguments, token=write)
            listed = await call(
                app,
                "review_media_rescrape",
                {"change": {"item_id": 500}},
                token="admin",
            )
            change = {
                "action": "rescrape",
                "item_id": 500,
                "candidate": listed["structuredContent"]["candidates"][0]["key"],
            }
            preview = await call(
                app, "review_media_rescrape", {"change": change}, token="admin"
            )
            details = preview["structuredContent"]
            other = {
                **change,
                "candidate": listed["structuredContent"]["candidates"][1]["key"],
            }
            application_args = {
                "request_id": details["request_id"],
                "change": change,
                "revision": details["revision"],
                "confirmed": True,
            }
            conflict = await call(
                app,
                "apply_media_change",
                {"application": {**application_args, "change": other}},
                token=write,
            )
            saved = await call(
                app,
                "apply_media_change",
                {"application": application_args},
                token=write,
            )

            assert denied["isError"] and "not allowed" in str(denied)
            assert read_only["isError"] and "read-only" in str(read_only)
            assert scanned["structuredContent"]["outcome"] == "scanned"
            assert replay["structuredContent"]["replayed"]
            data.pop("request_id")
            shared["data"].pop("request_id")
            assert shared["data"] == data
            assert not listed["structuredContent"]["valid"]
            assert details["valid"] and details["changes"]
            assert conflict["structuredContent"]["conflict"]
            assert saved["structuredContent"]["outcome"] == "rescraped"
            assert (await MediaItem.get(id=500)).title == "New Movie"

    asyncio.run(run())


def test_sizes_and_compaction(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            listed = await rescrape_review(env.owner, item_id=env.show.id)
            _, preview = await chosen(env.owner, env.show.id)
            scan = await scan_review(env.owner, 60)
            return listed, preview, scan

    listed, preview, scan = asyncio.run(run())
    admin = SimpleNamespace(id=1, role=UserRole.ADMIN)
    schemas = {
        schema["function"]["name"]: schema
        for schema in tools.tool_schemas(admin, media_change=True)
    }
    added = [schemas[name] for name in sorted(tools.MEDIA_CHANGE_TOOLS)]

    # results stay small; the tools cost context only once unlocked
    assert len(dumps(listed)) < 1500 and len(dumps(preview)) < 2500
    assert len(dumps(scan)) < 1500 and len(dumps(added)) < 2100
    assert not tools.MEDIA_CHANGE_TOOLS & {
        schema["function"]["name"] for schema in tools.tool_schemas(admin)
    }
    for focus in ({"plan_only": True}, {"authoring": True}):
        assert not tools.MEDIA_CHANGE_TOOLS & names(
            {"tools": tools.tool_schemas(admin, media_change=True, **focus)}
        )
    reader = SimpleNamespace(id=2, role=UserRole.USER)
    assert not tools.MEDIA_CHANGE_TOOLS & names(
        {"tools": tools.tool_schemas(reader, media_change=True)}
    )
    # the active preview keeps its effects and choices when compacted
    for data in (listed, preview, scan):
        source = {
            "id": "review",
            "name": "review_media_rescrape",
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
        compacted = history.compact_exchanges(messages, 1, 7000)
        fact = loads(compacted[0]["content"].split("\n", 1)[1])["tools"][0]
        for key in (
            "candidates",
            "changes",
            "notes",
            "episodes",
            "organization",
            "bindings",
            "nfo",
            "workflows",
        ):
            if key in data:
                assert fact["data"][key] == data[key]
        assert len(dumps(compacted)) < 7000
    # an earlier turn keeps the season needed to choose between the same show
    summary = tool_facts(
        {"id": "list", "name": "review_media_rescrape", "ok": True, "data": listed}
    )["data"]
    assert summary["candidates"] == [
        {key: entry.get(key) for key in ("key", "title", "year")}
        | ({"season": entry["season"]} if "season" in entry else {})
        for entry in listed["candidates"]
    ]
    assert (
        tool_facts(
            {
                "id": "preview",
                "name": "review_media_rescrape",
                "ok": True,
                "data": preview,
            }
        )["data"]["request_id"]
        == preview["request_id"]
    )


def test_retry_before_publication(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            change, reviewed = await chosen(env.owner, env.movie.id)
            with monkeypatch.context() as patch:
                patch.setattr(shelver, "gen_nfo", AsyncMock(return_value=False))
                refused = await apply(env.owner, reviewed, change)
            saved = await apply(env.owner, reviewed, change)

            assert refused["failed"] and refused["applied"] is False
            assert saved["outcome"] == "rescraped" and not saved["replayed"]

    asyncio.run(run())


def test_distinct_reviews_conflict(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            first_change, first_review = await chosen(env.owner, env.movie.id)
            second_change, second_review = await chosen(
                env.owner, env.movie.id, index=1
            )
            entered, release = asyncio.Event(), asyncio.Event()
            save = service.MediaItemService.save_video_metadata

            async def delayed(*args, **kwargs):
                entered.set()
                await release.wait()
                return await save(*args, **kwargs)

            monkeypatch.setattr(
                service.MediaItemService, "save_video_metadata", delayed
            )
            monkeypatch.setattr(service, "APPLY_WAIT", 0.05)
            first = asyncio.create_task(apply(env.owner, first_review, first_change))
            await entered.wait()
            second = await apply(env.owner, second_review, second_change)
            release.set()
            await first
            while service._RUNNING:
                await asyncio.sleep(0.01)
            repeated = await apply(env.owner, second_review, second_change)

            assert second.get("pending")
            assert repeated.get("stale") and not repeated["applied"]
            assert (await MediaItem.get(id=env.movie.id)).unique_id == "201"

    asyncio.run(run())


def test_season_candidates(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            await MediaLib.filter(id=env.shows.id).update(
                rename_template="{{parent.title}} ({{year}})/Season {{season}}/"
                "{{title}} S{{season}}E{{episode}}"
            )
            monkeypatch.setattr(
                env.engine,
                "execute",
                AsyncMock(
                    return_value=[{**SHOWS[0], "season": 1}, {**SHOWS[0], "season": 2}]
                ),
            )
            listed = await rescrape_review(env.owner, item_id=env.show.id)
            first, second = listed["candidates"]
            assert first["key"] != second["key"]
            assert [entry["season"] for entry in listed["candidates"]] == [1, 2]
            preview = await rescrape_review(
                env.owner, item_id=env.show.id, candidate=second["key"]
            )
            assert preview["episodes"]["season"] == 2
            assert preview["organization"]["expected"] == "New Show (2020)/Season 02"
            assert {"field": "season", "before": 1, "after": 2} in preview["changes"]

    asyncio.run(run())


def test_candidate_size(monkeypatch, tmp_path):
    async def run():
        async with media(monkeypatch, tmp_path) as env:
            candidate = {
                **MOVIES[0],
                "title": "电影" * 100,
                "originaltitle": "x" * 6000,
            }
            monkeypatch.setattr(
                env.engine, "execute", AsyncMock(return_value=[candidate] * 5)
            )
            listed = await rescrape_review(env.owner, item_id=env.movie.id)

            assert len(listed["candidates"]) == 5
            assert all(entry.get("usable", True) for entry in listed["candidates"])
            assert len(dumps(listed)) < 4000
            assert all(entry["key"] for entry in listed["candidates"])

    asyncio.run(run())


def test_review_provenance(monkeypatch, tmp_path):
    steps = [
        lambda body: [("review_library_scan", {"library_id": 61})],
        lambda body: [("review_media_rescrape", {"item_id": 600})],
        lambda body: None,
    ]
    configure(monkeypatch, scripted(steps, []))

    async def run():
        async with media(monkeypatch, tmp_path) as env:
            conversation = await Sessions.create(
                ConversationCreate.model_validate({"model": MODEL}), env.owner
            )
            done, results = await converse(
                conversation["id"], env.owner, "Scan shows, then rescrape.", key="a"
            )
            turn = await AssistantTurn.get(request_id="a")

            # reviews record their library and workflows; item IDs never are
            assert done.event == "done" and all(result["ok"] for result in results)
            assert set(turn.resource_ids) == {"media_lib:61", env.tv_graph.id}

    asyncio.run(run())
