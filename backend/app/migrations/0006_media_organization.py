from tortoise import migrations
from tortoise.migrations import operations as ops
from orjson import loads
from tortoise.fields.data import JSON_DUMPS
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0005_auto_20260905_0751')]

    initial = False

    operations = [
        ops.AddField(
            model_name='DownloadTask',
            name='transfer_pending',
            field=fields.BooleanField(default=False, db_default=False),
        ),
        ops.AddField(
            model_name='DownloadTask',
            name='transfer_targets',
            field=fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads),
        ),
        ops.AddField(
            model_name='MediaEvent',
            name='payload',
            field=fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads),
        ),
        ops.AddField(
            model_name='MediaLib',
            name='rename_template',
            field=fields.CharField(null=True, max_length=1024),
        ),
    ]
