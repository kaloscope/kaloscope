import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiofiles
from pydantic import ValidationError
from sanic import Request
from sanic.request.form import File
from tortoise import timezone
from tortoise.expressions import Q, Subquery
from tortoise.transactions import atomic, in_transaction

from app.core.config import KaloscopeConfig
from app.core.exceptions import (
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.media.common import ContentError
from app.core.middleware import SessionHolder
from app.models.flow import FlowGraph, IndexerResource
from app.models.media import LibType, MediaItem
from app.models.user import (
    HistoryEntry,
    HistoryQuery,
    HistoryType,
    Permissions,
    PermType,
    User,
    UserFavorite,
    UserHistory,
    UserInfo,
    UserPermission,
    UserPreference,
    UserRole,
)
from app.services.base import BaseService
from app.services.flow import FlowGraphService
from app.services.media import MediaItemService
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
        "watch_records": -1,
        "read_records": -1,
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
        preferences = cls.DEFAULT_PREFERENCES.copy()
        if isinstance(user.preferences, dict):
            preferences.update(user.preferences)
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
    async def update_pref(cls, id: int, pref: UserPreference):
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
            Positive retention days, -1 for permanent storage, or 0 to disable it.
        """
        days = 0
        user = await User.get_or_none(id=user_id)
        if user is None:
            return days

        if rel_type == HistoryType.SEARCH:
            days = UserService.get_pref(user.preferences, "search_records")
        elif rel_type == HistoryType.VIDEO:
            days = UserService.get_pref(user.preferences, "watch_records")
        elif rel_type in (HistoryType.TEXT, HistoryType.IMAGE):
            days = UserService.get_pref(user.preferences, "read_records")
        return days if type(days) is int and days >= -1 else 0

    @classmethod
    async def record(cls, user: UserInfo, obj: HistoryEntry) -> UserHistory | None:
        """Record a user history entry, incrementing repetitions on duplicate.

        Args:
            user: The authenticated user with loaded library permissions.
            obj: The history entry data.

        Returns:
            The history instance, or None if recording is disabled or the search
            is empty.

        Raises:
            ContentError: If a reading locator or its content version is invalid.
            NotFoundException: If a reading work or chapter is missing or hidden.
            ForbiddenException: If the user cannot access the reading library.
        """
        if obj.locator is not None:
            await MediaItemService.validate_locator(obj.rel_id, user, obj.locator)

        # serialize the SQLite lookup and insert so concurrent first saves share one row
        async with in_transaction():
            if await cls.retention_days(user.id, obj.rel_type) == 0:
                return None

            history = None
            created = False
            if obj.rel_type != HistoryType.SEARCH:
                defaults: dict[str, Any] = {
                    "position": obj.position or 0,
                    "percentage": obj.percentage or 0,
                }
                if obj.locator is not None:
                    defaults.update(
                        position=None,
                        locator=obj.locator.model_dump(exclude_none=True),
                    )
                history, created = await UserHistory.update_or_create(
                    user_id=user.id,
                    rel_type=obj.rel_type,
                    rel_id=obj.rel_id,
                    defaults=defaults,
                )
            else:
                # record web search history
                keyword = (obj.keyword or "").strip()
                if keyword:
                    history, created = await UserHistory.get_or_create(
                        user_id=user.id,
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
    async def get_page(cls, user: UserInfo, query: HistoryQuery) -> dict[str, Any]:
        """List retained history with accessible reading works and usable locators.

        Args:
            user: The authenticated user with loaded library permissions.
            query: The history type, optional related ID and pagination parameters.

        Returns:
            A history page with related media or graphs. Unusable reading positions
            are returned as null without deleting the user's stored history.
        """
        await cls.clean_expired(user.id, query.rel_type)
        queries = [Q(user_id=user.id, rel_type=query.rel_type)]
        if query.rel_id is not None:
            queries.append(Q(rel_id=query.rel_id))
        reading = query.rel_type in (HistoryType.TEXT, HistoryType.IMAGE)
        if reading:
            works = MediaItem.filter(
                parent_id=None,
                visible=True,
                lib__lib_type=LibType.NOVEL
                if query.rel_type == HistoryType.TEXT
                else LibType.COMIC,
            )
            if user.role != UserRole.ADMIN:
                works = works.filter(
                    lib_id__in=Subquery(
                        UserPermission.filter(
                            user_id=user.id, rel_type=PermType.MEDIA_LIB
                        ).values("rel_id")
                    )
                )
            # filter before pagination to keep totals and page boundaries accurate
            queries.append(Q(rel_id__in=Subquery(works.values("id"))))
        page = await UserHistory.page(*queries, **query.page_params)
        result = await cls.dump_page(page)
        for history in result["items"]:
            rel_id = history["rel_id"]
            if not rel_id:
                continue
            if query.rel_type == HistoryType.SEARCH:
                graph = await FlowGraph.get_or_none(id=rel_id)
                history["graph"] = (
                    await FlowGraphService.dump(
                        graph, exclude={"draft", "definition", "logs"}
                    )
                    if graph is not None
                    else None
                )
                continue
            media = await MediaItem.get_or_none(id=rel_id)
            history["media"] = (
                await MediaItemService.dump(media, exclude={"children"})
                if media is not None
                else None
            )
            if reading:
                try:
                    entry = HistoryEntry.model_validate(history)
                    assert entry.locator is not None
                    await MediaItemService.validate_locator(rel_id, user, entry.locator)
                    history["locator"] = entry.locator.model_dump(exclude_none=True)
                except (
                    ValidationError,
                    ContentError,
                    NotFoundException,
                    ForbiddenException,
                ):
                    history["locator"] = None
                    history["percentage"] = None
        if reading and result["items"]:
            # recheck visibility and grants after the unlocked content reads
            visible = set(
                await UserHistory.filter(
                    *queries, id__in=[history["id"] for history in result["items"]]
                ).values_list("id", flat=True)
            )
            if len(visible) != len(result["items"]):
                result["items"] = [
                    history for history in result["items"] if history["id"] in visible
                ]
                result["total"] = await UserHistory.filter(*queries).count()
        return result

    @classmethod
    async def clean_expired(cls, user_id: int, rel_type: HistoryType):
        """Delete expired history records based on user preferences.

        A value of -1 keeps records permanently. A value of 0 deletes all
        records of the given type. Positive values expire records by age.

        Args:
            user_id: The user ID.
            rel_type: The history type to clean.
        """
        days = await cls.retention_days(user_id, rel_type)
        if days == -1:
            return
        if days == 0:
            await UserHistory.filter(user_id=user_id, rel_type=rel_type).delete()
        else:
            try:
                cutoff = timezone.now() - timedelta(days=days)
            except OverflowError:
                # a retention window beyond the calendar cannot expire records
                return
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
