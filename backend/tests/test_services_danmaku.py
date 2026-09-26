"""Test danmaku matching, cache updates, and server error recovery."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from filelock import Timeout
from tortoise import Tortoise

from app.core.config import KaloscopeConfig
from app.core.media.coordination import library_lock
from app.models.media import LibType, MediaItem, MediaLib
from app.services import danmaku


@pytest.fixture
def library(monkeypatch, tmp_path):
    monkeypatch.setattr(KaloscopeConfig, "get_workspace", lambda _name: str(tmp_path))

    @asynccontextmanager
    async def create(handler, lib_type=LibType.TV_SHOW, server="https://danmaku.test"):
        await Tortoise.init(
            db_url="sqlite://:memory:", modules={"models": ["app.models"]}
        )
        await Tortoise.generate_schemas()
        try:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True
            ) as client:
                app = SimpleNamespace(ctx=SimpleNamespace(httpx=client))
                monkeypatch.setattr(danmaku.Sanic, "get_app", lambda: app)
                lib = await MediaLib.create(
                    name="Anime",
                    dir=str(tmp_path),
                    priority=1,
                    lib_type=lib_type,
                    danmaku_server=server,
                )
                yield lib
        finally:
            await Tortoise.close_connections()

    return create


async def media(lib, name, *, parent=None, episode=None, anime_id: str | int = "old"):
    return await MediaItem.create(
        lib=lib,
        parent=parent,
        dir=lib.dir,
        path=f"{lib.dir}/{name}",
        name=name,
        episode=episode,
        danmaku_meta=(
            {
                "anime_id": anime_id,
                "episode_id": f"old-{episode}",
                "type": "tvseries",
            }
            if episode is not None
            else None
        ),
    )


@pytest.mark.parametrize("suffix", ["", "/api/v2/", "/token/v2"])
def test_anime_search(library, suffix):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "animes": [
                    {
                        "animeId": 42,
                        "animeTitle": "New Anime",
                        "type": "tvseries",
                        "typeDescription": "TV动画",
                    },
                    {"animeId": "source-43", "animeTitle": "Another Anime"},
                ],
            },
        )

    async def run():
        async with library(handler, server=f"https://danmaku.test{suffix}") as lib:
            item = await media(lib, "Series")
            return await danmaku.DanmakuService.search_anime(item.path, "动画 & 续集")

    results = asyncio.run(run())

    assert [result.anime_id for result in results] == ["42", "source-43"]
    assert results[0].anime_title == "New Anime"
    assert results[0].type_description == "TV动画"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.path == (
        "/token/v2/search/anime" if suffix == "/token/v2" else "/api/v2/search/anime"
    )
    assert dict(requests[0].url.params) == {"keyword": "动画 & 续集"}


@pytest.mark.parametrize(
    "search",
    [danmaku.DanmakuService.search_anime, danmaku.DanmakuService.search_episodes],
)
@pytest.mark.parametrize(
    ("status", "data"),
    [
        (503, {"success": False}),
        (200, {"success": False}),
        (200, {"success": True, "animes": None}),
    ],
)
def test_search_failures(library, search, status, data):
    async def run():
        async with library(lambda _: httpx.Response(status, json=data)) as lib:
            item = await media(lib, "Series")
            return await search(item.path, "Anime")

    assert asyncio.run(run()) == []


def test_empty_episode_lists(library):
    async def run():
        async with library(
            lambda _: httpx.Response(
                200,
                json={"success": True, "animes": [{"animeId": 42, "episodes": None}]},
            )
        ) as lib:
            item = await media(lib, "Series")
            return await danmaku.DanmakuService.search_episodes(item.path, "Anime")

    assert asyncio.run(run()) == []


def test_unconfigured_server(library):
    def handler(request):
        pytest.fail(f"unconfigured library should not request {request.url}")

    async def run():
        async with library(handler, server=None) as lib:
            item = await media(lib, "Series")
            results = await danmaku.DanmakuService.search_anime(item.path, "Anime")
            confirmed = await danmaku.DanmakuService.confirm_anime(
                item.path, danmaku.DanmakuAnime(anime_id="new", type="tvseries")
            )
            return results, confirmed

    assert asyncio.run(run()) == ([], False)


def test_proxy_query_encoding(library):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "animes": []})

    async def run():
        async with library(
            handler, server="https://proxy.test/?url=https://danmaku.test/api/v2"
        ) as lib:
            item = await media(lib, "Series")
            return await danmaku.DanmakuService.search_anime(item.path, "Anime & 2")

    assert asyncio.run(run()) == []
    assert dict(requests[0].url.params) == {
        "url": "https://danmaku.test/api/v2/search/anime?keyword=Anime+%26+2"
    }


def test_replaces_matches_and_caches(library, tmp_path):
    def handler(request):
        assert request.url.path == "/api/v2/bangumi/new"
        return httpx.Response(
            200,
            json={
                "success": True,
                "bangumi": {
                    "episodes": [
                        {
                            "episodeNumber": "2",
                            "episodeId": "new-2",
                            "episodeTitle": "Second",
                        },
                        {
                            "episodeNumber": 1,
                            "episodeId": "new-1",
                            "episodeTitle": "First",
                        },
                    ]
                },
            },
        )

    async def run():
        async with library(handler) as lib:
            parent = await media(lib, "Series")
            episodes = [
                await media(lib, f"{number}.mkv", parent=parent, episode=number)
                for number in (1, 2, 3)
            ]
            for item in episodes:
                cache = tmp_path / f".{item.name}.json"
                cache.write_text("[]")
                if item.episode != 1:
                    await MediaItem.filter(id=item.id).update(danmaku_path=str(cache))
            other_parent = await media(lib, "Other Series")
            other = await media(lib, "other.mkv", parent=other_parent, episode=1)
            result = await danmaku.DanmakuService.confirm_anime(
                parent.path,
                danmaku.DanmakuAnime(
                    anime_id="new", anime_title="New Anime", type="tvseries"
                ),
            )
            for item in episodes:
                await item.refresh_from_db()
            await other.refresh_from_db()
            return result, episodes, other

    result, episodes, other = asyncio.run(run())

    assert result is True
    for item, episode_id in zip(episodes[:2], ["new-1", "new-2"], strict=True):
        assert item.danmaku_meta is not None
        assert item.danmaku_meta["episode_id"] == episode_id
        assert item.danmaku_meta["anime_title"] == "New Anime"
    assert episodes[2].danmaku_meta is None
    assert all(item.danmaku_path is None for item in episodes)
    assert not list(tmp_path.glob(".*.json"))
    assert other.danmaku_meta is not None
    assert other.danmaku_meta["anime_id"] == "old"


def test_preserves_manual_matches(library, tmp_path):
    def handler(request):
        pytest.fail(f"unchanged anime should not request {request.url}")

    async def run():
        async with library(handler) as lib:
            parent = await media(lib, "Series")
            item = await media(lib, "1.mkv", parent=parent, episode=1, anime_id=42)
            cache = tmp_path / ".1.mkv.json"
            cache.write_text("[]")
            result = await danmaku.DanmakuService.confirm_anime(
                parent.path, danmaku.DanmakuAnime(anime_id="42", type="tvseries")
            )
            await item.refresh_from_db()
            return result, item, cache

    result, item, cache = asyncio.run(run())

    assert result is True
    assert item.danmaku_meta is not None
    assert item.danmaku_meta["episode_id"] == "old-1"
    assert cache.exists()


@pytest.mark.parametrize(
    ("status", "data"),
    [
        (503, {}),
        (200, {"success": False, "errorMessage": "Unavailable"}),
        (200, {"success": True, "bangumi": None}),
        (200, {"success": True, "bangumi": {"episodes": None}}),
        (200, {"success": True, "bangumi": {"episodes": []}}),
        (
            200,
            {
                "success": True,
                "bangumi": {"episodes": [{"episodeNumber": "2", "episodeId": "new-2"}]},
            },
        ),
        (
            200,
            {
                "success": True,
                "bangumi": {
                    "episodes": [
                        {
                            "seasonId": "season-1",
                            "episodeNumber": "1",
                            "episodeId": "new-1",
                        },
                        {
                            "seasonId": "season-2",
                            "episodeNumber": "1",
                            "episodeId": "new-2",
                        },
                    ]
                },
            },
        ),
    ],
)
def test_unusable_bangumi(library, tmp_path, status, data):
    async def run():
        async with library(lambda _: httpx.Response(status, json=data)) as lib:
            parent = await media(lib, "Series")
            item = await media(lib, "1.mkv", parent=parent, episode=1)
            cache = tmp_path / ".1.mkv.json"
            cache.write_text("[]")
            result = await danmaku.DanmakuService.confirm_anime(
                parent.path, danmaku.DanmakuAnime(anime_id="new", type="tvseries")
            )
            await item.refresh_from_db()
            return result, item, cache

    result, item, cache = asyncio.run(run())

    assert result is False
    assert item.danmaku_meta is not None
    assert item.danmaku_meta["anime_id"] == "old"
    assert cache.exists()


@pytest.mark.parametrize("with_parent", [False, True])
@pytest.mark.parametrize(
    ("episodes", "episode_id"),
    [
        ([{"episodeId": 420001, "episodeTitle": "Movie"}], "420001"),
        (
            [
                {"episodeNumber": "S1", "episodeId": 429001},
                {"episodeNumber": "1", "episodeId": 420001, "episodeTitle": "Movie"},
                {"episodeNumber": "C1", "episodeId": 429101},
            ],
            "420001",
        ),
        (
            [
                {"episodeNumber": "S1", "episodeId": 429001},
                {"episodeNumber": "C1", "episodeId": 429101},
            ],
            None,
        ),
        (
            [
                {"episodeNumber": "1", "episodeId": 420001},
                {"episodeNumber": "2", "episodeId": 420002},
                {"episodeNumber": "S1", "episodeId": 429001},
            ],
            None,
        ),
    ],
)
def test_movie_matches(library, with_parent, episodes, episode_id):
    def handler(request):
        assert request.url.path == "/api/v2/bangumi/42"
        return httpx.Response(
            200,
            json={"success": True, "bangumi": {"episodes": episodes}},
        )

    async def run():
        async with library(handler, lib_type=LibType.MOVIE) as lib:
            parent = await media(lib, "Movie") if with_parent else None
            item = await media(lib, "movie.mkv", parent=parent)
            result = await danmaku.DanmakuService.confirm_anime(
                (parent or item).path,
                danmaku.DanmakuAnime.model_validate({"anime_id": 42, "type": "movie"}),
            )
            await item.refresh_from_db()
            return result, item

    result, item = asyncio.run(run())

    assert result is (episode_id is not None)
    if episode_id is not None:
        assert item.danmaku_meta is not None
        assert item.danmaku_meta["episode_id"] == episode_id
    else:
        assert item.danmaku_meta is None


@pytest.mark.parametrize("has_sibling_match", [False, True])
def test_episode_refreshes_siblings(library, tmp_path, has_sibling_match):
    def handler(request):
        if request.url.path == "/api/v2/comment/new-1":
            return httpx.Response(
                200, json={"comments": [{"cid": 1, "p": "1,1,16777215,1", "m": "Hi"}]}
            )
        assert request.url.path == "/api/v2/bangumi/new"
        episodes = [{"episodeNumber": 1, "episodeId": "new-1"}]
        if has_sibling_match:
            episodes.append({"episodeNumber": 2, "episodeId": "new-2"})
        return httpx.Response(
            200,
            json={"success": True, "bangumi": {"episodes": episodes}},
        )

    async def run():
        async with library(handler) as lib:
            parent = await media(lib, "Series")
            first = await media(lib, "1.mkv", parent=parent, episode=1)
            second = await media(lib, "2.mkv", parent=parent, episode=2)
            cache = tmp_path / ".2.mkv.json"
            cache.write_text('[{"text": "Old comments"}]')
            result = await danmaku.DanmakuService.confirm_episode(
                first.path,
                danmaku.DanmakuMeta(
                    anime_id="new", episode_id="new-1", type="tvseries"
                ),
            )
            await first.refresh_from_db()
            await second.refresh_from_db()
            return result, first, second

    result, first, second = asyncio.run(run())

    assert result.comments[0].text == "Hi"
    assert first.danmaku_meta is not None
    assert first.danmaku_meta["episode_id"] == "new-1"
    assert first.danmaku_path is not None
    if has_sibling_match:
        assert second.danmaku_meta is not None
        assert second.danmaku_meta["episode_id"] == "new-2"
    else:
        assert second.danmaku_meta is None
    assert second.danmaku_path is None
    assert not (tmp_path / ".2.mkv.json").exists()


def test_sibling_refresh_retry(library, tmp_path):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/api/v2/comment/new-1":
            return httpx.Response(
                200, json={"comments": [{"cid": 1, "p": "1,1,16777215,1", "m": "Hi"}]}
            )
        assert request.url.path == "/api/v2/bangumi/new"
        if requests.count("/api/v2/bangumi/new") == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "success": True,
                "bangumi": {
                    "episodes": [
                        {"episodeNumber": 2, "episodeId": "new-2"},
                        {"episodeNumber": 3, "episodeId": "new-3"},
                    ]
                },
            },
        )

    async def run():
        async with library(handler) as lib:
            parent = await media(lib, "Series")
            first = await media(lib, "1.mkv", parent=parent, episode=1)
            second = await media(lib, "2.mkv", parent=parent, episode=2)
            third = await media(lib, "3.mkv", parent=parent, episode=3, anime_id="new")
            cache = tmp_path / ".2.mkv.json"
            cache.write_text('[{"text": "Old comments"}]')
            await MediaItem.filter(id=second.id).update(danmaku_path=str(cache))
            manual_cache = tmp_path / ".3.mkv.json"
            manual_cache.write_text('[{"text": "Manual comments"}]')
            assert third.danmaku_meta is not None
            third.danmaku_meta["episode_id"] = "manual-3"
            await MediaItem.filter(id=third.id).update(
                danmaku_meta=third.danmaku_meta, danmaku_path=str(manual_cache)
            )
            meta = danmaku.DanmakuMeta(
                anime_id="new", episode_id="new-1", type="tvseries"
            )

            await danmaku.DanmakuService.confirm_episode(first.path, meta)
            await first.refresh_from_db()
            await second.refresh_from_db()
            assert first.danmaku_meta is not None
            assert first.danmaku_meta["episode_id"] == "new-1"
            assert second.danmaku_meta is not None
            assert second.danmaku_meta["episode_id"] == "old-2"
            assert second.danmaku_path == str(cache)
            assert cache.exists()

            await danmaku.DanmakuService.confirm_episode(first.path, meta)
            await second.refresh_from_db()
            assert second.danmaku_meta is not None
            assert second.danmaku_meta["episode_id"] == "new-2"
            assert second.danmaku_path is None
            assert not cache.exists()

            await danmaku.DanmakuService.confirm_episode(first.path, meta)
            await third.refresh_from_db()
            assert third.danmaku_meta is not None
            assert third.danmaku_meta["episode_id"] == "manual-3"
            assert third.danmaku_path == str(manual_cache)
            assert manual_cache.read_text() == '[{"text": "Manual comments"}]'

    asyncio.run(run())

    assert requests.count("/api/v2/bangumi/new") == 2


@pytest.mark.parametrize("status", [200, 503])
def test_override_without_comments(library, tmp_path, status):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(status, json={"comments": []})

    async def run():
        async with library(handler) as lib:
            parent = await media(lib, "Series")
            item = await media(lib, "1.mkv", parent=parent, episode=1, anime_id="new")
            cache = tmp_path / ".1.mkv.json"
            cache.write_text('[{"text": "Old comments"}]')
            await MediaItem.filter(id=item.id).update(danmaku_path=str(cache))
            result = await danmaku.DanmakuService.confirm_episode(
                item.path,
                danmaku.DanmakuMeta(
                    anime_id="new", episode_id="manual-1", type="tvseries"
                ),
            )
            await item.refresh_from_db()
            assert item.danmaku_meta is not None
            assert item.danmaku_meta["episode_id"] == "manual-1"
            assert item.danmaku_path is None
            assert not cache.exists()
            assert result.comments == []
            playback = await danmaku.DanmakuService.match_danmakus(item.path)
            assert playback.metadata is not None
            assert playback.metadata.episode_id == "manual-1"
            assert playback.comments == []

    asyncio.run(run())

    assert requests == ["/api/v2/comment/manual-1", "/api/v2/comment/manual-1"]


@pytest.mark.parametrize(
    ("suffix", "prefix"),
    [
        ("", "/api/v2"),
        ("/token", "/token/api/v2"),
        ("/api/v1/token", "/api/v1/token/api/v2"),
    ],
)
def test_confirmed_anime_playback(library, suffix, prefix):
    def handler(request):
        assert request.method == "GET"
        if request.url.path == f"{prefix}/search/anime":
            assert dict(request.url.params) == {"keyword": "Anime"}
            return httpx.Response(
                200,
                json={"success": True, "animes": [{"animeId": "new"}]},
            )
        if request.url.path == f"{prefix}/bangumi/new":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bangumi": {
                        "episodes": [{"episodeNumber": 1, "episodeId": "new-1"}]
                    },
                },
            )
        if request.url.path == f"{prefix}/comment/new-1":
            assert dict(request.url.params) == {"withRelated": "true"}
            return httpx.Response(
                302, headers={"Location": "https://comments.test/new-1"}
            )
        assert str(request.url) == "https://comments.test/new-1"
        return httpx.Response(
            200,
            json={"comments": [{"cid": 7, "p": "3,1,16777215,1", "m": "New"}]},
        )

    async def run():
        async with library(handler, server=f"https://danmaku.test{suffix}") as lib:
            parent = await media(lib, "Series")
            item = await media(lib, "1.mkv", parent=parent, episode=1)
            results = await danmaku.DanmakuService.search_anime(parent.path, "Anime")
            assert await danmaku.DanmakuService.confirm_anime(parent.path, results[0])
            return await danmaku.DanmakuService.match_danmakus(item.path)

    result = asyncio.run(run())

    assert result.metadata is not None
    assert result.metadata.episode_id == "new-1"
    assert result.comments[0].text == "New"


@pytest.mark.parametrize("change", ["moved", "removed", "empty"])
def test_confirmation_scope(library, change):
    async def run():
        requests = []
        unrelated = []

        async def handler(request):
            requests.append(request.url.path)
            if request.url.path == "/api/v2/comment/new-1":
                await MediaItem.filter(id=first.id).update(parent_id=other.id)
                if second is not None:
                    if change == "removed":
                        await second.delete()
                    else:
                        await MediaItem.filter(id=second.id).update(parent_id=other.id)
                unrelated.append(await media(lib, "new.mkv", parent=parent, episode=3))
                return httpx.Response(200, json={"comments": []})

            assert request.url.path == "/api/v2/bangumi/new"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bangumi": {
                        "episodes": [
                            {"episodeNumber": number, "episodeId": f"new-{number}"}
                            for number in (2, 3, 4)
                        ]
                    },
                },
            )

        async with library(handler) as lib:
            parent = await media(lib, "Series")
            other = await media(lib, "Other")
            first = await media(lib, "1.mkv", parent=parent, episode=1)
            second = (
                None
                if change == "empty"
                else await media(lib, "2.mkv", parent=parent, episode=2)
            )
            unrelated.append(await media(lib, "other.mkv", parent=other, episode=4))
            meta = danmaku.DanmakuMeta(
                anime_id="new", episode_id="new-1", type="tvseries"
            )

            result = await danmaku.DanmakuService.confirm_episode(first.path, meta)

            await first.refresh_from_db()
            assert result.metadata == meta
            assert first.danmaku_meta["episode_id"] == "new-1"
            if second is not None:
                if change == "removed":
                    assert not await MediaItem.filter(id=second.id).exists()
                else:
                    await second.refresh_from_db()
                    assert second.parent_id == other.id
                    assert second.danmaku_meta["episode_id"] == "new-2"
            for item in unrelated:
                await item.refresh_from_db()
                assert item.danmaku_meta["anime_id"] == "old"
                assert item.danmaku_meta["episode_id"] == f"old-{item.episode}"
            expected = ["/api/v2/comment/new-1"]
            if change == "moved":
                expected.append("/api/v2/bangumi/new")
            assert requests == expected

    asyncio.run(run())


@pytest.mark.parametrize("change", ["unchanged", "moved", "removed"])
@pytest.mark.parametrize("recorded_path", [False, True])
def test_cache_deletion(library, tmp_path, monkeypatch, change, recorded_path):
    def handler(request):
        pytest.fail(f"cache deletion should not request {request.url}")

    async def run():
        async with library(handler) as lib:
            item = await media(lib, "1.mkv", episode=1)
            cache = tmp_path / (
                "cached.json" if recorded_path else f".{item.name}.json"
            )
            cache.write_text("original")
            if recorded_path:
                await MediaItem.filter(id=item.id).update(danmaku_path=str(cache))
            metadata = item.danmaku_meta
            current_cache = cache
            waiting = asyncio.Event()

            def waiting_lock(directory):
                waiting.set()
                return library_lock(directory)

            monkeypatch.setattr(danmaku, "library_lock", waiting_lock)
            deletion = None
            try:
                async with library_lock(lib.dir):
                    deletion = asyncio.create_task(
                        danmaku.DanmakuService.delete_danmakus(item.path)
                    )
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    assert not deletion.done()
                    assert cache.read_text() == "original"

                    if change == "moved":
                        directory = tmp_path / "Renamed"
                        directory.mkdir()
                        current_cache = directory / (
                            "cached.json" if recorded_path else ".Renamed.mkv.json"
                        )
                        cache.rename(current_cache)
                        await MediaItem.filter(id=item.id).update(
                            path=str(directory / "Renamed.mkv"),
                            dir=str(directory),
                            name="Renamed.mkv",
                            danmaku_path=str(current_cache) if recorded_path else None,
                        )
                        await media(lib, "1.mkv", episode=2)
                        cache.write_text("replacement")
                    elif change == "removed":
                        await item.delete()

                await asyncio.wait_for(deletion, timeout=3)
                current = await MediaItem.get_or_none(id=item.id)

                if change == "removed":
                    assert current is None
                    assert cache.read_text() == "original"
                else:
                    assert current is not None
                    assert current.danmaku_path is None
                    assert current.danmaku_meta == metadata
                    assert not current_cache.exists()
                    if change == "moved":
                        assert cache.read_text() == "replacement"
            finally:
                if deletion is not None:
                    if not deletion.done():
                        deletion.cancel()
                    await asyncio.gather(deletion, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["moved", "removed", "confirmed"])
@pytest.mark.parametrize("recorded_path", [False, True])
def test_refresh_wait(library, tmp_path, monkeypatch, change, recorded_path):
    async def run():
        loading, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(request):
            assert request.url.path == "/api/v2/bangumi/new"
            loading.set()
            await release.wait()
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bangumi": {
                        "episodes": [
                            {"episodeNumber": number, "episodeId": f"new-{number}"}
                            for number in range(1, 5)
                        ]
                    },
                },
            )

        def waiting_lock(directory):
            waiting.set()
            return library_lock(directory)

        monkeypatch.setattr(danmaku, "library_lock", waiting_lock)
        async with library(handler) as lib:
            parent = await media(lib, "Series")
            destination = await media(lib, "Other Series")
            item = await media(lib, "1.mkv", parent=parent, episode=1)
            other = await media(lib, "other.mkv", parent=destination, episode=4)
            cache = tmp_path / (
                "cached.json" if recorded_path else f".{item.name}.json"
            )
            cache.write_text("original")
            if recorded_path:
                await MediaItem.filter(id=item.id).update(danmaku_path=str(cache))
            current_cache = cache
            metadata = {"anime_id": "new", "episode_id": "manual", "type": "tvseries"}
            refresh = asyncio.create_task(
                danmaku.DanmakuService.refresh_episodes(
                    parent, danmaku.DanmakuAnime(anime_id="new", type="tvseries")
                )
            )

            try:
                await asyncio.wait_for(loading.wait(), timeout=3)
                async with await library_lock(lib.dir).acquire(timeout=1):
                    release.set()
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    assert not refresh.done()
                    assert cache.read_text() == "original"

                    if change == "moved":
                        directory = tmp_path / "Renamed"
                        directory.mkdir()
                        current_cache = directory / (
                            "cached.json" if recorded_path else ".Renamed.mkv.json"
                        )
                        cache.rename(current_cache)
                        await MediaItem.filter(id=item.id).update(
                            parent_id=destination.id,
                            path=str(directory / "Renamed.mkv"),
                            dir=str(directory),
                            name="Renamed.mkv",
                            episode=2,
                            danmaku_path=str(current_cache) if recorded_path else None,
                        )
                        cache.write_text("replacement")
                    elif change == "removed":
                        await item.delete()
                    else:
                        await MediaItem.filter(id=item.id).update(
                            danmaku_meta=metadata, danmaku_path=str(cache)
                        )
                        cache.write_text("manual")
                    added = await media(lib, "added.mkv", parent=parent, episode=3)

                assert await asyncio.wait_for(refresh, timeout=3) is True
                current = await MediaItem.get_or_none(id=item.id)

                if change == "removed":
                    assert current is None
                    assert cache.read_text() == "original"
                elif change == "confirmed":
                    assert current.danmaku_meta == metadata
                    assert current.danmaku_path == str(cache)
                    assert cache.read_text() == "manual"
                else:
                    assert current.parent_id == destination.id
                    assert current.danmaku_meta["episode_id"] == "new-2"
                    assert current.danmaku_path is None
                    assert not current_cache.exists()
                    assert cache.read_text() == "replacement"
                for untouched in (other, added):
                    await untouched.refresh_from_db()
                    assert untouched.danmaku_meta["anime_id"] == "old"
            finally:
                release.set()
                if not refresh.done():
                    refresh.cancel()
                await asyncio.gather(refresh, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("has_comments", [False, True])
@pytest.mark.parametrize("replacement", [{"hash": "new"}, {"size": 20}])
def test_confirmation_replacement(library, tmp_path, has_comments, replacement):
    async def run():
        loading = asyncio.Event()
        release = asyncio.Event()
        requests = []

        async def handler(request):
            requests.append(request.url.path)
            if request.url.path == "/api/v2/comment/selected-1":
                loading.set()
                await release.wait()
                comments = (
                    [{"cid": 1, "p": "1,1,16777215,1", "m": "Stale comments"}]
                    if has_comments
                    else []
                )
                return httpx.Response(200, json={"comments": comments})
            assert request.url.path == "/api/v2/bangumi/selected"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "bangumi": {
                        "episodes": [{"episodeNumber": 2, "episodeId": "selected-2"}]
                    },
                },
            )

        async with library(handler) as lib:
            parent = await media(lib, "Series")
            first = await media(lib, "1.mkv", parent=parent, episode=1)
            second = await media(lib, "2.mkv", parent=parent, episode=2)
            await MediaItem.filter(id=first.id).update(hash="old", size=10)
            cache = tmp_path / ".1.mkv.json"
            sibling_cache = tmp_path / ".2.mkv.json"
            sibling_content = '[{"text": "Sibling comments"}]'
            sibling_cache.write_text(sibling_content)
            sibling_metadata = second.danmaku_meta
            await MediaItem.filter(id=second.id).update(danmaku_path=str(sibling_cache))
            pending = asyncio.create_task(
                danmaku.DanmakuService.confirm_episode(
                    first.path,
                    danmaku.DanmakuMeta(
                        anime_id="selected", episode_id="selected-1", type="tvseries"
                    ),
                )
            )
            try:
                await asyncio.wait_for(loading.wait(), timeout=5)
                async with library_lock(lib.dir):
                    await MediaItem.filter(id=first.id).update(
                        **replacement, danmaku_meta=None, danmaku_path=None
                    )
            finally:
                release.set()

            result = await pending
            await first.refresh_from_db()
            await second.refresh_from_db()

            assert result.metadata is None
            assert result.comments == []
            assert first.danmaku_meta is None
            assert first.danmaku_path is None
            assert not cache.exists()
            assert second.danmaku_meta == sibling_metadata
            assert second.danmaku_path == str(sibling_cache)
            assert sibling_cache.read_text() == sibling_content
            assert requests == ["/api/v2/comment/selected-1"]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["moved", "removed"])
@pytest.mark.parametrize("has_comments", [False, True])
def test_confirmation_wait(library, tmp_path, monkeypatch, change, has_comments):
    async def run():
        loading, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(request):
            assert request.url.path == "/api/v2/comment/selected"
            loading.set()
            await release.wait()
            comments = (
                [{"cid": 1, "p": "1,1,16777215,1", "m": "New"}] if has_comments else []
            )
            return httpx.Response(200, json={"comments": comments})

        def waiting_lock(directory):
            if release.is_set():
                waiting.set()
            return library_lock(directory)

        monkeypatch.setattr(danmaku, "library_lock", waiting_lock)
        async with library(handler, lib_type=LibType.MOVIE) as lib:
            item = await media(lib, "movie.mkv")
            cache = tmp_path / "cached.json"
            cache.write_text('[{"text":"Original"}]')
            await MediaItem.filter(id=item.id).update(danmaku_path=str(cache))
            meta = danmaku.DanmakuMeta(
                anime_id="new", episode_id="selected", type="movie"
            )
            confirmation = asyncio.create_task(
                danmaku.DanmakuService.confirm_episode(item.path, meta)
            )
            try:
                await asyncio.wait_for(loading.wait(), timeout=3)
                async with await library_lock(lib.dir).acquire(timeout=1):
                    release.set()
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    assert not confirmation.done()
                    assert cache.read_text() == '[{"text":"Original"}]'

                    if change == "moved":
                        directory = tmp_path / "Renamed"
                        directory.mkdir()
                        current_cache = directory / cache.name
                        cache.rename(current_cache)
                        await MediaItem.filter(id=item.id).update(
                            path=str(directory / "Renamed.mkv"),
                            dir=str(directory),
                            name="Renamed",
                            danmaku_path=str(current_cache),
                        )
                        cache.write_text('[{"text":"Replacement"}]')
                    else:
                        await item.delete()

                result = await asyncio.wait_for(confirmation, timeout=3)
                current = await MediaItem.get_or_none(id=item.id)

                if change == "removed":
                    assert current is None
                    assert result.comments == []
                    assert cache.read_text() == '[{"text":"Original"}]'
                else:
                    assert result.metadata == meta
                    assert current.danmaku_meta["episode_id"] == "selected"
                    assert cache.read_text() == '[{"text":"Replacement"}]'
                    if has_comments:
                        assert [comment.text for comment in result.comments] == ["New"]
                        assert current.danmaku_path == str(current_cache)
                        assert (
                            await danmaku.DanmakuService.load_from_cache(current_cache)
                            == result.comments
                        )
                    else:
                        assert result.comments == []
                        assert current.danmaku_path is None
                        assert not current_cache.exists()
            finally:
                release.set()
                if not confirmation.done():
                    confirmation.cancel()
                await asyncio.gather(confirmation, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("cancellations", [1, 2])
@pytest.mark.parametrize("operation", ["confirm", "match"])
def test_cache_cancellation(library, tmp_path, monkeypatch, cancellations, operation):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    cache = tmp_path / ".movie.mkv.json"
    write_bytes = Path.write_bytes

    def delayed_write(path, content):
        if path != cache:
            return write_bytes(path, content)
        started.set()
        assert release.wait(timeout=5)
        try:
            return write_bytes(path, content)
        finally:
            finished.set()

    monkeypatch.setattr(Path, "write_bytes", delayed_write)

    async def run():
        async with library(
            lambda _: httpx.Response(
                200,
                json={"comments": [{"cid": 1, "p": "1,1,16777215,1", "m": "New"}]},
            ),
            lib_type=LibType.MOVIE,
        ) as lib:
            await MediaLib.filter(id=lib.id).update(danmaku_ttl=0)
            item = await media(lib, "movie.mkv")
            metadata = {"anime_id": "old", "episode_id": "previous", "type": "movie"}
            await MediaItem.filter(id=item.id).update(
                danmaku_meta=metadata, danmaku_path=str(cache)
            )
            cache.write_text('[{"text":"Old"}]')
            pending = (
                danmaku.DanmakuService.confirm_episode(
                    item.path,
                    danmaku.DanmakuMeta(
                        anime_id="new", episode_id="selected", type="movie"
                    ),
                )
                if operation == "confirm"
                else danmaku.DanmakuService.match_danmakus(item.path)
            )
            request = asyncio.create_task(pending)
            try:
                assert await asyncio.to_thread(started.wait, 3)
                for _ in range(cancellations):
                    request.cancel()
                    with pytest.raises(Timeout):
                        async with await library_lock(lib.dir).acquire(timeout=0):
                            pass
                    assert not request.done()
                    assert not finished.is_set()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(request, timeout=3)
                async with await library_lock(lib.dir).acquire(timeout=1):
                    assert finished.is_set()
                    assert not cache.exists()
                    await item.refresh_from_db()
                    assert item.danmaku_meta == metadata
                    assert item.danmaku_path == str(cache)
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["confirm", "match"])
def test_cache_cancel_after_write(library, tmp_path, monkeypatch, operation):
    write_cache = danmaku.DanmakuService._write_cache

    async def cancelled_write(media, comments):
        path = await write_cache(media, comments)
        asyncio.current_task().cancel()
        return path

    monkeypatch.setattr(danmaku.DanmakuService, "_write_cache", cancelled_write)

    async def run():
        async with library(
            lambda _: httpx.Response(
                200,
                json={"comments": [{"cid": 1, "p": "1,1,16777215,1", "m": "New"}]},
            ),
            lib_type=LibType.MOVIE,
        ) as lib:
            item = await media(lib, "movie.mkv")
            await MediaItem.filter(id=item.id).update(
                danmaku_meta={
                    "anime_id": "old",
                    "episode_id": "previous",
                    "type": "movie",
                }
            )
            pending = (
                danmaku.DanmakuService.confirm_episode(
                    item.path,
                    danmaku.DanmakuMeta(
                        anime_id="new", episode_id="selected", type="movie"
                    ),
                )
                if operation == "confirm"
                else danmaku.DanmakuService.match_danmakus(item.path)
            )
            request = asyncio.create_task(pending)

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(request, timeout=3)

            async with await library_lock(lib.dir).acquire(timeout=1):
                assert not (tmp_path / ".movie.mkv.json").exists()

    asyncio.run(run())


@pytest.mark.parametrize("pending_path", ["/api/v2/match", "/api/v2/comment/old"])
@pytest.mark.parametrize("replacement", [{"hash": "new"}, {"size": 20}])
def test_content_replacement(library, tmp_path, pending_path, replacement):
    async def run():
        loading = asyncio.Event()
        release = asyncio.Event()
        matches = []

        async def handler(request):
            if request.url.path == pending_path and not release.is_set():
                loading.set()
                await release.wait()
            if request.url.path == "/api/v2/match":
                payload = json.loads(request.content)
                matches.append(payload)
                episode_id = (
                    "old"
                    if (payload["fileHash"], payload["fileSize"]) == ("old", 10)
                    else "new"
                )
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "matches": [
                            {
                                "animeId": "anime",
                                "episodeId": episode_id,
                                "type": "tvseries",
                            }
                        ],
                    },
                )
            assert request.url.path in (
                "/api/v2/comment/old",
                "/api/v2/comment/new",
            )
            return httpx.Response(
                200,
                json={
                    "comments": [
                        {
                            "cid": 1,
                            "p": "1,1,16777215,1",
                            "m": request.url.path.rsplit("/", 1)[-1],
                        }
                    ]
                },
            )

        async with library(handler) as lib:
            item = await media(lib, "video.mkv")
            await MediaItem.filter(id=item.id).update(hash="old", size=10)
            cache = tmp_path / ".video.mkv.json"
            pending = asyncio.create_task(
                danmaku.DanmakuService.match_danmakus(item.path)
            )
            try:
                await asyncio.wait_for(loading.wait(), timeout=5)
                async with library_lock(lib.dir):
                    await MediaItem.filter(id=item.id).update(
                        **replacement, danmaku_meta=None, danmaku_path=None
                    )
            finally:
                release.set()

            stale = await pending
            await item.refresh_from_db()

            assert stale.metadata is None
            assert stale.comments == []
            assert item.danmaku_meta is None
            assert item.danmaku_path is None
            assert not cache.exists()

            current = await danmaku.DanmakuService.match_danmakus(item.path)
            await item.refresh_from_db()

            assert current.metadata is not None
            assert current.metadata.episode_id == "new"
            assert [comment.text for comment in current.comments] == ["new"]
            assert item.danmaku_meta is not None
            assert item.danmaku_meta["episode_id"] == "new"
            assert item.danmaku_path == str(cache)
            assert [comment["text"] for comment in json.loads(cache.read_text())] == [
                "new"
            ]
            assert [(match["fileHash"], match["fileSize"]) for match in matches] == [
                ("old", 10),
                (item.hash, item.size),
            ]

    asyncio.run(run())


@pytest.mark.parametrize("change", ["moved", "removed", "confirmed"])
@pytest.mark.parametrize("has_comments", [False, True])
def test_match_wait(library, tmp_path, monkeypatch, change, has_comments):
    async def run():
        loading, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handler(request):
            assert request.url.path == "/api/v2/comment/previous"
            loading.set()
            await release.wait()
            comments = (
                [{"cid": 1, "p": "1,1,16777215,1", "m": "Fetched"}]
                if has_comments
                else []
            )
            return httpx.Response(200, json={"comments": comments})

        def waiting_lock(directory):
            if release.is_set():
                waiting.set()
            return library_lock(directory)

        monkeypatch.setattr(danmaku, "library_lock", waiting_lock)
        async with library(handler, lib_type=LibType.MOVIE) as lib:
            await MediaLib.filter(id=lib.id).update(danmaku_ttl=0)
            item = await media(lib, "movie.mkv")
            cache = tmp_path / "cached.json"
            cache.write_text('[{"text":"Original"}]')
            metadata = danmaku.DanmakuMeta(
                anime_id="old", episode_id="previous", type="movie"
            )
            await MediaItem.filter(id=item.id).update(
                danmaku_meta=metadata.model_dump(), danmaku_path=str(cache)
            )
            current_cache = cache
            request = asyncio.create_task(
                danmaku.DanmakuService.match_danmakus(item.path)
            )
            try:
                await asyncio.wait_for(loading.wait(), timeout=3)
                async with await library_lock(lib.dir).acquire(timeout=1):
                    release.set()
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    assert not request.done()
                    assert cache.read_text() == '[{"text":"Original"}]'

                    if change == "moved":
                        directory = tmp_path / "Renamed"
                        directory.mkdir()
                        current_cache = directory / cache.name
                        cache.rename(current_cache)
                        await MediaItem.filter(id=item.id).update(
                            path=str(directory / "Renamed.mkv"),
                            dir=str(directory),
                            name="Renamed",
                            danmaku_path=str(current_cache),
                        )
                        cache.write_text('[{"text":"Replacement"}]')
                    elif change == "removed":
                        await item.delete()
                    else:
                        metadata = metadata.model_copy(update={"episode_id": "manual"})
                        await MediaItem.filter(id=item.id).update(
                            danmaku_meta=metadata.model_dump()
                        )
                        cache.write_text('[{"text":"Manual"}]')

                result = await asyncio.wait_for(request, timeout=3)
                current = await MediaItem.get_or_none(id=item.id)

                if change == "removed":
                    assert current is None
                    assert result.metadata is None
                    assert result.comments == []
                    assert cache.read_text() == '[{"text":"Original"}]'
                else:
                    assert result.metadata == metadata
                    assert current.danmaku_meta == metadata.model_dump()
                    assert current.danmaku_path == str(current_cache)
                    expected = (
                        "Manual"
                        if change == "confirmed"
                        else "Fetched"
                        if has_comments
                        else "Original"
                    )
                    assert [comment.text for comment in result.comments] == [expected]
                    assert (
                        await danmaku.DanmakuService.load_from_cache(current_cache)
                        == result.comments
                    )
                    if change == "moved":
                        assert cache.read_text() == '[{"text":"Replacement"}]'
            finally:
                release.set()
                if not request.done():
                    request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("change", ["moved", "removed"])
def test_cached_match(library, tmp_path, monkeypatch, change):
    def handler(request):
        pytest.fail(f"cached playback should not request {request.url}")

    async def run():
        async with library(handler) as lib:
            item = await media(lib, "1.mkv", episode=1)
            cache = tmp_path / f".{item.name}.json"
            cache.write_text('[{"text":"Cached"}]')
            waiting = asyncio.Event()

            def waiting_lock(directory):
                waiting.set()
                return library_lock(directory)

            monkeypatch.setattr(danmaku, "library_lock", waiting_lock)
            request = None
            try:
                async with library_lock(lib.dir):
                    request = asyncio.create_task(
                        danmaku.DanmakuService.match_danmakus(item.path)
                    )
                    await asyncio.wait_for(waiting.wait(), timeout=3)
                    assert not request.done()

                    if change == "moved":
                        directory = tmp_path / "Renamed"
                        directory.mkdir()
                        current_cache = directory / ".Renamed.json"
                        cache.rename(current_cache)
                        await MediaItem.filter(id=item.id).update(
                            path=str(directory / "Renamed.mkv"),
                            dir=str(directory),
                            name="Renamed",
                        )
                        cache.write_text('[{"text":"Replacement"}]')
                    else:
                        await item.delete()

                result = await asyncio.wait_for(request, timeout=3)

                if change == "removed":
                    assert result.metadata is None
                    assert result.comments == []
                    assert cache.read_text() == '[{"text":"Cached"}]'
                else:
                    assert result.metadata.episode_id == "old-1"
                    assert [comment.text for comment in result.comments] == ["Cached"]
                    assert current_cache.read_text() == '[{"text":"Cached"}]'
                    assert cache.read_text() == '[{"text":"Replacement"}]'
            finally:
                if request is not None:
                    if not request.done():
                        request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    asyncio.run(run())
