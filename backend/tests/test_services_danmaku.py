"""Test danmaku matching, cache updates, and server error recovery."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from tortoise import Tortoise

from app.models.media import LibType, MediaItem, MediaLib
from app.services import danmaku


@pytest.fixture
def library(monkeypatch, tmp_path):
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
