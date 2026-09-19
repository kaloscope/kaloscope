from tortoise import fields, migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    dependencies = [("models", "0005_auto_20260905_0751")]

    initial = False

    operations = [
        ops.AddField(
            model_name="MediaLib",
            name="rename_template",
            field=fields.CharField(max_length=1024, null=True),
        ),
        ops.AddField(
            model_name="MediaEvent",
            name="payload",
            field=fields.JSONField(null=True),
        ),
        ops.AddField(
            model_name="DownloadTask",
            name="transfer_targets",
            field=fields.JSONField(null=True),
        ),
    ]
