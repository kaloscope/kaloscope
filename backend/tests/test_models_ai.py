"""Unit tests for AI provider configuration validation."""

import pytest
from pydantic import ValidationError

from app.core.ai.presets import get_provider_presets
from app.models.ai import (
    AIProtocol,
    AIProviderConnection,
    AIProviderPreset,
    AIProviderUpsert,
)


def test_custom_provider():
    provider = AIProviderUpsert(
        name=" Custom gateway ",
        base_url=" https://AI.Example.com:443/v1/ ",
        model_ids=[" vendor/model-one ", "model-two:latest"],
        api_key=" test-key ",
    )

    assert provider.name == "Custom gateway"
    assert provider.base_url == "https://ai.example.com/v1"
    assert provider.model_ids == ["vendor/model-one", "model-two:latest"]
    assert provider.api_key.get_secret_value() == "test-key"
    assert provider.preset is None
    assert provider.protocol == AIProtocol.CHAT_COMPLETIONS
    assert provider.enabled is True
    assert "test-key" not in provider.model_dump_json()
    assert "test-key" not in repr(provider)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/model",
        "ftp://example.com",
        "https://",
        "example.com/v1",
        "https://user:password@example.com/v1",
        "https://example.com/v1?key=secret",
        "https://example.com/v1?",
        "https://example.com/v1#fragment",
        "https://example.com:invalid/v1",
        "https://example.com/path with spaces",
        "https://example.com/path\npart",
    ],
)
@pytest.mark.parametrize("model", [AIProviderConnection, AIProviderUpsert])
def test_invalid_url(url, model):
    with pytest.raises(ValidationError):
        model.model_validate(
            {
                "base_url": url,
                **({"name": "Invalid"} if model is AIProviderUpsert else {}),
            }
        )


def test_draft_connection():
    draft = AIProviderConnection(
        base_url=" https://AI.Example.com:443/v1/ ", api_key=" key "
    )

    assert draft.base_url == "https://ai.example.com/v1"
    assert draft.api_key.get_secret_value() == "key"
    assert '"key"' not in draft.model_dump_json()
    assert draft.id is None

    with pytest.raises(ValidationError):
        AIProviderConnection(base_url="https://example.com", api_key="key\nheader")


@pytest.mark.parametrize(
    "fields",
    [
        {"name": " "},
        {"name": "x" * 65},
        {"id": 0},
        {"preset": "unknown"},
        {"protocol": "unknown"},
        {"model_ids": [""]},
        {"model_ids": ["x" * 256]},
        {"model_ids": [str(i) for i in range(101)]},
        {"model_ids": ["model", " model "]},
        {"model_ids": ["model\nname"]},
        {"api_key": "key\r\nAuthorization: injected"},
        {"api_key": "密钥"},
        {"api_key": "x" * 4097},
        {"has_api_key": True},
    ],
)
def test_invalid_config(fields):
    with pytest.raises(ValidationError):
        AIProviderUpsert.model_validate(
            {"name": "Test", "base_url": "http://localhost:11434/v1", **fields}
        )


def test_validation_redacts_secrets():
    with pytest.raises(ValidationError) as error:
        AIProviderUpsert(
            name="", base_url="https://example.com", api_key="secret\nheader"
        )

    assert "secret" not in str(error.value)
    assert "header" not in str(error.value)


def test_presets():
    presets = get_provider_presets()

    assert {preset["id"] for preset in presets} == set(AIProviderPreset)
    for preset in presets:
        provider = AIProviderUpsert(
            name=preset["name"],
            preset=preset["id"],
            protocol=preset["protocol"],
            base_url=preset["base_url"],
        )
        assert provider.model_ids == []
        assert provider.api_key is None
        assert preset["api_key_required"] == (
            provider.preset not in {AIProviderPreset.OLLAMA, AIProviderPreset.LMSTUDIO}
        )

    presets[0]["name"] = "Changed"
    assert get_provider_presets()[0]["name"] == "OpenAI"
