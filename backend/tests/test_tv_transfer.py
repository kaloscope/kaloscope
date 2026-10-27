"""Unit tests for TV download transfer planning and preview."""

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import orjson
import pytest
from lxml import etree
from pydantic import ValidationError
from sanic import Sanic
from torrentool.bencode import Bencode

from app.core.ai import tools
from app.core.constants import NFO_MIME_TYPE
from app.core.dl import syncer
from app.core.exceptions import BadRequestException, ForbiddenException
from app.core.media import watcher
from app.core.media.handlers.base import get_handler
from app.models.download import (
    DownloadPlan,
    DownloadPlanCheck,
    DownloadPlanCreate,
    DownloadState,
    DownloadTask,
    DownloadTaskCheck,
    DownloadTaskCreate,
    TransferMethod,
)
from app.models.flow import FlowLog, FlowTrigger
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.media import LibType, MediaEvent, MediaItem, MediaLib
from app.models.user import Permissions, UserRole
from app.services import download as service
from app.services.mcp import MCPAccessTokenService
from app.utils.bittorrent import decode_torrent
from app.utils.json import dumps, loads
from tests.test_assistant_persistence import database
from tests.test_download_plan_authoring import fixture_plan
from tests.test_download_plan_authoring import isolate as isolate
from tests.test_download_plan_conversation import engine_fixture
from tests.test_download_plan_ingestion import bindings, creation, libraries, reviewed
from tests.test_download_tasks_once import RESULTS, Rpc, searched, settings
from tests.test_download_tasks_once import sealing as sealing
from tests.test_mcp import application, call
from tests.test_services_media_index import _ingest_engine
from tests.test_tv_ingestion import EPISODE_HTML, SHOW_HTML, tv_libraries

SEASON = "Wayfinder (2024)/Season {{ '%02d' % season }}/"
PACK = "Wayfinder.2024.1080p"
PACK_FILES = [
    f"{PACK}/Wayfinder.S02E01.mkv",
    f"{PACK}/Wayfinder.S02E01.en.srt",
    f"{PACK}/Wayfinder.S00E01.mkv",
]
PACK_TORRENT = Bencode.encode(
    {
        "info": {
            "name": PACK,
            "files": [{"length": 1, "path": [Path(file).name]} for file in PACK_FILES],
            "piece length": 16384,
            "pieces": b"\x01" * 20,
        }
    }
)
RULE = {"sub_pattern": r"^[^/]+/", "sub_repl": SEASON}


def shows(tmp_path):
    """Describe an unsaved TV library rooted in the test directory.

    Args:
        tmp_path: Isolated directory for the library root.

    Returns:
        TV library used only for read-only previews.
    """
    return MediaLib(
        id=1, name="Shows", dir=str(tmp_path / "shows"), lib_type=LibType.TV_SHOW
    )


def tree(path):
    """Snapshot every file below a directory.

    Args:
        path: Directory to read.

    Returns:
        Relative paths mapped to their contents.
    """
    return {
        str(file.relative_to(path)): file.read_bytes()
        for file in sorted(path.rglob("*"))
        if file.is_file()
    }


def pack_site(request):
    """Serve the season pack's torrent file from the synthetic tracker.

    Args:
        request: HEAD or GET request from source normalization.

    Returns:
        Torrent content for file URLs, otherwise a player page.
    """
    if request.url.path.startswith("/files/"):
        return httpx.Response(
            200,
            content=PACK_TORRENT,
            headers={"content-type": "application/x-bittorrent"},
        )
    return httpx.Response(200, text="<html>player</html>")


def located(preview):
    """List the preview's destinations and the numbers the library reads.

    Args:
        preview: Transfer preview.

    Returns:
        Destination, season and episode of each shown item.
    """
    return [
        (item["to"], item.get("season"), item.get("episode"))
        for item in preview["items"]
    ]


@pytest.mark.parametrize(
    ("pattern", "repl", "samples", "expected", "problem"),
    [
        (
            None,
            None,
            ["Wayfinder.S00/Wayfinder.E01.mkv"],
            [("Wayfinder.S00/Wayfinder.E01.mkv", 0, 1)],
            None,
        ),
        (
            "^",
            SEASON,
            ["Wayfinder.S02E05.1080p.WEB-DL.mkv"],
            [("Wayfinder (2024)/Season 02/Wayfinder.S02E05.1080p.WEB-DL.mkv", 2, 5)],
            None,
        ),
        (
            None,
            None,
            ["Wayfinder.S02E05.mkv"],
            [("Wayfinder.S02E05.mkv", None, None)],
            "TV ingestion indexes only",
        ),
        (
            None,
            None,
            [
                "Wayfinder (2024)/Season 02/Wayfinder.S02E01.mkv",
                "Wayfinder (2024)/Season 02/Wayfinder.S02E01.en.srt",
            ],
            [
                ("Wayfinder (2024)/Season 02/Wayfinder.S02E01.mkv", 2, 1),
                ("Wayfinder (2024)/Season 02/Wayfinder.S02E01.en.srt", None, None),
            ],
            None,
        ),
        (
            "^(.+/)?",
            "{{title}}/",
            ["Wayfinder.S02.1080p/Wayfinder.S02E01.mkv"],
            [("Wayfinder/Wayfinder.S02E01.mkv", 1, 1)],
            "read as season 1, but the file states season 2",
        ),
        (
            r"^(?:.*/)?",
            SEASON,
            ["Extras/Wayfinder.S00E01.mkv"],
            [("Wayfinder (2024)/Season 00/Wayfinder.S00E01.mkv", 0, 1)],
            None,
        ),
        (
            "^(.+/)?",
            "Wayfinder/",
            ["Wayfinder.S00E01.mkv"],
            [("Wayfinder/Wayfinder.S00E01.mkv", 1, 1)],
            "read as season 1, but the file states season 0",
        ),
        (
            r"^(?=.*\.mkv$)",
            "Wayfinder/Season 01/",
            ["Wayfinder.S01E01.mkv", "Wayfinder.S01E01.en.srt"],
            [
                ("Wayfinder.S01E01.en.srt", None, None),
                ("Wayfinder/Season 01/Wayfinder.S01E01.mkv", 1, 1),
            ],
            "Keep companion names beside",
        ),
        (
            "^",
            "Wayfinder/Season {{ season }}/",
            ["Wayfinder - 03.mkv"],
            [("Wayfinder/Season /Wayfinder - 03.mkv", None, 3)],
            "has no season number",
        ),
        (
            "^",
            "Wayfinder/Season 01/",
            ["Wayfinder Pilot.mkv"],
            [("Wayfinder/Season 01/Wayfinder Pilot.mkv", 1, None)],
            None,
        ),
        (
            r"^[^/]+/",
            "Wayfinder/Season 01/",
            ["Pack/Wayfinder.S01E01.mkv", "Pack/Featurettes/Making Of.mkv"],
            [
                ("Wayfinder/Season 01/Wayfinder.S01E01.mkv", 1, 1),
                ("Wayfinder/Season 01/Featurettes/Making Of.mkv", None, None),
            ],
            None,
        ),
        (
            None,
            None,
            ["Wayfinder/Season 01/Extras/Making Of.mkv"],
            [("Wayfinder/Season 01/Extras/Making Of.mkv", None, None)],
            "No video reaches a folder depth",
        ),
    ],
    ids=[
        "explicit-zero-series",
        "episode",
        "library-root",
        "season-pack",
        "flattened-season",
        "special",
        "special-series-folder",
        "companion",
        "unresolved-season",
        "unresolved-episode",
        "extras",
        "only-extras",
    ],
)
def test_preview(tmp_path, pattern, repl, samples, expected, problem):
    issues, preview = asyncio.run(
        service.transfer_preview(shows(tmp_path), pattern, repl, samples=samples)
    )

    messages = [issue["message"] for issue in issues]
    assert located(preview) == expected
    assert any(problem in message for message in messages) if problem else not issues
    assert preview["basis"] == "samples"
    assert preview["notes"][0].startswith("Samples only illustrate")
    assert ("hint" in preview) == bool(problem)
    if expected[0][2] is None and expected[0][1] is not None:
        assert "episode=null" in preview["notes"][1]
    if len(expected) > 1 and preview["items"][1].get("indexed") is False:
        # extras below the season folder are reported, not rejected
        assert "below the indexed folder depth" in preview["notes"][1]


@pytest.mark.parametrize(
    ("pattern", "repl", "samples", "path", "message", "rule"),
    [
        ("^(.*)$", r"\2", [], "sub_repl", "Invalid replacement", "unchecked"),
        ("(", "x", ["W.S01E01.mkv"], "sub_pattern", "Invalid regex", "unchecked"),
        (
            "^",
            "{{ title",
            ["W.S01E01.mkv"],
            "sub_repl",
            "Invalid template",
            "unchecked",
        ),
        (
            "^",
            "{{ ''.__class__.__mro__ }}/",
            ["W.S01E01.mkv"],
            "sub_repl",
            "blocked Python internals",
            "unchecked",
        ),
        (
            "^",
            "W/Season {{ '%02d' % season }}/",
            ["W.S01E01.mkv", "W - 03.mkv"],
            "sub_repl",
            "W - 03.mkv: ",
            "unchecked",
        ),
        ("^", "../outside/", ["W.S01E01.mkv"], "sub_repl", "outside", "applied"),
        ("^", "/elsewhere/", ["W.S01E01.mkv"], "sub_repl", "outside", "applied"),
        (
            "^.*$",
            "W/Season 01/W.S01E01.mkv",
            ["W.S01E01.mkv", "W.S01E02.mkv"],
            "sub_repl",
            "new transfers are blocked",
            "conflict",
        ),
    ],
    ids=[
        "replacement-without-files",
        "regex",
        "template-syntax",
        "sandbox",
        "template-error",
        "traversal",
        "absolute",
        "duplicates",
    ],
)
def test_rule_errors(tmp_path, pattern, repl, samples, path, message, rule):
    issues, preview = asyncio.run(
        service.transfer_preview(shows(tmp_path), pattern, repl, samples=samples)
    )

    assert issues[0]["path"] == path and message in issues[0]["message"]
    assert preview["rule"] == rule
    if rule == "conflict":
        assert "items" not in preview
        assert len(issues) == 1


def test_bounds(tmp_path):
    lib = shows(tmp_path)
    existing = tmp_path / "shows" / "W" / "Season 01" / "W.S01E04.mkv"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"unrelated")
    before = tree(tmp_path)
    samples = [f"W.S01E{number:02d}.mkv" for number in range(1, 9)]

    issues, preview = asyncio.run(
        service.transfer_preview(lib, "^", "W/Season 01/", samples=samples)
    )

    assert tree(tmp_path) == before
    assert issues == [
        {
            "path": "sub_repl",
            "message": "W/Season 01/W.S01E04.mkv already exists; transfer will not "
            "overwrite it.",
        }
    ]
    # problems come first and the rest is counted, not listed
    assert [item["from"] for item in preview["items"]] == [
        "W.S01E04.mkv",
        *samples[:3],
        *samples[4:6],
    ]
    assert preview["omitted"] == 2
    # long known paths and many problems stay within a compact result
    pack = "Wayfinder." + "Extended." * 40 + "S02"
    files = [f"{pack}/Wayfinder.S02E{number:02d}.mkv" for number in range(1, 11)]
    issues, preview = asyncio.run(
        service.transfer_preview(lib, "^(.+/)?", "{{ title }}/", files=files)
    )
    assert len(issues) == 5 and issues[-1]["message"].startswith("6 more files")
    assert all(len(item["from"]) <= 160 for item in preview["items"])
    assert len(dumps(preview)) < 2048 and preview["omitted"] == 4
    for invalid in (
        [*samples, "W.S01E09.mkv"],
        ["../W.S01E01.mkv"],
        ["/W.S01E01.mkv"],
        [""],
    ):
        with pytest.raises(ValidationError):
            DownloadPlanCheck.model_validate(
                {
                    "plan": {
                        "graph_id": 1,
                        "downloader_id": 1,
                        "dir": "/downloads",
                        "keyword": "W",
                        "interval_num": 1,
                        "batch_limit": 1,
                    },
                    "transfer_samples": invalid,
                }
            )


@pytest.mark.parametrize("series_zero", [False, True], ids=["pack", "series-zero"])
def test_runtime_parity(tmp_path, series_zero):
    files = ["Wayfinder.S00/Wayfinder.E01.mkv"] if series_zero else PACK_FILES
    rule = {} if series_zero else RULE
    downloads = tmp_path / "downloads"
    for file in files:
        (downloads / file).parent.mkdir(parents=True, exist_ok=True)
        (downloads / file).write_bytes(file.encode())

    async def run():
        async with database():
            _, downloader, _ = await fixture_plan()
            lib = await MediaLib.create(
                lib_type=LibType.TV_SHOW,
                dir=str(tmp_path / "shows"),
                name="Shows",
                priority=1,
            )
            issues, preview = await service.transfer_preview(
                lib, rule.get("sub_pattern"), rule.get("sub_repl"), files=files
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(downloads),
                name=PACK,
                files=files,
                state=DownloadState.COMPLETED,
                transfer_lib_id=lib.id,
                transfer_method=TransferMethod.COPY,
                **rule,
            )

            assert issues == [] and preview["basis"] == "known_files"
            assert "notes" not in preview
            assert await syncer.transfer_files(task, task.files)
            await task.refresh_from_db()
            root = tmp_path / "shows"
            planned = {item["from"]: item for item in preview["items"]}
            assert task.transfer_targets == {
                file: str(root / planned[file]["to"]) for file in files
            }
            assert sorted(tree(root)) == sorted(item["to"] for item in preview["items"])
            # the library handler reads the numbers the preview reported
            handler = get_handler(LibType.TV_SHOW)
            for item in preview["items"]:
                if "season" not in item:
                    continue
                parent, child = await handler.gen_items(lib, root / item["to"])
                assert (parent.season, child.episode) == (
                    item["season"],
                    item["episode"],
                )

            # retries and recovery keep recorded destinations after a rule change
            recorded = dict(task.transfer_targets)
            await DownloadTask.filter(id=task.id).update(sub_repl="Elsewhere/")
            await task.refresh_from_db()
            assert await syncer.transfer_files(task, task.files)
            await syncer.backfill_transfer_targets(lib)
            await task.refresh_from_db()
            assert task.transfer_targets == recorded
            assert not (root / "Elsewhere").exists()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["absolute", "symlink", "parent-file", "nul"])
def test_destination_boundary(tmp_path, case):
    lib = shows(tmp_path)
    root = Path(lib.dir)
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if case == "symlink":
        (root / "W").symlink_to(outside, target_is_directory=True)
    elif case == "parent-file":
        (root / "W").write_text("existing file")
    prefix = f"{root}/W/Season 01/" if case == "absolute" else "W/Season 01/"
    if case == "nul":
        prefix = "W/\0/"
    before = tree(tmp_path)

    issues, _ = asyncio.run(
        service.transfer_preview(lib, "^", prefix, samples=["W.S01E01.mkv"])
    )

    assert issues
    assert tree(tmp_path) == before


@pytest.mark.parametrize("root_alias", [False, True])
def test_library_symlinks(tmp_path, root_alias):
    lib = shows(tmp_path)
    root = Path(lib.dir)
    storage = tmp_path / "storage"
    storage.mkdir()
    if root_alias:
        root.symlink_to(storage, target_is_directory=True)
    else:
        root.mkdir()
    (root / "Series").mkdir()
    (root / "Alias").symlink_to(root / "Series", target_is_directory=True)

    issues, preview = asyncio.run(
        service.transfer_preview(lib, "^", "Alias/Season 01/", files=["W.S01E01.mkv"])
    )

    assert issues == []
    assert located(preview) == [("Alias/Season 01/W.S01E01.mkv", 1, 1)]


def test_normalized_collision(tmp_path):
    files = ["W.S01E01.mkv", "W.S01E02.mkv"]
    replacement = "W/Season 01/{{ 'part/..' if episode == 1 else '.' }}/W.S01E01.mkv"

    issues, _ = asyncio.run(
        service.transfer_preview(shows(tmp_path), "^.*$", replacement, files=files)
    )

    assert issues
    assert any("destination" in issue["message"] for issue in issues)


@pytest.mark.parametrize("episode", [2, None])
def test_episode_mismatch(tmp_path, episode):
    name = "W.S01E02.mkv" if episode is not None else "Pilot.mkv"
    issues, preview = asyncio.run(
        service.transfer_preview(
            shows(tmp_path), "^.*$", f"W/Season 01/{name}", files=["W.S01E01.mkv"]
        )
    )

    assert preview["items"][0]["episode"] == episode
    assert any("episode" in issue["message"] for issue in issues)


@pytest.mark.parametrize(
    "case",
    [
        "symlink",
        "season",
        "collision",
        "extras",
        "normalized",
        "episode",
        "unnumbered",
        "extension",
    ],
)
def test_actual_file_checks(tmp_path, case):
    async def run():
        async with database():
            _, downloader, _ = await fixture_plan()
            root, downloads, outside = (
                tmp_path / "shows",
                tmp_path / "downloads",
                tmp_path / "outside",
            )
            for directory in (root, downloads, outside):
                directory.mkdir()
            files = ["W.S02E01.mkv", "W.S02E02.mkv"]
            for file in files:
                (downloads / file).write_bytes(file.encode())
            if case == "symlink":
                (root / "W").symlink_to(outside, target_is_directory=True)
            lib = await MediaLib.create(
                lib_type=LibType.TV_SHOW, dir=str(root), name="Shows", priority=1
            )
            pattern, replacement = {
                "symlink": ("^", "W/Season 02/"),
                "season": ("^", "W/"),
                "collision": ("^.*$", "W/Season 02/same.mkv"),
                "extras": ("^", "W/Season 02/Extras/"),
                "normalized": (
                    "^.*$",
                    "W/Season 02/{{ 'part/..' if episode == 1 else '.' }}/same.mkv",
                ),
                "episode": ("^.*$", "W/Season 02/W.S02E{{ episode + 10 }}.mkv"),
                "unnumbered": (
                    "^.*$",
                    "W/Season 02/{{ 'First' if episode == 1 else 'Second' }}.mkv",
                ),
                "extension": ("^.*$", "W/Season 02/{{ episode }}.txt"),
            }[case]
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(downloads),
                name="W",
                files=files,
                state=DownloadState.COMPLETED,
                transfer_lib_id=lib.id,
                transfer_method=TransferMethod.COPY,
                sub_pattern=pattern,
                sub_repl=replacement,
            )
            before = [tree(directory) for directory in (root, downloads, outside)]

            assert not await syncer.transfer_files(task, files)
            await task.refresh_from_db()
            assert not task.transfer_targets
            assert [
                tree(directory) for directory in (root, downloads, outside)
            ] == before

    asyncio.run(run())


def test_late_companion_batch(tmp_path):
    async def run():
        async with database():
            _, downloader, _ = await fixture_plan()
            root, downloads = tmp_path / "shows", tmp_path / "downloads"
            files = ["W.S01E01.mkv", "W.S01E01.en.srt", "Extras/Featurette.mkv"]
            for name in files:
                source = downloads / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(name.encode())
            lib = await MediaLib.create(
                lib_type=LibType.TV_SHOW, dir=str(root), name="Shows", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(downloads),
                name="W",
                files=files,
                state=DownloadState.COMPLETED,
                transfer_lib_id=lib.id,
                transfer_method=TransferMethod.COPY,
                sub_pattern="^",
                sub_repl="W/Season 01/",
            )

            assert await syncer.transfer_files(task, files[:1])
            assert await syncer.transfer_files(task, files[1:])
            await task.refresh_from_db()
            assert task.transfer_targets == {
                name: str(root / "W/Season 01" / name) for name in files
            }
            assert sorted(tree(root)) == sorted(f"W/Season 01/{name}" for name in files)

    asyncio.run(run())


@pytest.mark.parametrize("partial", [False, True])
def test_recorded_rule_error(tmp_path, partial):
    async def run():
        async with database():
            _, downloader, _ = await fixture_plan()
            root, downloads = tmp_path / "shows", tmp_path / "downloads"
            downloads.mkdir()
            name = "W.S01E01.mkv"
            (downloads / name).write_bytes(b"episode")
            lib = await MediaLib.create(
                lib_type=LibType.TV_SHOW, dir=str(root), name="Shows", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(downloads),
                name="W",
                files=[name],
                state=DownloadState.COMPLETED,
                transfer_lib_id=lib.id,
                transfer_method=TransferMethod.COPY,
                sub_pattern="^",
                sub_repl="W/Season 01/",
            )
            assert await syncer.transfer_files(task, task.files)
            await task.refresh_from_db()
            recorded = dict(task.transfer_targets)
            task.sub_pattern = "("
            if partial:
                extra = "W.S01E02.mkv"
                (downloads / extra).write_bytes(b"next episode")
                task.files.append(extra)
                await task.save(update_fields=["files"])
            await task.save(update_fields=["sub_pattern"])

            assert await syncer.transfer_files(task, task.files) is not partial
            await task.refresh_from_db()
            assert task.transfer_targets == recorded

    asyncio.run(run())


@pytest.mark.parametrize("method", [TransferMethod.COPY, TransferMethod.HARDLINK])
@pytest.mark.parametrize("case", ["escape", "conflict"])
def test_runtime_checks(tmp_path, method, case):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "W.S01E01.mkv").write_bytes(b"episode")
    root = tmp_path / "shows"
    unrelated = root / "W" / "Season 01" / "W.S01E01.mkv"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unrelated")
    repl = "../outside/" if case == "escape" else "W/Season 01/"

    async def run():
        async with database():
            _, downloader, _ = await fixture_plan()
            lib = await MediaLib.create(
                lib_type=LibType.TV_SHOW, dir=str(root), name="Shows", priority=1
            )
            task = await DownloadTask.create(
                downloader=downloader,
                dir=str(downloads),
                name="W",
                files=["W.S01E01.mkv"],
                state=DownloadState.COMPLETED,
                transfer_lib_id=lib.id,
                transfer_method=method,
                sub_pattern="^",
                sub_repl=repl,
            )

            assert not await syncer.transfer_files(task, task.files)
            await task.refresh_from_db()
            assert not task.transfer_targets
            assert not (tmp_path / "outside").exists()
            assert unrelated.read_bytes() == b"unrelated"
            assert (downloads / "W.S01E01.mkv").read_bytes() == b"episode"

    asyncio.run(run())


def test_one_time(monkeypatch, tmp_path):
    driver = Rpc()
    engine_fixture(monkeypatch, RESULTS)

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(pack_site)) as client,
        ):
            data = await libraries(tmp_path)
            items = await searched(owner, data.graph.id, monkeypatch, driver, client)
            base = settings(
                data.graph.id,
                items["r2"],
                data.downloader.id,
                transfer_lib_id=data.shows.id,
                transfer_method="hardlink",
            )
            fields = {**base, **RULE}

            async def validate(values):
                return await service.DownloadTaskService.validate_request(
                    DownloadTaskCheck.model_validate(values), owner
                )

            plain = await validate(base)
            check = await validate({**fields, "transfer_samples": ["Other.mkv"]})
            triggers = await bindings(data.shows.id)
            body = DownloadTaskCreate(
                request_id=check["request_id"],
                info_hash=check["download"]["info_hash"],
                **fields,
            )
            unconfirmed = await service.DownloadTaskService.create_once(body, owner)
            confirmed = body.model_copy(update={"confirmed": True})
            created = await service.DownloadTaskService.create_once(confirmed, owner)
            replayed = await service.DownloadTaskService.create_once(confirmed, owner)
            changed = await service.DownloadTaskService.create_once(
                confirmed.model_copy(update={"sub_repl": "Wayfinder/"}), owner
            )

            messages = [issue["message"] for issue in plain["issues"]]
            assert not plain["valid"]
            assert any("states season 2" in message for message in messages)
            assert any("states season 0" in message for message in messages)
            assert check["valid"], check["issues"]
            transfer = check["download"]["transfer"]
            # the torrent's own file list wins over supplied samples
            assert transfer["basis"] == "known_files" and transfer["rule"] == "applied"
            assert located(transfer) == [
                ("Wayfinder (2024)/Season 02/Wayfinder.S02E01.mkv", 2, 1),
                ("Wayfinder (2024)/Season 02/Wayfinder.S02E01.en.srt", None, None),
                ("Wayfinder (2024)/Season 00/Wayfinder.S00E01.mkv", 0, 1),
            ]
            assert (
                check["download"]["sub_pattern"],
                check["download"]["sub_repl"],
            ) == (
                RULE["sub_pattern"],
                RULE["sub_repl"],
            )
            assert unconfirmed["confirmation_required"]
            assert unconfirmed["download"]["transfer"] == transfer
            assert created["created"] and created["download"]["transfer"] == transfer
            assert replayed["replayed"] and replayed["task_id"] == created["task_id"]
            assert changed["conflict"] and not changed["created"]
            task = await DownloadTask.get(id=created["task_id"])
            assert (task.sub_pattern, task.sub_repl) == tuple(RULE.values())
            assert len(driver.added) == 1
            # validation and submission leave the library and its bindings untouched
            assert not (tmp_path / "shows").exists()
            assert not await MediaItem.exists() and not await FlowLog.exists()
            assert await bindings(data.shows.id) == triggers

    asyncio.run(run())


def test_legacy_receipt(monkeypatch, tmp_path):
    driver = Rpc()
    engine_fixture(monkeypatch, RESULTS)

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(pack_site)) as client,
        ):
            data = await libraries(tmp_path)
            items = await searched(owner, data.graph.id, monkeypatch, driver, client)
            body = DownloadTaskCreate(
                request_id=uuid4(),
                info_hash=decode_torrent(PACK_TORRENT).info_hash,
                confirmed=True,
                **settings(data.graph.id, items["r2"], data.downloader.id),
            )
            source = await service.open_source(body.resource)
            # receipts recorded before transfer rules existed hash these fields only
            legacy = {
                **body.model_dump(
                    mode="json",
                    exclude={"request_id", "confirmed", "sub_pattern", "sub_repl"},
                ),
                "resource": {
                    "workflow_id": data.graph.id,
                    "resource_id": "r2",
                    "link": hashlib.sha256(source["link"].encode()).hexdigest(),
                },
                "ingestion": None,
            }
            task = await DownloadTask.create(
                downloader=data.downloader,
                dir=body.dir,
                name=PACK,
                state=DownloadState.DOWNLOADING,
                creation_key=hashlib.sha256(
                    f"{owner.id}:{body.request_id}".encode()
                ).hexdigest(),
                creation_receipt={
                    "digest": hashlib.sha256(
                        orjson.dumps(legacy, option=orjson.OPT_SORT_KEYS)
                    ).hexdigest(),
                    "download": {"workflow_id": data.graph.id, "transfer_lib_id": None},
                    "ingest_workflow_id": None,
                },
            )

            replayed = await service.DownloadTaskService.create_once(body, owner)

            assert replayed["replayed"] and replayed["task_id"] == task.id
            assert driver.added == []

    asyncio.run(run())


def test_plan(tmp_path):
    async def run():
        async with database() as (owner, _):
            data = await libraries(tmp_path)
            plan = data.body.plan.model_copy(
                update={
                    "transfer_lib_id": data.shows.id,
                    "transfer_method": TransferMethod.HARDLINK,
                    "sub_pattern": "^",
                    "sub_repl": SEASON,
                }
            )
            samples = ["Wayfinder.S02E03.1080p.mkv", "Wayfinder.S00E01.mkv"]

            async def validate(settings, **fields):
                return await service.DownloadPlanService.validate_creation(
                    DownloadPlanCheck(plan=settings, **fields), owner
                )

            check = await validate(plan, transfer_samples=samples)
            unnamed = await validate(plan)
            root = await validate(
                plan.model_copy(update={"sub_pattern": None, "sub_repl": None}),
                transfer_samples=samples[:1],
            )
            orphan = await validate(plan.model_copy(update={"transfer_lib_id": None}))
            invalid = await service.DownloadPlanService.create_once(
                DownloadPlanCreate(
                    request_id=uuid4(),
                    plan=plan.model_copy(update={"sub_pattern": "("}),
                    confirmed=True,
                ),
                owner,
            )
            request_id = uuid4()
            unconfirmed = await service.DownloadPlanService.create_once(
                DownloadPlanCreate(request_id=request_id, plan=plan), owner
            )
            created = await service.DownloadPlanService.create_once(
                DownloadPlanCreate(request_id=request_id, plan=plan, confirmed=True),
                owner,
            )
            with pytest.raises(BadRequestException):
                await service.DownloadPlanService.create_once(
                    DownloadPlanCreate(
                        request_id=request_id,
                        plan=plan.model_copy(update={"sub_repl": "Wayfinder/"}),
                        confirmed=True,
                    ),
                    owner,
                )

            assert check["valid"] and check["plan"]["sub_repl"] == SEASON
            assert check["transfer"]["basis"] == "samples"
            assert located(check["transfer"]) == [
                ("Wayfinder (2024)/Season 02/Wayfinder.S02E03.1080p.mkv", 2, 3),
                ("Wayfinder (2024)/Season 00/Wayfinder.S00E01.mkv", 0, 1),
            ]
            # without names the rule is only parsed and the TV layout is explained
            assert unnamed["valid"] and unnamed["transfer"]["basis"] is None
            assert "Supply transfer_samples" in unnamed["transfer"]["notes"][0]
            assert not root["valid"]
            assert "TV ingestion indexes only" in root["issues"][0]["message"]
            assert orphan["issues"][-1]["path"] == "transfer_lib_id"
            assert not invalid["created"]
            assert invalid["issues"][0]["path"] == "sub_pattern"
            assert unconfirmed["confirmation_required"]
            assert unconfirmed["transfer"] == unnamed["transfer"]
            assert created["created"] and created["plan"]["sub_repl"] == SEASON
            assert await DownloadPlan.all().count() == 1

    asyncio.run(run())


def test_parity(monkeypatch, tmp_path):
    driver = Rpc()
    engine_fixture(monkeypatch, RESULTS)

    async def run():
        async with (
            application() as app,
            httpx.AsyncClient(transport=httpx.MockTransport(pack_site)) as client,
        ):
            data = await libraries(tmp_path)
            admin = app.shared_ctx.sessions["admin"]
            items = await searched(admin, data.graph.id, monkeypatch, driver, client)
            fields = {
                **settings(
                    data.graph.id,
                    items["r2"],
                    data.downloader.id,
                    transfer_lib_id=data.shows.id,
                    transfer_method="hardlink",
                ),
                **RULE,
            }
            plan = {
                "plan": data.body.plan.model_dump(mode="json")
                | {"transfer_lib_id": data.shows.id, "sub_pattern": "^"},
                "transfer_samples": ["Wayfinder.S02E03.mkv"],
            }
            plan["plan"]["sub_repl"] = SEASON

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
            mcp_plan = await call(
                app, "validate_download_plan", {"validation": plan}, token="admin"
            )
            assistant_plan = await tools.execute_tool(
                "validate_download_plan",
                dumps(plan).decode(),
                admin,
                conversation_id=1,
                call_id="plan",
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
                MCPAccessTokenCreate(name="tv", scope=MCPTokenScope.READ_WRITE),
            )
            created = await call(
                app, "create_download_task", {"creation": body}, token=write["token"]
            )
            user = SimpleNamespace(
                id=admin.id,
                role=UserRole.USER,
                perms=Permissions(
                    indexer_ids=[data.graph.id], media_lib_ids=[data.shows.id]
                ),
            )
            with pytest.raises(ForbiddenException):
                await service.DownloadTaskService.validate_request(
                    DownloadTaskCheck.model_validate(fields), user
                )
            denied = await tools.execute_tool(
                "validate_download_task",
                dumps(fields).decode(),
                user,
                conversation_id=1,
                call_id="denied",
            )

            assert not mcp.get("isError"), mcp
            mcp_data = mcp["structuredContent"]
            assert mcp_data.pop("request_id") and assistant["data"].pop("request_id")
            assert mcp_data == loads(dumps(assistant["data"]))
            assert mcp_data["download"]["transfer"]["basis"] == "known_files"
            planned = mcp_plan["structuredContent"]
            assert planned.pop("request_id") and assistant_plan["data"].pop(
                "request_id"
            )
            assert planned == loads(dumps(assistant_plan["data"]))
            assert located(planned["transfer"]) == [
                ("Wayfinder (2024)/Season 02/Wayfinder.S02E03.mkv", 2, 3)
            ]
            assert read_only["isError"] and "read-only" in str(read_only)
            assert created["structuredContent"]["created"]
            task = await DownloadTask.get(id=created["structuredContent"]["task_id"])
            assert task.sub_repl == SEASON and len(driver.added) == 1
            assert "error" in denied and "data" not in denied

    asyncio.run(run())


@pytest.mark.parametrize(
    ("name", "season", "number", "title"),
    [
        ("The Wayfinder Journals S01E02.mkv", 1, 2, "The Island Without a Name"),
        ("The Wayfinder Journals S00E01.mkv", 0, 1, "Charting the Journals"),
    ],
    ids=["episode", "special"],
)
def test_ingestion(tmp_path, monkeypatch, name, season, number, title):
    mimetypes.add_type(NFO_MIME_TYPE, ".nfo")
    monkeypatch.setattr("app.services.media.create_task", lambda task: task.close())
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / name).write_bytes(b"video")
    rule = {
        "sub_pattern": "^",
        "sub_repl": "The Wayfinder Journals (2024)/Season {{ '%02d' % season }}/",
    }

    def site(request):
        return httpx.Response(
            200, text=EPISODE_HTML if request.url.path == "/episodes" else SHOW_HTML
        )

    async def run():
        async with (
            database() as (owner, _),
            httpx.AsyncClient(transport=httpx.MockTransport(site)) as client,
        ):
            app = SimpleNamespace(ctx=SimpleNamespace(httpx=client))
            monkeypatch.setattr(Sanic, "get_app", lambda *_, **__: app)
            _ingest_engine(monkeypatch)
            data = await tv_libraries(tmp_path)
            (tmp_path / "shows").mkdir()
            validation = await reviewed(owner, data, data.series.id)
            await service.DownloadPlanService.create_once(
                creation(data, validation), owner
            )
            issues, preview = await service.transfer_preview(
                data.shows, *rule.values(), files=[name]
            )
            # the downloader finished a single-file torrent at its directory root
            task = await DownloadTask.create(
                downloader=data.downloader,
                dir=str(downloads),
                name=name,
                files=[name],
                state=DownloadState.COMPLETED,
                transfer_lib_id=data.shows.id,
                transfer_method=TransferMethod.COPY,
                **rule,
            )

            assert issues == []
            assert await FlowTrigger.filter(graph_id=data.series.id).exists()
            assert await syncer.transfer_files(task, task.files)
            video = tmp_path / "shows" / preview["items"][0]["to"]
            assert video.read_bytes() == b"video"
            event = await MediaEvent.create(
                lib=data.shows, src_path=str(video), event_type="created"
            )
            await asyncio.wait_for(watcher.consume_event(event), timeout=5)
            for nfo in (
                video.parent / f"{video.parent.name}.nfo",
                video.with_suffix(".nfo"),
            ):
                written = await MediaEvent.create(
                    lib=data.shows, src_path=str(nfo), event_type="created"
                )
                await asyncio.wait_for(watcher.consume_event(written), timeout=5)

            episode = etree.parse(video.with_suffix(".nfo")).getroot()
            assert episode.findtext("title") == title
            item = await MediaItem.get(path=str(video))
            assert (item.season, item.episode) == (season, number)
            assert (preview["items"][0]["season"], preview["items"][0]["episode"]) == (
                season,
                number,
            )

    asyncio.run(run())
