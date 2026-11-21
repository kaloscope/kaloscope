"""Define download action grants shared by web routes, the assistant and MCP."""

from dataclasses import dataclass

from app.core.exceptions import ErrorCode, ForbiddenException
from app.models.user import UserInfo, UserRole


@dataclass(frozen=True)
class DownloadGrant:
    """Describe the required account role and external execution grant."""

    role: UserRole | None = None
    read_write: bool = False

    def allows(self, user: UserInfo) -> bool:
        """Check the caller's role before applying resource-specific permissions.

        Args:
            user: Authenticated account.

        Returns:
            Whether the account can request the action.
        """
        return self.role is None or self.role == user.role

    def require(self, user: UserInfo) -> None:
        """Require the account role for a download action.

        Args:
            user: Authenticated account.

        Raises:
            ForbiddenException: If the account lacks the required role.
        """
        if not self.allows(user):
            raise ForbiddenException(ErrorCode.PERMISSION_DENIED)


READ_PLAN = DownloadGrant()
SEARCH_INDEXER = DownloadGrant(read_write=True)
ADMIN_PREVIEW = DownloadGrant(UserRole.ADMIN)
ADMIN_CHANGE = DownloadGrant(UserRole.ADMIN, read_write=True)

DOWNLOAD_GRANTS = {
    "diagnose_download_ingestion": ADMIN_PREVIEW,
    "get_download_plan": READ_PLAN,
    "list_download_plans": READ_PLAN,
    "list_download_indexers": READ_PLAN,
    "search_indexer": SEARCH_INDEXER,
    "get_download_plan_options": ADMIN_PREVIEW,
    "validate_download_plan": ADMIN_PREVIEW,
    "get_library_ingestion": ADMIN_PREVIEW,
    "validate_download_task": ADMIN_PREVIEW,
    "list_download_tasks": ADMIN_PREVIEW,
    "get_download_task": ADMIN_PREVIEW,
    "review_download_task_action": ADMIN_PREVIEW,
    "review_download_plan_change": ADMIN_PREVIEW,
    "get_plan_replacement": ADMIN_PREVIEW,
    "review_plan_replacement": ADMIN_PREVIEW,
    "create_download_plan": ADMIN_CHANGE,
    "create_download_task": ADMIN_CHANGE,
    "apply_download_plan_change": ADMIN_CHANGE,
    "apply_download_task_action": ADMIN_CHANGE,
}
