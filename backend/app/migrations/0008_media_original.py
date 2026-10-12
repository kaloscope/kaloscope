from tortoise import migrations
from tortoise.migrations import operations as ops
from orjson import loads
from tortoise.fields.data import JSON_DUMPS
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0007_auto_20261002_2108')]

    initial = False

    operations = [
        ops.AddField(
            model_name='MediaItem',
            name='original',
            field=fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads),
        ),
    ]
