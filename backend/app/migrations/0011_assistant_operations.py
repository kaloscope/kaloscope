from uuid import uuid4
from app.models.mcp import MCPTokenDomain
from tortoise import migrations
from tortoise.migrations import operations as ops
from orjson import loads
from tortoise.fields.data import JSON_DUMPS
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0010_media_batches')]

    initial = False

    operations = [
        ops.CreateModel(
            name='DownloadTaskCreation',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('key', fields.CharField(unique=True, max_length=64)),
                ('digest', fields.CharField(max_length=64)),
                ('task_id', fields.IntField()),
            ],
            options={'table': 'download_task_creation', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Retain the reviewed submission identity when its download task is removed.'},
            bases=['TortoiseModel'],
        ),
        ops.RunSQL(
            "INSERT INTO download_task_creation (key, digest, task_id, created_at, updated_at) "
            "SELECT creation_key, json_extract(creation_receipt, '$.digest'), id, created_at, updated_at "
            "FROM download_task WHERE creation_key IS NOT NULL AND json_type(creation_receipt, '$.digest') = 'text'",
            reverse_sql=ops.RunSQL.noop,
        ),
        ops.CreateModel(
            name='AssistantReviewDecision',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('owner_id', fields.IntField()),
                ('request_id', fields.UUIDField()),
                ('approved', fields.BooleanField()),
                ('result', fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads)),
            ],
            options={'table': 'assistant_review_decision', 'app': 'models', 'unique_together': (('owner_id', 'request_id'),), 'pk_attr': 'id', 'table_description': "Keep the first decision for an account's reviewed operation."},
            bases=['TortoiseModel'],
        ),
        ops.CreateModel(
            name='AgentAdminReview',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('operation_id', fields.UUIDField(default=uuid4, unique=True)),
                ('owner_id', fields.IntField(db_index=True)),
                ('digest', fields.CharField(max_length=64)),
                ('request', fields.JSONField(encoder=JSON_DUMPS, decoder=loads)),
                ('preview', fields.JSONField(encoder=JSON_DUMPS, decoder=loads)),
                ('status', fields.CharField(default='pending', max_length=16)),
                ('result', fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads)),
                ('expires_at', fields.DatetimeField(auto_now=False, auto_now_add=False)),
            ],
            options={'table': 'agent_admin_review', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Record the exact action, revision, approval and result across disconnects.'},
            bases=['TortoiseModel'],
        ),
        ops.AddField(
            model_name='MCPAccessToken',
            name='domain',
            field=fields.CharEnumField(default=MCPTokenDomain.ALL, db_default=MCPTokenDomain.ALL, description='ALL: all\nMEDIA: media\nDOWNLOADS: downloads\nWORKFLOWS: workflows\nSYSTEM: system', enum_type=MCPTokenDomain, max_length=16),
        ),
        ops.AddField(
            model_name='MCPAccessToken',
            name='library_ids',
            field=fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads),
        ),
        ops.CreateModel(
            name='DownloadPlanAttempt',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('plan_id', fields.IntField(db_index=True)),
                ('workflow_id', fields.IntField()),
                ('status', fields.CharField(default='started', max_length=16)),
                ('results_seen', fields.IntField(default=0)),
                ('submitted', fields.IntField(default=0)),
                ('errors', fields.IntField(default=0)),
                ('uncertain', fields.IntField(default=0)),
            ],
            options={'table': 'download_plan_attempt', 'app': 'models', 'pk_attr': 'id',
                     'table_description': 'Keep the search and submission evidence of one subscription check.'},
            bases=['TortoiseModel'],
        ),
        ops.AddField(model_name='DownloadTask', name='last_transfer_failed', field=fields.BooleanField(null=True)),
        ops.AddField(model_name='DownloadTask', name='plan_attempt_id', field=fields.IntField(null=True, db_index=True)),
        ops.RunSQL(
            'CREATE INDEX IF NOT EXISTS idx_download_ta_plan_at_a59207 ON download_task(plan_attempt_id)',
            reverse_sql='DROP INDEX IF EXISTS idx_download_ta_plan_at_a59207',
        ),
        ops.CreateModel(
            name='DownloadPlanCreation',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('key', fields.CharField(unique=True, max_length=64)),
                ('digest', fields.CharField(max_length=64)),
                ('plan_id', fields.IntField()),
            ],
            options={'table': 'download_plan_creation', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Keep the account-scoped creation key after a subscription is removed.'},
            bases=['TortoiseModel'],
        ),
        ops.CreateModel(
            name='DownloadPlanDispatch',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('key', fields.CharField(unique=True, max_length=64)),
                ('plan_id', fields.IntField(db_index=True)),
                ('info_hash', fields.CharField(null=True, max_length=40)),
                ('info_hash_v2', fields.CharField(null=True, max_length=68)),
                ('counted', fields.BooleanField(default=False)),
                ('task_id', fields.IntField(null=True)),
            ],
            options={'table': 'download_plan_dispatch', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Track one in-flight plan submission until its effects can be verified.'},
            bases=['TortoiseModel'],
        ),
        ops.CreateModel(
            name='DownloadPlanReplacement',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('operation_id', fields.CharField(unique=True, max_length=36)),
                ('owner_id', fields.IntField(db_index=True)),
                ('plan_id', fields.IntField(db_index=True)),
                ('digest', fields.CharField(max_length=64)),
                ('preview', fields.JSONField(encoder=JSON_DUMPS, decoder=loads)),
                ('status', fields.CharField(default='pending', db_index=True, max_length=16)),
                ('expires_at', fields.DatetimeField(auto_now=False, auto_now_add=False)),
                ('decided_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
                ('new_plan_id', fields.IntField(null=True)),
                ('first_execution', fields.JSONField(null=True, encoder=JSON_DUMPS, decoder=loads)),
            ],
            options={'table': 'download_plan_replacement', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Bind a replacement decision to the exact stored review and account.'},
            bases=['TortoiseModel'],
        ),
        ops.AddField(
            model_name='DownloadPlan',
            name='replacement_due',
            field=fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
        ),
        ops.RunSQL(
            "INSERT INTO download_plan_creation (key, digest, plan_id, created_at, updated_at) "
            "SELECT creation_key, COALESCE(json_extract(creation_receipt, '$.digest'), ''), "
            "id, created_at, updated_at FROM download_plan WHERE creation_key IS NOT NULL",
            reverse_sql=ops.RunSQL.noop,
        ),
        # earlier versions could store duplicate rows for the same personal favorite
        ops.RunSQL(
            "DELETE FROM user_favorite WHERE id NOT IN "
            "(SELECT MAX(id) FROM user_favorite GROUP BY user_id, indexer_id, rsrc_id)",
            reverse_sql=ops.RunSQL.noop,
        ),
        ops.AddConstraint(
            model_name='UserFavorite',
            constraint=ops.UniqueConstraint(fields=('user', 'indexer', 'rsrc_id')),
        ),
        # older task conversations propagated facts into later turns without a role marker
        ops.RunSQL(
            """
            WITH first_task AS (
                SELECT conversation_id, MIN(id) AS start_id
                FROM assistant_turn
                WHERE json_extract(context, '$.page_type') IN ('active_downloads', 'completed_downloads')
                    OR id IN (
                        SELECT turn_id FROM assistant_tool_call
                        WHERE name IN ('list_download_tasks', 'get_download_task',
                                       'review_download_task_action', 'apply_download_task_action')
                            AND json_extract(result, '$.ok') = 1
                    )
                GROUP BY conversation_id
            )
            UPDATE assistant_turn AS turn
            SET resource_ids = json_insert(resource_ids, '$[#]', 'admin:downloads')
            FROM first_task
            WHERE turn.conversation_id = first_task.conversation_id
                AND turn.id >= first_task.start_id
                AND NOT EXISTS (SELECT 1 FROM json_each(turn.resource_ids) WHERE value = 'admin:downloads')
            """,
            reverse_sql=ops.RunSQL.noop,
        ),
        ops.RunSQL(
            """
            UPDATE assistant_conversation AS conversation
            SET summary_resources = json_insert(summary_resources, '$[#]', 'admin:downloads')
            WHERE EXISTS (
                SELECT 1 FROM assistant_turn AS turn, json_each(turn.resource_ids) AS resource
                WHERE turn.conversation_id = conversation.id AND turn.id <= conversation.summary_through
                    AND resource.value = 'admin:downloads'
            )
            AND NOT EXISTS (SELECT 1 FROM json_each(summary_resources) WHERE value = 'admin:downloads')
            """,
            reverse_sql=ops.RunSQL.noop,
        ),
    ]
