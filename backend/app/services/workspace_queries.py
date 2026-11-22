"""Expose read-only administrator workspace diagnostics and template listings."""

import asyncio
from collections import Counter

from sanic import Sanic

from app.core.dl.config import decrypt_config, load_driver
from app.core.exceptions import BadRequestException, ForbiddenException
from app.core.transcode.tasks import list_tasks
from app.models.agent_actions import WorkspaceQuery
from app.models.download import Downloader
from app.models.flow import FlowTemplate
from app.models.mcp import MCPTokenDomain
from app.models.media import MediaAction, MediaChangeReceipt
from app.models.user import UserInfo
from app.services.review_common import require_live_admin

WORKSPACE_TOPICS = {
    MCPTokenDomain.SYSTEM: frozenset({"logs", "transcoding", "downloaders"}),
    MCPTokenDomain.WORKFLOWS: frozenset({"workflow_templates", "receipt"}),
}


async def _health(row: Downloader) -> dict:
    """Probe one configured driver without exposing connection details.

    Args:
        row: Saved downloader configuration.

    Returns:
        Name, ID and observed version-check outcome.
    """
    version = None
    try:
        driver = load_driver(decrypt_config(row.config))
        try:
            if driver.supports_version:
                version = await asyncio.wait_for(driver.version(), 3)
                status = "up" if version else "down"
            else:
                status = "unsupported"
        finally:
            await driver.close()
    except Exception:
        status = "unavailable"
    return {"id": row.id, "name": row.name, "status": status, "version": version}


async def inspect_workspace(query: WorkspaceQuery, user: UserInfo) -> dict:
    """Query operational facts with bounded pages and server-defined fields.

    Args:
        query: Diagnostic topic or template page.
        user: Authenticated administrator.

    Returns:
        Facts observed by the backend for the selected topic.

    Raises:
        ForbiddenException: If the role or token domain cannot read this topic.
        BadRequestException: If a receipt query omits its operation ID.
    """
    await require_live_admin(user)
    domain = getattr(user, "mcp_domain", MCPTokenDomain.ALL)
    if domain != MCPTokenDomain.ALL and query.topic not in WORKSPACE_TOPICS.get(
        domain, ()
    ):
        raise ForbiddenException
    offset = (query.page - 1) * 20
    if query.topic == "receipt":
        if query.operation_id is None:
            raise BadRequestException
        from app.services.agent_actions import action_status

        return await action_status(query.operation_id, user)
    if query.topic == "batches":
        rows = MediaChangeReceipt.filter(action=MediaAction.BATCH).order_by("-id")
        return {
            "total": await rows.count(),
            "items": await rows.offset(offset)
            .limit(20)
            .values("id", "lib_id", "updated_at"),
        }
    if query.topic == "workflow_templates":
        rows = FlowTemplate.filter(newest=True).order_by("name", "id")
        return {
            "total": await rows.count(),
            "items": await rows.offset(offset)
            .limit(20)
            .values("id", "name", "category", "revision"),
        }
    if query.topic == "downloaders":
        rows = await Downloader.all().offset(offset).limit(20)
        return {
            "total": await Downloader.all().count(),
            "items": await asyncio.gather(*(_health(row) for row in rows)),
        }
    if query.topic == "transcoding":
        rows = await list_tasks()
        return {
            "total": len(rows),
            "items": [
                {
                    k: row.get(k)
                    for k in ("id", "state", "profile", "progress", "hwaccel")
                }
                for row in rows[offset : offset + 20]
            ],
        }
    if query.topic == "logs":
        shared = getattr(Sanic.get_app().shared_ctx, "log_snapshot", {})
        snapshot = shared.get("value")
        if snapshot is None:
            return {"available": False}
        return {
            "available": True,
            "paused": snapshot.paused,
            "levels": dict(Counter(r["level"] for r in snapshot.records)),
            "retained": len(snapshot.records),
        }
    return {
        "actions": [
            "copy_template",
            "publish_workflow",
            "create_library",
            "delete_library",
            "edit_metadata",
        ],
        "approval": "browser",
    }
