import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiofiles
from sanic import Request
from sanic.request.form import File
from tortoise import timezone
from tortoise.transactions import atomic

from app.core.config import KaloscopeConfig
from app.core.exceptions import ErrorCode, ForbiddenException, KaloscopeException
from app.core.middleware import SessionHolder
from app.models.base import KVPair
from app.models.flow import IndexerResource
from app.models.user import (
    HistoryEntry,
    HistoryType,
    Permissions,
    PermType,
    User,
    UserFavorite,
    UserHistory,
    UserInfo,
    UserPermission,
    UserRole,
)
from app.services.base import BaseService
from app.utils.crypto import encrypt
from app.utils.dict import entries, remove


class UserService(BaseService[User], model=User):
    """The service class for all user related operations."""

    # default preferences for new users
    DEFAULT_PREFERENCES = {
        "homepage": "/dashboard",
        "vibration": False,
        "recent_searches": True,
        "recent_watches": True,
        "search_records": 3,
        "watch_records": 3,
    }

    @classmethod
    async def login(
        cls, username: str, password: str, ambiguity: bool = True
    ) -> UserInfo:
        """Login with the username and password.

        Args:
            username: The entered username.
            password: The entered password.
            ambiguity: Whether to return a more ambiguous error message.

        Raises:
            KaloscopeException: If the user is not found or the password is incorrect.

        Returns:
            The login user object.
        """
        user = await User.get_or_none(username=username)
        if user is None:
            raise KaloscopeException(
                ErrorCode.LOGIN_FAILED if ambiguity else ErrorCode.USER_NOT_FOUND
            )
        if user.password != encrypt(password):
            raise KaloscopeException(
                ErrorCode.LOGIN_FAILED if ambiguity else ErrorCode.INCORRECT_PASSWORD
            )
        # set default preferences
        preferences = user.preferences
        if preferences is None:
            preferences = cls.DEFAULT_PREFERENCES
        elif isinstance(preferences, dict):
            for key, value in cls.DEFAULT_PREFERENCES.items():
                preferences.setdefault(key, value)
        # construct the login user object
        request = Request.get_current()
        now = timezone.now()
        expiration = timedelta(hours=cls.app_config().TOKEN_EXPIRATION_HOURS)
        return UserInfo(
            id=user.id,
            login_id=uuid.uuid4().hex,
            username=user.username,
            avatar=user.avatar,
            role=user.role,
            preferences=preferences,
            user_agent=request.headers.get("User-Agent"),
            client_ip=request.client_ip,
            login_at=now,
            expire_at=now + expiration,
            last_activity=now,
        )

    @classmethod
    async def change_pwd(cls, username: str, cur_pwd: str, new_pwd: str):
        """Change the user's password.

        Args:
            username: The username to change the password.
            cur_pwd: The current password.
            new_pwd: The new password.
        """
        if KaloscopeConfig.get().public_instance_mode:
            raise ForbiddenException()
        user = await cls.login(username, cur_pwd, ambiguity=False)
        await User.filter(id=user.id).update(password=encrypt(new_pwd))
        # remove the user's token
        sessions = SessionHolder.get_sessions()
        remove(sessions, vfilter=lambda u: u.id == user.id)

    @classmethod
    async def change_avatar(cls, id: int, avatar: File | None) -> str:
        """Change the user's avatar.

        Args:
            id: The user ID.
            avatar: The avatar file.

        Returns:
            The avatar file path.
        """
        if avatar is not None:
            # save the avatar file
            avatar_dir = Path(KaloscopeConfig.get_workspace("images")) / "avatars"
            avatar_dir.mkdir(parents=True, exist_ok=True)
            avatar_file = f"{uuid.uuid4().hex}.webp"
            async with aiofiles.open(avatar_dir / avatar_file, "wb") as f:
                await f.write(avatar.body)
            # update the user's avatar file path
            avatar_path = f"avatars/{avatar_file}"
        else:
            avatar_path = ""
        await User.filter(id=id).update(avatar=avatar_path)
        # update the online user's avatar
        sessions = SessionHolder.get_sessions()
        for token, login_user in entries(sessions, vfilter=lambda u: u.id == id):
            login_user.avatar = avatar_path
            sessions[token] = login_user
        return avatar_path

    @classmethod
    @atomic()
    async def update_pref(cls, id: int, pref: KVPair):
        """Update the user's preference.

        Args:
            id: The user ID.
            pref: The preference to update.

        Raises:
            KaloscopeException: If the user is not found.
        """
        # SQLite does not support the `SELECT ... FOR UPDATE` syntax,
        # use the following `UPDATE` statement to acquire a `RESERVED` lock.
        await User.filter(id=id).update(updated_at=timezone.now())
        # get the user's preferences from the database
        user = await User.filter(id=id).select_for_update().first()
        if user is None:
            raise KaloscopeException(ErrorCode.USER_NOT_FOUND)
        preferences = cls.DEFAULT_PREFERENCES.copy()
        if isinstance(user.preferences, dict):
            preferences.update(user.preferences)
        if pref.key in preferences:
            preferences[pref.key] = pref.value
            await User.filter(id=id).update(preferences=preferences)
            # update the online user's preferences
            sessions = SessionHolder.get_sessions()
            for token, login_user in entries(sessions, vfilter=lambda u: u.id == id):
                login_user.preferences = preferences
                sessions[token] = login_user

    @classmethod
    def get_pref(cls, preferences: dict | None, key: str) -> Any:
        """Get a preference value with fallback to the default.

        Args:
            preferences: The user's preferences dict.
            key: The preference key to look up.

        Returns:
            The preference value, or the default value if not set.
        """
        prefs = preferences if isinstance(preferences, dict) else {}
        return prefs.get(key, cls.DEFAULT_PREFERENCES.get(key))

    @classmethod
    async def create(cls, username: str, password: str, role: UserRole = UserRole.USER):
        """Create a new user.

        Args:
            username: The username to create.
            password: The password to create.
            role: The role of the user. Defaults to UserRole.USER.
        """
        if await User.filter(username=username).count() > 0:
            raise KaloscopeException(ErrorCode.USERNAME_ALREADY_EXISTS)
        await User.create(username=username, password=encrypt(password), role=role)


class UserFavoriteService(BaseService[UserFavorite], model=UserFavorite):
    """The service class for all user favorite related operations."""

    @classmethod
    async def favorite(cls, user_id: int, indexer_id: int, rsrc: IndexerResource):
        """Add a new favorite for the user.

        Args:
            user_id: The user ID.
            indexer_id: The indexer ID.
            rsrc: The resource to favorite.
        """
        data = rsrc.rsrc or {}
        data.pop("favorite", None)
        await UserFavorite.update_or_create(
            user_id=user_id,
            indexer_id=indexer_id,
            rsrc_id=rsrc.rsrc_id,
            defaults={"rsrc": data, "url": rsrc.url},
        )

    @classmethod
    async def unfavorite(cls, user_id: int, indexer_id: int, rsrc: IndexerResource):
        """Remove a favorite of the user.

        Args:
            user_id: The user ID.
            indexer_id: The indexer ID.
            rsrc: The resource to unfavorite.
        """
        await UserFavorite.filter(
            user_id=user_id, indexer_id=indexer_id, rsrc_id=rsrc.rsrc_id
        ).delete()


class UserHistoryService(BaseService[UserHistory], model=UserHistory):
    """The service class for all user history related operations."""

    @classmethod
    async def retention_days(cls, user_id: int, rel_type: HistoryType) -> int:
        """Get the maximum retention days for a given history type.

        Args:
            user_id: The user ID.
            rel_type: The history type.

        Returns:
            The maximum retention days, or 0 if not set.
        """
        days = 0
        user = await User.get_or_none(id=user_id)
        if user is None:
            return days

        if rel_type == HistoryType.SEARCH:
            days = UserService.get_pref(user.preferences, "search_records")
        elif rel_type == HistoryType.VIDEO:
            days = UserService.get_pref(user.preferences, "watch_records")
        return days if isinstance(days, int) and days >= 0 else 0

    @classmethod
    async def record(cls, user_id: int, obj: HistoryEntry) -> UserHistory | None:
        """Record a user history entry, incrementing repetitions on duplicate.

        Args:
            user_id: The user ID.
            obj: The history entry data.

        Returns:
            The user history instance, or `None` if the entry is invalid.
        """
        if await cls.retention_days(user_id, obj.rel_type) == 0:
            # do not record history if the retention days is set to 0
            return None

        history = None
        created = False
        if obj.rel_type == HistoryType.VIDEO:
            # record video watch history
            history, created = await UserHistory.update_or_create(
                user_id=user_id,
                rel_type=obj.rel_type,
                rel_id=obj.rel_id,
                defaults={
                    "position": obj.position or 0,
                    "percentage": obj.percentage or 0,
                },
            )
        elif obj.rel_type == HistoryType.SEARCH:
            # record web search history
            keyword = (obj.keyword or "").strip()
            if keyword:
                history, created = await UserHistory.get_or_create(
                    user_id=user_id,
                    rel_type=obj.rel_type,
                    rel_id=obj.rel_id,
                    keyword=keyword,
                )

        # increment repetitions if not created
        if history is not None and not created:
            history.repetitions += 1
            await history.save(update_fields=["repetitions", "updated_at"])

        return history

    @classmethod
    async def clean_expired(cls, user_id: int, rel_type: HistoryType):
        """Delete expired history records based on user preferences.

        Reads `search_records` or `watch_records` from the user's preferences to
        determine the retention window (in days). A value of 0 means no history
        is kept, so all records of the given type are deleted.

        Args:
            user_id: The user ID.
            rel_type: The history type to clean.
        """
        days = await cls.retention_days(user_id, rel_type)
        if days == 0:
            await UserHistory.filter(user_id=user_id, rel_type=rel_type).delete()
        else:
            cutoff = timezone.now() - timedelta(days=days)
            await UserHistory.filter(
                user_id=user_id, rel_type=rel_type, updated_at__lt=cutoff
            ).delete()


class UserPermissionService(BaseService[UserPermission], model=UserPermission):
    """The service class for all user permission related operations."""

    @classmethod
    @atomic()
    async def update_permissions(cls, user_id: int, obj: Permissions):
        """Update the user's permissions by replacing existing ones.

        Args:
            user_id: The user ID.
            obj: The permissions update data.
        """
        await UserPermission.filter(user_id=user_id).delete()
        perms = [
            UserPermission(user_id=user_id, rel_type=PermType.INDEXER, rel_id=rid)
            for rid in obj.indexer_ids
        ] + [
            UserPermission(user_id=user_id, rel_type=PermType.MEDIA_LIB, rel_id=rid)
            for rid in obj.media_lib_ids
        ]
        if perms:
            await UserPermission.bulk_create(perms)
