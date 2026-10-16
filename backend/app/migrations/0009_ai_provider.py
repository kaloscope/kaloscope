from tortoise import migrations
from tortoise.migrations import operations as ops
from app.models.ai import AIProtocol, AIProviderPreset
from orjson import loads
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
    ]
