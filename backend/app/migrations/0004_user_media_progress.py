from tortoise import fields, migrations
from tortoise.fields.base import OnDelete
from tortoise.migrations import operations as ops

from app.models.user import MediaProgressStatus


class Migration(migrations.Migration):
    dependencies = [("models", "0003_auto_20260531_1044")]

    initial = False

    operations = [
        ops.CreateModel(
            name="UserMediaProgress",
            fields=[
                (
                    "id",
                    fields.IntField(
                        generated=True, primary_key=True, unique=True, db_index=True
                    ),
                ),
                (
                    "created_at",
                    fields.DatetimeField(
                        null=True, auto_now=False, auto_now_add=True
                    ),
                ),
                (
                    "updated_at",
                    fields.DatetimeField(
                        null=True, auto_now=True, auto_now_add=False
                    ),
                ),
                (
                    "user",
                    fields.ForeignKeyField(
                        "models.User",
                        source_field="user_id",
                        db_index=True,
                        db_constraint=True,
                        to_field="id",
                        related_name="media_progresses",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                (
                    "media",
                    fields.ForeignKeyField(
                        "models.MediaItem",
                        source_field="media_id",
                        db_index=True,
                        db_constraint=True,
                        to_field="id",
                        related_name="user_progresses",
                        on_delete=OnDelete.CASCADE,
                    ),
                ),
                ("position", fields.IntField(default=0)),
                ("percentage", fields.IntField(default=0)),
                (
                    "status",
                    fields.CharEnumField(
                        description="WATCHING: watching\nWATCHED: watched",
                        enum_type=MediaProgressStatus,
                        max_length=16,
                    ),
                ),
                ("manual", fields.BooleanField(default=False)),
            ],
            options={
                "table": "user_media_progress",
                "unique_together": [("user", "media")],
                "ordering": ["-updated_at"],
                "app": "models",
                "pk_attr": "id",
            },
            bases=["TortoiseModel"],
        ),
    ]
