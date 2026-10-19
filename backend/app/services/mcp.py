"""Manage independent MCP credentials and resolve their current account identity."""

import hashlib
import re
import secrets
from datetime import timedelta
from typing import Any

from tortoise import timezone

from app.core.exceptions import NotFoundException, UnauthorizedException
from app.models.mcp import MCPAccessToken, MCPAccessTokenCreate, MCPTokenScope
from app.models.user import UserInfo
from app.services.base import BaseService

_TOKEN_PREFIX = "ks_mcp_"
_TOKEN_PATTERN = re.compile(_TOKEN_PREFIX + r"[A-Za-z0-9_-]{43}")


class MCPAccessTokenService(BaseService[MCPAccessToken], model=MCPAccessToken):
    """Create, inspect, delete, and authenticate MCP-only access tokens."""

    @classmethod
    async def create(cls, user_id: int, body: MCPAccessTokenCreate) -> dict[str, Any]:
        """Issue a read-only credential and return its plaintext exactly once.

        Args:
            user_id: Account owning the new credential.
            body: Validated label and lifetime, defaulting to 90 days.

        Returns:
            Public token metadata plus the one-time `token` value.
        """
        token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
        record = await MCPAccessToken.create(
            user_id=user_id,
            name=body.name,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            prefix=token[:17],
            scope=MCPTokenScope.READ_ONLY,
            expires_at=timezone.now() + timedelta(days=body.expires_in_days),
        )
        return {**await cls.dump(record), "token": token}

    @classmethod
    async def list_tokens(cls, user_id: int) -> list[dict[str, Any]]:
        """List an account's token metadata without credentials or digests.

        Args:
            user_id: Account whose active and expired tokens to list.

        Returns:
            Token metadata ordered with the newest first.
        """
        return await cls.dump_list(MCPAccessToken.filter(user_id=user_id))

    @classmethod
    async def delete(cls, id: int, user_id: int) -> None:
        """Delete one owned token regardless of its expiry.

        Args:
            id: Token identifier to delete.
            user_id: Account that must own the token.

        Raises:
            NotFoundException: If the token is missing or belongs to another user.
        """
        if not await MCPAccessToken.filter(id=id, user_id=user_id).delete():
            raise NotFoundException

    @classmethod
    async def authenticate(
        cls, token: str | None, *, client_ip: str, user_agent: str | None
    ) -> UserInfo:
        """Resolve an active credential to its owner's current account identity.

        Resource permissions are loaded by the existing `authorize` decorator.
        Usage updates never extend the fixed expiry or create a login session.

        Args:
            token: Bearer credential; missing or malformed values are rejected.
            client_ip: Address of the requesting MCP client.
            user_agent: Optional client identification header.

        Returns:
            Current account identity suitable for existing authorization services.

        Raises:
            UnauthorizedException: If the token is invalid, expired, deleted, or
                no longer belongs to an existing account.
        """
        if not token or not _TOKEN_PATTERN.fullmatch(token):
            raise UnauthorizedException(headers={"WWW-Authenticate": "Bearer"})
        now = timezone.now()
        active = MCPAccessToken.filter(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            scope=MCPTokenScope.READ_ONLY,
            expires_at__gt=now,
        )
        record = await active.select_related("user").first()
        if record is None:
            raise UnauthorizedException(headers={"WWW-Authenticate": "Bearer"})
        # recheck existence when recording usage, without restoring deleted records
        if not await active.update(last_used_at=now):
            raise UnauthorizedException(headers={"WWW-Authenticate": "Bearer"})
        user = record.user
        return UserInfo(
            id=user.id,
            login_id=f"mcp:{record.id}",
            username=user.username,
            avatar=user.avatar,
            role=user.role,
            preferences=user.preferences if isinstance(user.preferences, dict) else {},
            client_ip=client_ip,
            user_agent=user_agent,
            login_at=record.created_at,
            expire_at=record.expires_at,
            last_activity=now,
        )
