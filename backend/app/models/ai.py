"""Define AI provider configuration and request validation."""

from enum import StrEnum, auto
from typing import Annotated, Literal

from httpx import URL, InvalidURL
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    SecretStr,
    StringConstraints,
    field_validator,
)
from tortoise.fields import BooleanField, CharEnumField, CharField, JSONField, TextField

from app.models.base import TortoiseModel


class AIProviderPreset(StrEnum):
    """Identify built-in provider presets without restricting custom endpoints."""

    OPENAI = auto()
    ANTHROPIC = auto()
    GOOGLE = auto()
    XAI = auto()
    DEEPSEEK = auto()
    OPENROUTER = auto()
    OLLAMA = auto()
    LMSTUDIO = auto()


class AIProtocol(StrEnum):
    """Identify the API format used by a configured provider."""

    CHAT_COMPLETIONS = auto()


class AIProvider(TortoiseModel):
    """Store one named connection and its explicitly configured model IDs.

    Keep `api_key` as Fernet ciphertext and exclude it from public responses.
    """

    name = CharField(max_length=64, unique=True)
    preset = CharEnumField(enum_type=AIProviderPreset, max_length=32, null=True)
    protocol = CharEnumField(
        enum_type=AIProtocol, max_length=32, default=AIProtocol.CHAT_COMPLETIONS
    )
    base_url = CharField(max_length=2048)
    api_key = TextField(null=True)
    model_ids = JSONField[list[str]](default=list)
    enabled = BooleanField(default=True)

    def has_api_key(self) -> bool:
        """Report whether a credential is stored without exposing it.

        Returns:
            Whether the provider contains an encrypted API key.
        """
        return bool(self.api_key)

    class Meta:
        table = "ai_provider"
        ordering = ["id"]

    class PydanticMeta:
        exclude = ("api_key",)
        computed = ("has_api_key",)


type ModelID = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
]


class AIProviderConnection(BaseModel):
    """Validate a draft connection and optional reference to its saved credential.

    Omit `api_key` or use `null` to reuse the saved key at the same endpoint.
    An empty string explicitly requests a connection without a key.
    """

    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True
    )

    id: PositiveInt | None = None
    preset: AIProviderPreset | None = None
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        """Normalize an HTTP endpoint without accepting embedded credentials.

        Args:
            value: Base URL including the provider's API version path.

        Returns:
            Normalized URL without a trailing slash.

        Raises:
            ValueError: If the URL is invalid or contains credentials, query
                parameters, fragments, or whitespace.
        """
        try:
            url = URL(value)
        except InvalidURL:
            raise ValueError("Invalid provider URL") from None
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.userinfo
            or "?" in value
            or "#" in value
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError("Use an HTTP URL without credentials, query, or fragment")
        return str(url).rstrip("/")

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        """Normalize a credential and reject characters unsafe for HTTP headers.

        Args:
            value: Optional credential; a blank value explicitly clears it.

        Returns:
            Trimmed secret or `None` when the existing credential should be retained.

        Raises:
            ValueError: If a nonempty credential contains whitespace or non-ASCII
                characters.
        """
        if value is None:
            return None
        secret = value.get_secret_value().strip()
        if any(not 33 <= ord(char) <= 126 for char in secret):
            raise ValueError("API key must contain only printable ASCII without spaces")
        return SecretStr(secret)


class AIProviderUpsert(AIProviderConnection):
    """Create or replace connection settings while treating credentials separately.

    Omit `api_key` or use `null` to retain an existing credential. An empty string
    clears it. Changing `base_url` with a stored credential requires an explicit
    replacement or an empty string. An empty `model_ids` list is valid while the
    connection is being configured. A `null` preset represents a custom provider.
    """

    name: str = Field(min_length=1, max_length=64)
    protocol: AIProtocol = AIProtocol.CHAT_COMPLETIONS
    model_ids: list[ModelID] = Field(default_factory=list, max_length=100)
    enabled: bool = True

    @field_validator("model_ids")
    @classmethod
    def validate_model_ids(cls, value: list[str]) -> list[str]:
        """Require distinct model IDs that can be displayed safely.

        Args:
            value: Trimmed model identifiers supplied by the administrator.

        Returns:
            Model identifiers in the supplied order.

        Raises:
            ValueError: If identifiers are duplicated or contain control characters.
        """
        if len(set(value)) != len(value):
            raise ValueError("Model IDs must be unique")
        if any(ord(char) < 32 or ord(char) == 127 for item in value for char in item):
            raise ValueError("Model IDs must not contain control characters")
        return value


class AIModelSelection(BaseModel):
    """Reference a saved provider and one explicitly configured model."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    provider_id: int = Field(gt=0, strict=True)
    model_id: ModelID


class AICompletionConfig(BaseModel):
    """Validate AI node settings without storing connection details in workflows."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    model: AIModelSelection
    system_prompt: str = Field(default="", max_length=65536)
    prompt: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=131072)
    ]
    response_format: Literal["text", "json_object"] = "text"
    native_schema: bool = Field(default=False, strict=True)
    output_schema: str = Field(default="", max_length=32768)
    timeout: int = Field(default=60, ge=1, le=300, strict=True)
    max_retries: int = Field(default=0, ge=0, le=3, strict=True)
    max_tokens: int = Field(default=0, ge=0, le=131072, strict=True)
    result_key: str = Field(
        default="ai_result", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
    )
