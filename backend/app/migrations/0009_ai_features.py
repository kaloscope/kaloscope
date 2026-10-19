from tortoise import migrations
from tortoise.migrations import operations as ops
from app.models.ai import AIProtocol, AIProviderPreset
from app.models.mcp import MCPTokenScope
from orjson import loads
from tortoise.fields.base import OnDelete
from tortoise.fields.data import JSON_DUMPS
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0008_media_original')]

    initial = False

    operations = [
        ops.CreateModel(
            name='AIProvider',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('name', fields.CharField(unique=True, max_length=64)),
                ('preset', fields.CharEnumField(null=True, description='OPENAI: openai\nANTHROPIC: anthropic\nGOOGLE: google\nXAI: xai\nDEEPSEEK: deepseek\nOPENROUTER: openrouter\nOLLAMA: ollama\nLMSTUDIO: lmstudio', enum_type=AIProviderPreset, max_length=32)),
                ('protocol', fields.CharEnumField(default=AIProtocol.CHAT_COMPLETIONS, description='CHAT_COMPLETIONS: chat_completions', enum_type=AIProtocol, max_length=32)),
                ('base_url', fields.CharField(max_length=2048)),
                ('api_key', fields.TextField(null=True, unique=False)),
                ('model_ids', fields.JSONField(default=list, encoder=JSON_DUMPS, decoder=loads)),
                ('enabled', fields.BooleanField(default=True)),
            ],
            options={'table': 'ai_provider', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Store one named connection and its explicitly configured model IDs.'},
            bases=['TortoiseModel'],
        ),
        ops.CreateModel(
            name='FlowDraftRevision',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('graph', fields.ForeignKeyField('models.FlowGraph', source_field='graph_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('version', fields.IntField()),
                ('draft', fields.JSONField(encoder=JSON_DUMPS, decoder=loads)),
            ],
            options={'table': 'flow_draft_revision', 'app': 'models', 'unique_together': (('graph', 'version'),), 'pk_attr': 'id', 'table_description': 'Retain recent saved drafts for review and version-checked restoration.'},
            bases=['TortoiseModel'],
        ),
        ops.CreateModel(
            name='MCPAccessToken',
            fields=[
                ('id', fields.IntField(generated=True, primary_key=True, unique=True, db_index=True)),
                ('created_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(null=True, auto_now=True, auto_now_add=False)),
                ('user', fields.ForeignKeyField('models.User', source_field='user_id', db_index=True, db_constraint=True, to_field='id', related_name=False, on_delete=OnDelete.CASCADE)),
                ('name', fields.CharField(max_length=64)),
                ('token_hash', fields.CharField(unique=True, max_length=64)),
                ('prefix', fields.CharField(max_length=17)),
                ('scope', fields.CharEnumField(default=MCPTokenScope.READ_ONLY, description='READ_ONLY: read_only\nREAD_WRITE: read_write', enum_type=MCPTokenScope, max_length=32)),
                ('expires_at', fields.DatetimeField(auto_now=False, auto_now_add=False)),
                ('last_used_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
            ],
            options={'table': 'mcp_access_token', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Store a user-owned MCP credential digest and its fixed lifetime.'},
            bases=['TortoiseModel'],
        ),
        ops.AddField(
            model_name='FlowGraph',
            name='draft_version',
            field=fields.IntField(default=0, db_default=0),
        ),
    ]
