"""Tests for personal search favorites across application, assistant and MCP."""

import asyncio

import pytest

from app.core.ai import tools
from app.core.exceptions import ForbiddenException, UnauthorizedException
from app.models.mcp import MCPAccessTokenCreate, MCPTokenScope
from app.models.user import PermType, UserFavorite, UserPermission
from app.routes import flow, user
from app.services.mcp import MCPAccessTokenService
from app.utils.json import dumps, loads
from tests.test_download_plan_authoring import fixture_plan
from tests.test_mcp import application as mcp_application
from tests.test_mcp import call
from tests.test_routes_permissions import application as application
from tests.test_routes_permissions import request

pytestmark = pytest.mark.usefixtures("application")


def test_shared_favorites():
    async def run():
        async with mcp_application() as app:
            graph, _, _ = await fixture_plan()
            reader = app.shared_ctx.sessions["reader"]
            payload = {
                "rsrc_id": "book/1",
                "rsrc": {
                    "id": "book/1",
                    "title": "The book",
                    "link": "private-fixture",
                },
            }
            with pytest.raises(UnauthorizedException):
                await flow.favorite_resource(
                    request(None, "POST", payload), id=graph.id
                )
            with pytest.raises(ForbiddenException):
                await flow.favorite_resource(
                    request(reader, "POST", payload), id=graph.id
                )
            grant = await UserPermission.create(
                user_id=reader.id, rel_type=PermType.INDEXER, rel_id=graph.id
            )
            await flow.favorite_resource(request(reader, "POST", payload), id=graph.id)
            admin = app.shared_ctx.sessions["admin"]
            await flow.favorite_resource(request(admin, "POST", payload), id=graph.id)
            query = {"workflow_id": graph.id}
            items = await call(app, "list_indexer_favorites", {"query": query})
            assert items["structuredContent"]["items"] == [
                {"workflow_id": graph.id, "resource_id": "book/1", "title": "The book"}
            ]
            assert b"private-fixture" not in dumps(items)
            change = {
                "workflow_id": graph.id,
                "resource_id": "book/1",
                "title": "Different",
                "favorite": True,
            }
            blocked = await call(app, "set_indexer_favorite", {"change": change})
            assert blocked["isError"]
            token = (
                await MCPAccessTokenService.create(
                    admin.id,
                    MCPAccessTokenCreate(name="rw", scope=MCPTokenScope.READ_WRITE),
                )
            )["token"]
            saved = await call(
                app, "set_indexer_favorite", {"change": change}, token=token
            )
            assert saved["structuredContent"]["favorite"]
            assert (await UserFavorite.get(user_id=admin.id)).rsrc == payload["rsrc"]
            another = await tools.execute_tool(
                "set_indexer_favorite",
                dumps({**change, "resource_id": "book/2"}),
                admin,
                conversation_id=1,
                call_id="favorite",
            )
            assert another["ok"] and another["data"]["favorite"]
            listed = loads((await user.list_favorites(request(admin, "POST", {}))).body)
            assert listed["total"] == 2
            removed = await call(
                app,
                "set_indexer_favorite",
                {"change": {**change, "favorite": False}},
                token=token,
            )
            assert not removed["structuredContent"]["favorite"]
            await flow.unfavorite_resource(request(admin, "POST", payload), id=graph.id)
            assert await UserFavorite.filter(user_id=admin.id).count() == 1
            await grant.delete()
            assert (
                loads((await user.list_favorites(request(reader, "POST", {}))).body)[
                    "items"
                ]
                == []
            )
            with pytest.raises(ForbiddenException):
                await flow.unfavorite_resource(
                    request(reader, "POST", payload), id=graph.id
                )
            assert await UserFavorite.filter(user_id=reader.id).count() == 1

    asyncio.run(run())
