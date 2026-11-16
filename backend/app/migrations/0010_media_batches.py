from tortoise import migrations
from tortoise.migrations import operations as ops
from app.models.media import MediaAction
from orjson import loads
from tortoise.fields.base import OnDelete
from tortoise.fields.data import JSON_DUMPS
from tortoise import fields
from tortoise.indexes import Index

class Migration(migrations.Migration):
    dependencies = [('models', '0009_ai_features')]

    initial = False

    operations = [
        ops.CreateModel(
            name='MediaBatchEntry',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('receipt', fields.ForeignKeyField('models.MediaChangeReceipt', source_field='receipt_id', db_index=True, db_constraint=True, to_field='id', related_name='entries', on_delete=OnDelete.CASCADE)),
                ('seq', fields.IntField()),
                ('kind', fields.CharField(max_length=16)),
                ('owner', fields.CharField(max_length=16)),
                ('item_id', fields.IntField(null=True)),
                ('src', fields.CharField(max_length=4096)),
                ('dst', fields.CharField(null=True, max_length=4096)),
                ('files', fields.IntField(default=1)),
                ('size', fields.BigIntField(null=True)),
                ('identity', fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads)),
                ('status', fields.CharField(max_length=16)),
                ('reason', fields.CharField(null=True, max_length=64)),
            ],
            options={'table': 'media_batch_entry', 'app': 'models', 'unique_together': (('receipt', 'seq'),), 'indexes': [Index(fields=['receipt_id', 'status'])], 'pk_attr': 'id', 'table_description': 'Keep one unit of a reviewed batch and what happened to it.'},
            bases=['TortoiseModel'],
        ),
        ops.AlterField(
            model_name='MediaChangeReceipt',
            name='action',
            field=fields.CharEnumField(description='SCAN: scan\nRESCRAPE: rescrape\nCONFIGURE: configure\nBATCH: batch', enum_type=MediaAction, max_length=16),
        ),
        ops.AddField(
            model_name='MediaChangeReceipt',
            name='plan',
            field=fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads),
        ),
    ]
