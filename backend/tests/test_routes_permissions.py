"""Unit tests for route role checks and media library permissions."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from sanic import HTTPResponse, Request, Sanic, empty
from sanic.compat import Header
from sanic.models.protocol_types import TransportProtocol

from app.core.exceptions import ErrorCode, ForbiddenException, UnauthorizedException
from app.models.media import LibType, MediaItem, MediaLib
from app.models.user import PermType, User, UserInfo, UserPermission, UserRole
from app.routes import danmaku as danmaku_routes
from app.routes import download as download_routes
from app.routes import filesystem as filesystem_routes
from app.routes import flow as flow_routes
from app.routes import media as media_routes
from app.routes import network as network_routes
from app.routes import subtitle as subtitle_routes
from app.routes import user as user_routes
from app.services.danmaku import DanmakuService, DanmakuWrapper
from app.services.subtitle import SubtitleService
from tests.test_services_media_reading import _database, _user

APP_NAME = "permission-route-test"
VIDEO = "Show.S01E01.mkv"
EPISODE = {"anime_id": "1", "type": "tvseries", "episode_id": "2"}


@pytest.fixture(autouse=True)
def application():
    app = Sanic(APP_NAME, configure_logging=False)
    try:
        yield app
    finally:
        Sanic.unregister_app(app)


def request(
    user: object | None,
    method: str = "GET",
    payload: dict | None = None,
) -> Request:
    """Create a real Sanic request for a decorated route.

    Args:
        user: Session user, or `None` for an unauthenticated request.
        method: HTTP method; `GET` sends the payload as query parameters and other
            methods send it as a JSON body.
        payload: Optional request parameters.

    Returns:
        Request suitable for the authorization and validation decorators.
    """
    url = "/_api/test"
    if method == "GET" and payload:
        url += f"?{urlencode(payload)}"
    result = Request(
        url_bytes=url.encode(),
        headers=Header({"content-type": "application/json"}),
        version="1.1",
        method=method,
        transport=cast(TransportProtocol, None),
        app=Sanic.get_app(APP_NAME),
    )
    if method != "GET":
        result.body = json.dumps(payload or {}).encode()
    result.ctx.user = user
    return result


async def call(handler: object, request: Request, **arguments: object) -> HTTPResponse:
    """Call a decorated route using its runtime asynchronous handler contract.

    Args:
        handler: Route callable whose signature is obscured by Sanic decorators.
        request: Request passed through authorization and validation.
        **arguments: URL parameters supplied by the router.

    Returns:
        Response produced by the decorated route.
    """
    callback = cast(Callable[..., Awaitable[HTTPResponse]], handler)
    return await callback(request, **arguments)


async def library(directory: Path, access: str) -> UserInfo:
    """Create a video item and a session with the requested library access.

    Args:
        directory: Library directory that receives the video file.
        access: `admin`, `granted` or `denied`.

    Returns:
        Session user whose permissions `authorize` reloads from the database.
    """
    directory.mkdir()
    video = directory / VIDEO
    video.write_bytes(b"video")
    lib = await MediaLib.create(
        name="Library", dir=str(directory), lib_type=LibType.TV_SHOW, priority=1
    )
    await MediaItem.create(lib=lib, path=str(video), dir=lib.dir, name=video.stem)
    session = _user() if access == "admin" else _user([])
    await User.create(
        id=session.id, username=session.username, password="unused", role=session.role
    )
    if access == "granted":
        await UserPermission.create(
            user_id=session.id, rel_type=PermType.MEDIA_LIB, rel_id=lib.id
        )
    return session


@pytest.mark.parametrize(
    ("handler", "arguments"),
    [
        pytest.param(filesystem_routes.list_files, {}, id="filesystem-list"),
        pytest.param(filesystem_routes.get_stats, {}, id="filesystem-stats"),
        pytest.param(filesystem_routes.create_dir, {}, id="filesystem-mkdir"),
        pytest.param(filesystem_routes.delete_dir, {}, id="filesystem-rmdir"),
        pytest.param(flow_routes.list_repositories, {}, id="flow-repositories"),
        pytest.param(flow_routes.list_templates, {}, id="flow-templates"),
        pytest.param(flow_routes.gen_graph_name, {}, id="flow-name"),
        pytest.param(download_routes.list_downloaders, {}, id="download-managers"),
        pytest.param(download_routes.sort_downloaders, {}, id="download-sort"),
        pytest.param(download_routes.get_downloader_presets, {}, id="download-presets"),
        pytest.param(download_routes.list_directories, {}, id="download-dirs"),
        pytest.param(download_routes.list_tasks, {}, id="download-list"),
        pytest.param(download_routes.valid_magnet_link, {}, id="download-validate"),
        pytest.param(download_routes.pause_tasks, {}, id="download-pause"),
        pytest.param(download_routes.start_tasks, {}, id="download-start"),
        pytest.param(download_routes.retry_tasks, {}, id="download-retry"),
        pytest.param(download_routes.get_stats, {}, id="download-stats"),
        pytest.param(network_routes.sort_rules, {}, id="network-rule-sort"),
        pytest.param(media_routes.list_transcodes, {}, id="media-transcodes"),
        pytest.param(user_routes.list_users, {}, id="user-list"),
        pytest.param(user_routes.get_permissions, {"user_id": 1}, id="user-perms"),
    ],
)
@pytest.mark.parametrize(
    ("role", "error", "code"),
    [
        (None, UnauthorizedException, ErrorCode.UNAUTHORIZED),
        (UserRole.USER, ForbiddenException, ErrorCode.FORBIDDEN),
    ],
)
def test_admin_only(handler, arguments, role, error, code):
    session = SimpleNamespace(role=role) if role else None

    with pytest.raises(error) as exc_info:
        asyncio.run(call(handler, request(session), **arguments))

    assert exc_info.value.message == code


@pytest.mark.parametrize(
    ("handler", "method", "extra", "service", "result"),
    [
        pytest.param(
            media_routes.get_item_title, "GET", {}, None, None, id="media-title"
        ),
        pytest.param(
            media_routes.probe_media_metadata,
            "GET",
            {},
            (media_routes, "probe_media"),
            SimpleNamespace(duration=1, chapters=[]),
            id="media-probe",
        ),
        pytest.param(
            media_routes.get_item_stream,
            "GET",
            {},
            (media_routes, "file_stream"),
            empty(),
            id="media-stream",
        ),
        pytest.param(
            subtitle_routes.list_tracks,
            "POST",
            {},
            (SubtitleService, "list_tracks"),
            [],
            id="subtitle-tracks",
        ),
        pytest.param(
            subtitle_routes.load_content,
            "GET",
            {"stream": 2},
            (SubtitleService, "load_content"),
            ("WEBVTT\n", SubtitleService.WEBVTT_CONTENT_TYPE),
            id="subtitle-embedded",
        ),
        pytest.param(
            danmaku_routes.match_danmakus,
            "POST",
            {},
            (DanmakuService, "match_danmakus"),
            DanmakuWrapper(comments=[]),
            id="danmaku-match",
        ),
        pytest.param(
            danmaku_routes.delete_danmakus,
            "POST",
            {},
            (DanmakuService, "delete_danmakus"),
            None,
            id="danmaku-delete",
        ),
        pytest.param(
            danmaku_routes.search_episodes,
            "POST",
            {"title": "Show"},
            (DanmakuService, "search_episodes"),
            [],
            id="danmaku-search",
        ),
        pytest.param(
            danmaku_routes.confirm_episode,
            "POST",
            {"metadata": EPISODE},
            (DanmakuService, "confirm_episode"),
            DanmakuWrapper(comments=[]),
            id="danmaku-confirm",
        ),
        pytest.param(
            danmaku_routes.search_anime,
            "POST",
            {"title": "Show"},
            (DanmakuService, "search_anime"),
            [],
            id="danmaku-anime-search",
        ),
        pytest.param(
            danmaku_routes.confirm_anime,
            "POST",
            {"metadata": {"anime_id": "1", "type": "tvseries"}},
            (DanmakuService, "confirm_anime"),
            True,
            id="danmaku-anime-confirm",
        ),
    ],
)
@pytest.mark.parametrize("access", ["anonymous", "admin", "granted", "denied"])
def test_library_access(
    tmp_path, monkeypatch, handler, method, extra, service, result, access
):
    mock = AsyncMock(return_value=result)
    if service:
        monkeypatch.setattr(*service, mock)
    directory = tmp_path / "library"
    payload = {"path": str(directory / VIDEO), **extra}

    async def run():
        if access == "anonymous":
            with pytest.raises(UnauthorizedException):
                await call(handler, request(None, method, payload))
            return None
        async with _database():
            session = await library(directory, access)
            if access == "denied":
                with pytest.raises(ForbiddenException) as exc_info:
                    await call(handler, request(session, method, payload))
                assert exc_info.value.message == ErrorCode.PERMISSION_DENIED
                return None
            return await call(handler, request(session, method, payload))

    response = asyncio.run(run())

    allowed = access in ("admin", "granted")
    assert mock.await_count == (1 if service and allowed else 0)
    if handler is media_routes.get_item_title and allowed:
        assert response is not None and response.body is not None
        assert json.loads(response.body) == {"title": "Show"}


@pytest.mark.parametrize(
    ("name", "access", "allowed"),
    [
        ("Show.S01E01.srt", "granted", True),
        ("Show.S01E01.zh.srt", "granted", True),
        ("Show.S01E01.zh.srt", "admin", True),
        ("Show.S01E01.zh.srt", "denied", False),
        ("Show.S01E02.srt", "admin", False),
        ("Show.S01E01x.srt", "admin", False),
        ("../outside/Show.S01E01.srt", "admin", False),
        ("Season/Show.S01E01.srt", "admin", False),
    ],
)
def test_external_subtitle(tmp_path, monkeypatch, name, access, allowed):
    mock = AsyncMock(return_value=("WEBVTT\n", SubtitleService.WEBVTT_CONTENT_TYPE))
    monkeypatch.setattr(SubtitleService, "load_content", mock)
    directory = tmp_path / "library"
    payload = {"path": f"{directory}/{name}"}

    async def run():
        async with _database():
            session = await library(directory, access)
            await call(subtitle_routes.load_content, request(session, "GET", payload))

    if allowed:
        asyncio.run(run())
    else:
        with pytest.raises(ForbiddenException) as exc_info:
            asyncio.run(run())
        assert exc_info.value.message == ErrorCode.PERMISSION_DENIED

    assert mock.await_count == (1 if allowed else 0)
