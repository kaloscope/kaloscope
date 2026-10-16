"""Describe provider defaults shared by the configuration API and its clients."""

from dataclasses import asdict, dataclass
from typing import Any

from app.models.ai import AIProtocol, AIProviderPreset


@dataclass(frozen=True)
class ProviderPreset:
    """Describe connection defaults without promising individual model capabilities."""

    id: AIProviderPreset
    name: str
    base_url: str
    api_key_required: bool = True
    protocol: AIProtocol = AIProtocol.CHAT_COMPLETIONS


_PRESETS = (
    ProviderPreset(AIProviderPreset.OPENAI, "OpenAI", "https://api.openai.com/v1"),
    ProviderPreset(
        AIProviderPreset.ANTHROPIC, "Anthropic", "https://api.anthropic.com/v1"
    ),
    ProviderPreset(
        AIProviderPreset.GOOGLE,
        "Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    ProviderPreset(AIProviderPreset.XAI, "xAI", "https://api.x.ai/v1"),
    ProviderPreset(AIProviderPreset.DEEPSEEK, "DeepSeek", "https://api.deepseek.com"),
    ProviderPreset(
        AIProviderPreset.OPENROUTER, "OpenRouter", "https://openrouter.ai/api/v1"
    ),
    ProviderPreset(
        AIProviderPreset.OLLAMA, "Ollama", "http://127.0.0.1:11434/v1", False
    ),
    ProviderPreset(
        AIProviderPreset.LMSTUDIO, "LM Studio", "http://127.0.0.1:1234/v1", False
    ),
)


def get_provider_presets() -> list[dict[str, Any]]:
    """Return editable connection defaults without creating database records.

    Returns:
        Fresh preset dictionaries, excluding model IDs and credentials.
    """
    return [asdict(preset) for preset in _PRESETS]
