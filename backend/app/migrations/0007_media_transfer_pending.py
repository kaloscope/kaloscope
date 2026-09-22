from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0006_media_organization')]

    initial = False

    operations = [
        ops.AddField(
            model_name='DownloadTask',
            name='transfer_pending',
            field=fields.BooleanField(default=False, db_default=False),
        ),
    ]
