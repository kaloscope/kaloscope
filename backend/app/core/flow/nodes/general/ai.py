"""Generate text or validated structured data with a configured AI model."""

from typing import Any

from jinja2 import TemplateError
from pydantic import ValidationError

from app.core.ai.completion import output_validator
from app.core.exceptions import ErrorCode, KaloscopeException
from app.core.flow.context import Context
from app.core.flow.fields import (
    AIModelField,
    CodeField,
    NumberField,
    SelectField,
    TextField,
    ToggleField,
)
from app.core.flow.nodes.base import Node, general_node
from app.core.renderer import ENV, render
from app.models.ai import AICompletionConfig
from app.services.ai import AIProviderService

OUTPUT_SCHEMA = """{
  "type": "object",
  "properties": {
    "result": { "type": "string" }
  },
  "required": ["result"],
  "additionalProperties": false
}"""


@general_node(order=6, icon="layerDiagonalSparkle")
class AINode(Node):
    """Store a validated completion in a named workflow variable."""

    model = AIModelField(required=True)
    system_prompt = CodeField(language="jinja2", collapse=True, width="32rem")
    prompt = CodeField(required=True, language="jinja2", width="32rem")
    timeout = NumberField("timeout", required=True, min=1, max=300, default=60, span=50)
    max_retries = NumberField(
        min=0, max=3, default=0, tooltip="ai_max_retries", span=50
    )
    max_tokens = NumberField(
        min=0, max=131072, default=0, tooltip="ai_max_tokens", span=50
    )
    response_format = SelectField(
        required=True,
        options={
            "ai_text": "text",
            "ai_json": "json_object",
        },
        default="text",
        tooltip="ai_response_format",
        span=50,
    )
    output_schema = CodeField(
        language="json",
        width="32rem",
        default=OUTPUT_SCHEMA,
        visible_when={"response_format": "json_object"},
    )
    native_schema = ToggleField(
        tooltip="ai_native_schema",
        visible_when={"response_format": "json_object"},
    )
    result_key = TextField(
        required=True, maxlength=64, default="ai_result", tooltip="ai_result_key"
    )

    @classmethod
    def configuration(cls, node_data: dict[str, Any]) -> AICompletionConfig:
        """Validate persisted settings, prompt syntax, and the output contract.

        Args:
            node_data: Saved node fields, including optional execution metadata.

        Returns:
            Validated node settings with schema defaults applied.

        Raises:
            KaloscopeException: If settings, template syntax, or the JSON Schema
                are invalid. Validation errors never include prompt content.
        """
        try:
            config = AICompletionConfig.model_validate(
                {field.id: field.extract(node_data) for field in cls._fields}
            )
        except ValidationError:
            raise KaloscopeException(
                ErrorCode.AI_CONFIG_INVALID, status_code=400
            ) from None
        try:
            ENV.parse(config.system_prompt)
            ENV.parse(config.prompt)
        except TemplateError:
            raise KaloscopeException(
                ErrorCode.AI_PROMPT_INVALID, status_code=400
            ) from None
        output_validator(config)
        return config

    @classmethod
    async def validate_definition(cls, definition: dict) -> None:
        """Check AI dependencies before publishing a graph without calling a model.

        Args:
            definition: Compressed graph definition with stable node type strings.

        Raises:
            KaloscopeException: If any AI node's settings or provider are invalid.
        """
        for node in definition.get("nodes", []):
            data = node.get("data", {})
            if data.get("$schema") == "ai":
                config = cls.configuration(data)
                await AIProviderService.resolve_model(config.model)

    @classmethod
    async def execute(cls, *, node_data: dict[str, Any], context: Context, **kwargs):
        """Render prompts and assign output only after the entire call succeeds.

        Args:
            node_data: Configured fields and mutable execution metadata.
            context: Current workflow variables available to prompt templates.
            **kwargs: Unused engine execution arguments.

        Raises:
            KaloscopeException: If configuration, prompt rendering, provider state,
                the request, or the output fails validation.
        """
        node_data.pop("$error", None)
        try:
            config = cls.configuration(node_data)
            try:
                config = AICompletionConfig.model_validate(
                    {
                        **config.model_dump(),
                        "system_prompt": render(
                            config.system_prompt, context._context, strict=True
                        ),
                        "prompt": render(config.prompt, context._context, strict=True),
                    }
                )
            except (TemplateError, ValidationError):
                raise KaloscopeException(
                    ErrorCode.AI_PROMPT_INVALID, status_code=400
                ) from None
            result = await AIProviderService.complete(config)
            context.replace(config.result_key, result)
        except KaloscopeException as error:
            node_data["$error"] = str(error.message)
            raise
