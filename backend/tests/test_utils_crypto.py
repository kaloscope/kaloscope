"""Unit tests for the crypto utility."""

import pytest

from app.utils.crypto import xor_decrypt, xor_encrypt


@pytest.mark.parametrize(
    "plain_text",
    ["Hello, World!", "", "你好世界 🌍 Hello!", "A" * 1000],
    ids=["ascii", "empty", "unicode", "long"],
)
def test_roundtrip(plain_text: str):
    """Preserve text across encryption and decryption."""
    encrypted = xor_encrypt(plain_text)

    assert xor_decrypt(encrypted) == plain_text


def test_legacy_ciphertext():
    """Keep ciphertext created with the default key compatible."""
    encrypted = "JwQLDhAaTwMAKBMJGw=="

    assert xor_encrypt("legacy secret") == encrypted
    assert xor_decrypt(encrypted) == "legacy secret"


def test_custom_key_roundtrip():
    """Round-trip text with an injected `key`."""
    encrypted = xor_encrypt("令牌 secret", key="instance-key")

    assert xor_decrypt(encrypted, key="instance-key") == "令牌 secret"


def test_empty_key():
    """Reject an empty injected `key`."""
    with pytest.raises(ValueError):
        xor_encrypt("secret", key=b"")


def test_invalid_base64():
    """Test that decrypting invalid Base64 raises an error."""
    invalid_encrypted = "This is not valid Base64!!!"
    with pytest.raises(ValueError):
        xor_decrypt(invalid_encrypted)
