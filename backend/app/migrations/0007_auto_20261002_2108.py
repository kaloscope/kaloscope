from tortoise import migrations
from tortoise.migrations import operations as ops
from app.models.media import IndexState, LibType, MediaFormat
from app.models.user import HistoryType
from orjson import loads
from tortoise.fields.data import JSON_DUMPS
from tortoise import fields
from tortoise.indexes import Index

class Migration(migrations.Migration):
    dependencies = [('models', '0006_auto_20260929_1745')]

    initial = False

    operations = [
        ops.AddIndex(
            model_name='MediaEvent',
            index=Index(fields=['lib_id', 'event_type']),
        ),
        ops.AddField(
            model_name='MediaItem',
            name='extra',
            field=fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads),
        ),
        ops.AddField(
            model_name='MediaItem',
            name='format',
            field=fields.CharEnumField(null=True, description='TXT: txt\nEPUB: epub\nDIR: dir\nCBZ: cbz\nZIP: zip', enum_type=MediaFormat, max_length=16),
        ),
        ops.AddField(
            model_name='MediaItem',
            name='index_error',
            field=fields.CharField(null=True, max_length=64),
        ),
        ops.AddField(
            model_name='MediaItem',
            name='index_state',
            field=fields.CharEnumField(null=True, description='PENDING: pending\nREADY: ready\nEMPTY: empty\nERROR: error', enum_type=IndexState, max_length=16),
        ),
        ops.AddField(
            model_name='MediaItem',
            name='index_version',
            field=fields.CharField(null=True, max_length=64),
        ),
        ops.AlterField(
            model_name='MediaLib',
            name='lib_type',
            field=fields.CharEnumField(description='MOVIE: movie\nTV_SHOW: tv_show\nNOVEL: novel\nCOMIC: comic', enum_type=LibType, max_length=16),
        ),
        ops.AlterField(
            model_name='UserHistory',
            name='rel_type',
            field=fields.CharEnumField(description='SEARCH: search\nVIDEO: video\nTEXT: text\nIMAGE: image', enum_type=HistoryType, max_length=16),
        ),
        ops.AddField(
            model_name='UserHistory',
            name='locator',
            field=fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads),
        ),
    ]
