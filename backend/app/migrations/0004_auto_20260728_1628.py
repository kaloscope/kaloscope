from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise import fields
from tortoise.indexes import Index

class Migration(migrations.Migration):
    dependencies = [('models', '0003_auto_20260531_1044')]

    initial = False

    operations = [
        ops.AddIndex(
            model_name='DownloadPlanHistory',
            index=Index(fields=['plan_id', 'info_hash']),
        ),
        ops.AddIndex(
            model_name='DownloadPlanHistory',
            index=Index(fields=['plan_id', 'info_hash_v2']),
        ),
        ops.AddIndex(
            model_name='DownloadTask',
            index=Index(fields=['state', 'created_at']),
        ),
        ops.AlterField(
            model_name='Downloader',
            name='preset',
            field=fields.CharField(null=True, max_length=16),
        ),
        ops.AddIndex(
            model_name='MediaItem',
            index=Index(fields=['path']),
        ),
        ops.AddIndex(
            model_name='MediaItem',
            index=Index(fields=['hash']),
        ),
        ops.AddIndex(
            model_name='MediaItem',
            index=Index(fields=['lib_id', 'parent_id', 'visible', 'created_at']),
        ),
        ops.AddIndex(
            model_name='Notification',
            index=Index(fields=['user_id', 'created_at']),
        ),
        ops.AddIndex(
            model_name='Notification',
            index=Index(fields=['role', 'created_at']),
        ),
    ]
