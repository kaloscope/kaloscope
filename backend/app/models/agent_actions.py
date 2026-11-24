"""Define administrator review receipts and compact domain action inputs."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator
from tortoise.fields import CharField, DatetimeField, IntField, JSONField, UUIDField

from app.models.base import TortoiseModel
from app.models.media import LibType


class AgentAdminReview(TortoiseModel):
    """Record the exact action, revision, approval and result across disconnects."""

    operation_id = UUIDField(default=uuid4, unique=True)
    owner_id = IntField(db_index=True)
    digest = CharField(max_length=64)
    request = JSONField()
    preview = JSONField()
    status = CharField(max_length=16, default="pending")
    result = JSONField(null=True)
    expires_at = DatetimeField()

    class Meta:
        table = "agent_admin_review"


class AdminChange(BaseModel):
    """Propose one bounded change for explicit browser approval."""

    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "publish_workflow",
        "create_library",
        "delete_library",
        "edit_metadata",
    ]
    target_id: PositiveInt | None = None
    name: str | None = Field(default=None, min_length=1, max_length=64)
    directory: str | None = Field(default=None, min_length=1, max_length=4096)
    library_type: LibType | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("name", "directory", "title")
    @classmethod
    def nonblank(cls, value: str | None) -> str | None:
        """Reject all-whitespace inputs while preserving exact titles and paths.

        Args:
            value: Optional user-supplied setting.

        Returns:
            The unchanged setting.

        Raises:
            ValueError: If a supplied setting contains no visible text.
        """
        if value is not None and not value.strip():
            raise ValueError("Supply a nonblank value.")
        return value


class WorkspaceQuery(BaseModel):
    """Read bounded administrative diagnostics or available workflow templates."""

    model_config = ConfigDict(extra="forbid")
    topic: Literal[
        "downloaders",
        "transcoding",
        "logs",
        "workflow_templates",
        "actions",
        "receipt",
        "batches",
    ]
    operation_id: UUID | None = None
    page: int = Field(default=1, ge=1, le=10000)


class BatchProgress(BaseModel):
    """Inspect the progress and remaining work of a reviewed batch."""

    model_config = ConfigDict(extra="forbid")
    batch_id: PositiveInt


class BatchStop(BatchProgress):
    """Stop the next unit of an identified running media batch."""
