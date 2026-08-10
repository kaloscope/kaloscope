"""Unit tests for the crypto utility."""

import pytest

from app.utils.crypto import xor_decrypt, xor_encrypt


def test_roundtrip_basic_string():
    """Test encryption and decryption of a basic ASCII string."""
    plain_text = "Hello, World!"
    encrypted = xor_encrypt(plain_text)
    decrypted = xor_decrypt(encrypted)
    assert decrypted == plain_text


def test_roundtrip_empty_string():
    """Test encryption and decryption of an empty string."""
    plain_text = ""
    encrypted = xor_encrypt(plain_text)
    decrypted = xor_decrypt(encrypted)
    assert decrypted == plain_text


def test_roundtrip_unicode():
    """Test encryption and decryption with Unicode characters."""
    plain_text = "你好世界 🌍 Hello!"
    encrypted = xor_encrypt(plain_text)
    decrypted = xor_decrypt(encrypted)
    assert decrypted == plain_text


def test_roundtrip_special_chars():
    """Test encryption and decryption with special characters."""
    plain_text = "!@#$%^&*()_+-=[]{}|;:',.<>?/~`"
    encrypted = xor_encrypt(plain_text)
    decrypted = xor_decrypt(encrypted)
    assert decrypted == plain_text


def test_roundtrip_numbers():
    """Test encryption and decryption with numeric strings."""
    plain_text = "1234567890"
    encrypted = xor_encrypt(plain_text)
    decrypted = xor_decrypt(encrypted)
    assert decrypted == plain_text


def test_roundtrip_multiline_text():
    """Test encryption and decryption with multiline text."""
    plain_text = "Line 1\nLine 2\nLine 3"
    encrypted = xor_encrypt(plain_text)
    decrypted = xor_decrypt(encrypted)
    assert decrypted == plain_text


def test_roundtrip_long_text():
    """Test encryption and decryption with long text."""
    plain_text = "A" * 1000
    encrypted = xor_encrypt(plain_text)
    decrypted = xor_decrypt(encrypted)
    assert decrypted == plain_text


def test_encrypted_output_is_base64():
    """Test that encrypted output is valid Base64."""
    plain_text = "Test string"
    encrypted = xor_encrypt(plain_text)
    # Base64 should only contain alphanumeric characters, +, /, and =
    assert all(c.isalnum() or c in "+/=" for c in encrypted)


def test_deterministic_output():
    """Test that encrypting the same input twice produces the same output."""
    plain_text = "Consistent output"
    encrypted1 = xor_encrypt(plain_text)
    encrypted2 = xor_encrypt(plain_text)
    assert encrypted1 == encrypted2


def test_legacy_ciphertext_compatible():
    """Keep ciphertext created with the default key compatible."""
    encrypted = "JwQLDhAaTwMAKBMJGw=="

    assert xor_encrypt("legacy secret") == encrypted
    assert xor_decrypt(encrypted) == "legacy secret"


def test_custom_key_roundtrip():
    """Round-trip text with an injected `key`."""
    encrypted = xor_encrypt("令牌 secret", key="instance-key")

    assert xor_decrypt(encrypted, key="instance-key") == "令牌 secret"


def test_empty_custom_key_rejected():
    """Reject an empty injected `key`."""
    with pytest.raises(ValueError):
        xor_encrypt("secret", key=b"")


def test_distinct_inputs_distinct_outputs():
    """Test that different inputs produce different encrypted outputs."""
    plain_text1 = "Text 1"
    plain_text2 = "Text 2"
    encrypted1 = xor_encrypt(plain_text1)
    encrypted2 = xor_encrypt(plain_text2)
    assert encrypted1 != encrypted2


def test_invalid_base64_raises():
    """Test that decrypting invalid Base64 raises an error."""
    invalid_encrypted = "This is not valid Base64!!!"
    with pytest.raises(ValueError):
        xor_decrypt(invalid_encrypted)
