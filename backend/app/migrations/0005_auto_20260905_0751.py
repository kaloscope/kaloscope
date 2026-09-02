from tortoise import migrations
from tortoise.migrations import operations as ops
from app.core.dl.driver import DownloadState
from app.models.download import OfflineDownloadErrorKind
from orjson import loads
from tortoise.fields.base import OnDelete
from tortoise.fields.data import JSON_DUMPS
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0004_auto_20260728_1628')]

    initial = False

    operations = [
        ops.CreateModel(
            name='OfflineDownloadJob',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('download', fields.OneToOneField('models.DownloadTask', source_field='download_id', db_constraint=True, to_field='id', related_name='offline_job', on_delete=OnDelete.CASCADE)),
                ('job_uuid', fields.CharField(unique=True, max_length=32)),
                ('source_fingerprint', fields.CharField(max_length=64)),
                ('remote_dir', fields.CharField(unique=True, max_length=4096)),
                ('manifest', fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads)),
                ('manifest_fingerprint', fields.CharField(null=True, max_length=64)),
                ('manifest_changed_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
                ('next_poll_at', fields.DatetimeField(null=True, db_index=True, auto_now=False, auto_now_add=False)),
                ('completion_due_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
                ('delete_due_at', fields.DatetimeField(null=True, db_index=True, auto_now=False, auto_now_add=False)),
                ('delete_local', fields.BooleanField(default=False, db_default=False)),
                ('unchanged_count', fields.IntField(default=0)),
                ('retry_count', fields.IntField(default=0)),
                ('last_error_kind', fields.CharEnumField(null=True, description='SUBMIT_UNKNOWN: submit_unknown\nINSTANCE_AUTH: instance_auth\nINSTANCE_RATE_LIMIT: instance_rate_limit\nINSTANCE_TRANSIENT: instance_transient\nREMOTE_TASK_MISSING: remote_task_missing\nREMOTE_FAILED: remote_failed\nTRANSFER_FAILED: transfer_failed\nMANIFEST_INVALID: manifest_invalid\nDIRECT_LINK_UNAVAILABLE: direct_link_unavailable\nLOCAL_PATH_INVALID: local_path_invalid\nLOCAL_FILE_CONFLICT: local_file_conflict\nPULL_FAILED: pull_failed\nVERIFY_FAILED: verify_failed\nCLEANUP_FAILED: cleanup_failed', enum_type=OfflineDownloadErrorKind, max_length=32)),
            ],
            options={'table': 'offline_download_job', 'app': 'models', 'pk_attr': 'id'},
            bases=['TortoiseModel'],
        ),
        ops.AlterField(
            model_name='DownloadTask',
            name='state',
            field=fields.CharEnumField(description='DOWNLOADING: downloading\nSUBMITTING: submitting\nSUBMIT_UNKNOWN: submit_unknown\nREMOTE: remote\nSETTLING: settling\nPULLING: pulling\nVERIFYING: verifying\nPAUSED: paused\nCOMPLETED: completed\nERROR: error', enum_type=DownloadState, max_length=16),
        ),
    ]
