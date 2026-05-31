from tortoise import migrations
from tortoise.migrations import operations as ops
from orjson import loads
from tortoise.fields.data import JSON_DUMPS
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0002_auto_20260518_1049')]

    initial = False

    operations = [
        ops.CreateModel(
            name='GlobalConfig',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('key', fields.CharField(unique=True, max_length=64)),
                ('value', fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads)),
            ],
            options={'table': 'global_config', 'app': 'models', 'pk_attr': 'id'},
            bases=['TortoiseModel'],
        ),
    ]
