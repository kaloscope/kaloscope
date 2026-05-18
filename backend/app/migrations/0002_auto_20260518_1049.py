from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0001_initial')]

    initial = False

    operations = [
        ops.CreateModel(
            name='DownloadPlanHistory',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('plan', fields.ForeignKeyField('models.DownloadPlan', source_field='plan_id', db_index=True, db_constraint=True, to_field='id', related_name='histories', on_delete=OnDelete.CASCADE)),
                ('info_hash', fields.CharField(null=True, max_length=40)),
                ('info_hash_v2', fields.CharField(null=True, max_length=68)),
            ],
            options={'table': 'download_plan_history', 'app': 'models', 'pk_attr': 'id'},
            bases=['TortoiseModel'],
        ),
    ]
