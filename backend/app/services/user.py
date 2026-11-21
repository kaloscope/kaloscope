import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import aiofiles
from pydantic import ValidationError
from sanic import Request
from sanic.request.form import File
from tortoise import timezone
from tortoise.expressions import F, Q, Subquery
from tortoise.transactions import atomic, in_transaction

from app.core.config import KaloscopeConfig
from app.core.exceptions import (
    ErrorCode,
    ForbiddenException,
    KaloscopeException,
    NotFoundException,
)
from app.core.flow.nodes.base import start_config
from app.core.media.common import ContentError
from app.core.middleware import SessionHolder
from app.models.flow import FlowGraph, GraphCategory, IndexerResource
from app.models.media import LibType, MediaItem
from app.models.user import (
    DISPLAY_KEYS,
    DisplayPreference,
    FavoriteQuery,
    HistoryEntry,
    HistoryQuery,
    HistoryType,
    IndexerFavoriteChange,
    IndexerFavoritesQuery,
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
        "recent_reads": True,
        "search_records": 3,
        "watch_records": -1,
        "read_records": -1,
    }

    @classmethod
    def preferences_for(cls, stored: dict | None, role: UserRole) -> dict:
        """Resolve saved preferences, including display values from older clients.

        Args:
            stored: Previously saved settings.
            role: Current role controlling access to the downloads homepage.

        Returns:
            Effective settings with defaults for invalid display values.
        """
        result = cls.DEFAULT_PREFERENCES | (stored if isinstance(stored, dict) else {})
        for key in DISPLAY_KEYS:
            try:
                DisplayPreference(key=key, value=result[key])
            except ValidationError:
                result[key] = cls.DEFAULT_PREFERENCES[key]
        if result["homepage"] == "/downloads" and role != UserRole.ADMIN:
            result["homepage"] = cls.DEFAULT_PREFERENCES["homepage"]
        return result

    @classmethod
    async def login(
        cls, username: str, password: str, ambiguity: bool = True
    ) -> UserInfo:
        """Login with the username and password.

        Args:
            username: Entered username.
            password: Entered password.
            ambiguity: Whether to return a more ambiguous error message. Defaults to
                `True`.

        Raises:
            KaloscopeException: If the user is not found or the password is incorrect.

        Returns:
            Login user object.
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
        preferences = cls.preferences_for(user.preferences, user.role)
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
            username: Username to change the password.
            cur_pwd: Current password.
            new_pwd: New password.
        """
        if KaloscopeConfig.get().public_instance_mode:
            raise ForbiddenException
        user = await cls.login(username, cur_pwd, ambiguity=False)
        await User.filter(id=user.id).update(password=encrypt(new_pwd))
        # remove the user's token
        sessions = SessionHolder.get_sessions()
        remove(sessions, vfilter=lambda u: u.id == user.id)

    @classmethod
    async def change_avatar(cls, id: int, avatar: File | None) -> str:
        """Change the user's avatar.

        Args:
            id: User ID.
            avatar: Avatar file.

        Returns:
            Avatar file path.
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
        """Update user's preference.

        Args:
            id: User ID.
            pref: Preference to update.

        Raises:
            KaloscopeException: If the user is not found.
            ForbiddenException: If the homepage requires an administrator.
        """
        # SQLite does not support the `SELECT ... FOR UPDATE` syntax,
        # use the following `UPDATE` statement to acquire a `RESERVED` lock
        await User.filter(id=id).update(updated_at=timezone.now())
        # get the user's preferences from the database
        user = await User.filter(id=id).select_for_update().first()
        if user is None:
            raise KaloscopeException(ErrorCode.USER_NOT_FOUND)
        preferences = cls.preferences_for(user.preferences, user.role)
        if (
            pref.key == "homepage"
            and pref.value == "/downloads"
            and user.role != UserRole.ADMIN
        ):
            raise ForbiddenException
        if pref.key in preferences:
            preferences[pref.key] = pref.value
            await User.filter(id=id).update(preferences=preferences)
            # update the online user's preferences
            sessions = SessionHolder.get_sessions()
            for token, login_user in entries(sessions, vfilter=lambda u: u.id == id):
                login_user.preferences = preferences
                sessions[token] = login_user

    @classmethod
    async def display_preferences(
        cls, user: UserInfo, change: DisplayPreference | None = None
    ) -> dict:
        """Read or update the authenticated account's reversible display preferences.

        Args:
            user: Authenticated account.
            change: One requested update, or absent for a read.

        Returns:
            Effective values from the database, including defaults.

        Raises:
            ForbiddenException: If an ordinary user selects the downloads homepage.
        """
        if change:
            if change.value == "/downloads" and user.role != UserRole.ADMIN:
                raise ForbiddenException
            await cls.update_pref(
                user.id, UserPreference(key=change.key, value=change.value)
            )
        record = await User.get(id=user.id)
        preferences = cls.preferences_for(record.preferences, record.role)
        return {key: preferences[key] for key in DISPLAY_KEYS}

    @classmethod
    def get_pref(cls, preferences: dict | None, key: str) -> Any:
        """Get a preference value with fallback to the default.

        Args:
            preferences: User's preferences dict.
            key: Preference key to look up.

        Returns:
            Preference value, or the default value if not set.
        """
        prefs = preferences if isinstance(preferences, dict) else {}
        return prefs.get(key, cls.DEFAULT_PREFERENCES.get(key))

    @classmethod
    async def create(cls, username: str, password: str, role: UserRole = UserRole.USER):
        """Create a new user.

        Args:
            username: Username to create.
            password: Password to create.
            role: Role of the user. Defaults to UserRole.USER.
        """
        if await User.filter(username=username).count() > 0:
            raise KaloscopeException(ErrorCode.USERNAME_ALREADY_EXISTS)
        await User.create(username=username, password=encrypt(password), role=role)


class UserFavoriteService(BaseService[UserFavorite], model=UserFavorite):
    """The service class for all user favorite related operations."""

    @classmethod
    async def list_page(cls, query: FavoriteQuery, user: UserInfo) -> dict:
        """List the owner's favorites inside the current indexer grants.

        Args:
            query: Indexer and resource filters and pagination.
            user: Current account with loaded indexer permissions.

        Returns:
            Matching favorites and their total count.
        """
        criteria = [Q(user_id=user.id)]
        if user.role != UserRole.ADMIN:
            criteria.append(
                Q(indexer_id__in=user.perms.indexer_ids if user.perms else [])
            )
        if query.indexer_id:
            criteria.append(Q(indexer_id=query.indexer_id))
        if query.rsrc_ids:
            criteria.append(Q(rsrc_id__in=query.rsrc_ids))
        return await cls.dump_page(
            await UserFavorite.page(*criteria, **query.page_params)
        )

    @classmethod
    async def summaries(cls, query: IndexerFavoritesQuery, user: UserInfo) -> dict:
        """Read personal favorites without returning external URLs or private metadata.

        Args:
            query: Optional indexer and requested page.
            user: Current authenticated account.

        Returns:
            Resource IDs and titles in the requested page.
        """
        result = await cls.list_page(
            FavoriteQuery(
                indexer_id=query.workflow_id, page_num=query.page, page_size=10
            ),
            user,
        )
        return {
            "total": result["total"],
            "next_page": query.page + 1 if query.page * 10 < result["total"] else None,
            "items": [
                {
                    "workflow_id": item["indexer_id"],
                    "resource_id": item["rsrc_id"],
                    "title": str(item["rsrc"].get("title") or "")[:256],
                }
                for item in result["items"]
            ],
        }

    @classmethod
    async def set_resource(
        cls, indexer_id: int, resource: IndexerResource, user: UserInfo
    ) -> None:
        """Store or remove a personal favorite within the caller's indexer grants.

        Args:
            indexer_id: Source indexer.
            resource: Identity and optional metadata. Omitting metadata removes it.
            user: Current owner.

        Raises:
            ForbiddenException: If the account cannot access the indexer.
            NotFoundException: If the indexer does not exist.
        """
        await FlowGraphService.require_indexer(indexer_id, user)
        identity = {
            "user_id": user.id,
            "indexer_id": indexer_id,
            "rsrc_id": str(resource.rsrc_id),
        }
        if resource.rsrc is None:
            await UserFavorite.filter(**identity).delete()
            return
        data = {key: value for key, value in resource.rsrc.items() if key != "favorite"}
        await UserFavorite.update_or_create(
            defaults={"rsrc": data, "url": resource.url}, **identity
        )

    @classmethod
    async def set_selected(cls, change: IndexerFavoriteChange, user: UserInfo) -> dict:
        """Apply an explicit personal favorite choice for one search result.

        Args:
            change: Selected resource identity, title and desired state.
            user: Current account.

        Returns:
            The stored resource identity and favorite state.

        Raises:
            ForbiddenException: If the account cannot access the indexer.
            NotFoundException: If the indexer does not exist.
        """
        graph = await FlowGraphService.require_indexer(change.workflow_id, user)
        identity = {
            "user_id": user.id,
            "indexer_id": graph.id,
            "rsrc_id": change.resource_id,
        }
        if change.favorite:
            details = (await start_config(graph, "details"))["details"]
            url = None
            if isinstance(details, dict):
                params = {k: v for k, v in (details.get("specific") or {}).items() if v}
                url = f"/websearch/{graph.id}/{quote(change.resource_id, safe='')}"
                if params:
                    url += "?" + urlencode(params)
            # replaying the action preserves richer metadata saved from the search page
            await UserFavorite.get_or_create(
                **identity,
                defaults={
                    "rsrc": {
                        "id": change.resource_id,
                        "title": change.title or change.resource_id,
                    },
                    "url": url if url and len(url) <= 255 else None,
                },
            )
        else:
            await UserFavorite.filter(**identity).delete()
        return {
            "workflow_id": graph.id,
            "resource_id": change.resource_id,
            "favorite": change.favorite,
        }


class UserHistoryService(BaseService[UserHistory], model=UserHistory):
    """The service class for all user history related operations."""

    @classmethod
    async def retention_days(cls, user_id: int, rel_type: HistoryType) -> int:
        """Get maximum retention days for a given history type.

        Args:
            user_id: User ID.
            rel_type: History type.

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
            user: Authenticated user with loaded library and indexer grants.
            obj: History entry data.

        Returns:
            History instance, or `None` if recording is disabled or the search
            is empty.

        Raises:
            ContentError: If a reading locator or its content version is invalid.
            NotFoundException: If the indexer, reading work or chapter is missing.
            ForbiddenException: If the user cannot access the indexer or library.
        """
        if obj.locator is not None:
            await MediaItemService.get_locator(obj.rel_id, user, obj.locator)

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
                    if obj.rel_id:
                        await FlowGraphService.require_indexer(obj.rel_id, user)
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
        """List retained history for the account's accessible indexers and media.

        Args:
            user: Authenticated user with loaded library permissions.
            query: History type, optional related or parent ID and pagination.

        Returns:
            A history page with related media or graphs. Stale reading positions
            recover at their chapter start or return null if unavailable. Neither
            case overwrites stored progress, and both clear the response percentage.
        """
        await cls.clean_expired(user.id, query.rel_type)
        queries = [Q(user_id=user.id, rel_type=query.rel_type)]
        if query.rel_id is not None:
            queries.append(Q(rel_id=query.rel_id))
        reading = query.rel_type in (HistoryType.TEXT, HistoryType.IMAGE)
        media_history = query.rel_type != HistoryType.SEARCH
        if media_history:
            items = MediaItem.filter(visible=True)
            if reading:
                items = items.filter(
                    parent_id=None,
                    lib__lib_type=LibType.NOVEL
                    if query.rel_type == HistoryType.TEXT
                    else LibType.COMIC,
                )
            else:
                items = items.filter(
                    Q(parent_id=None)
                    | Q(parent__visible=True, parent__lib_id=F("lib__id")),
                    lib__lib_type__in=[LibType.MOVIE, LibType.TV_SHOW],
                )
                if query.parent_id is not None:
                    items = items.filter(parent_id=query.parent_id)
            if user.role != UserRole.ADMIN:
                items = items.filter(
                    lib_id__in=Subquery(
                        UserPermission.filter(
                            user_id=user.id, rel_type=PermType.MEDIA_LIB
                        ).values("rel_id")
                    )
                )
            # filter before pagination to keep totals and page boundaries accurate
            queries.append(Q(rel_id__in=Subquery(items.values("id"))))
        elif user.role != UserRole.ADMIN:
            grants = UserPermission.filter(user_id=user.id, rel_type=PermType.INDEXER)
            indexers = FlowGraph.filter(
                category=GraphCategory.INDEXER, id__in=Subquery(grants.values("rel_id"))
            )
            queries.append(Q(rel_id=0) | Q(rel_id__in=Subquery(indexers.values("id"))))
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
                    locator = await MediaItemService.get_locator(
                        rel_id, user, entry.locator, restore=True
                    )
                    history["locator"] = (
                        locator.model_dump(exclude_none=True) if locator else None
                    )
                    if locator != entry.locator:
                        history["percentage"] = None
                except (
                    ValidationError,
                    ContentError,
                    NotFoundException,
                    ForbiddenException,
                ):
                    history["locator"] = None
                    history["percentage"] = None
        if result["items"] and (media_history or user.role != UserRole.ADMIN):
            # recheck visibility and grants after the unlocked metadata reads
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
            user_id: User ID.
            rel_type: History type to clean.
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
        """Update user's permissions by replacing existing ones.

        Args:
            user_id: User ID.
            obj: Permissions update data.
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
