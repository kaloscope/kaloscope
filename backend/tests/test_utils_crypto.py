"""Unit tests for the crypto utility."""

import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import cast

import pytest

from app.core.config import KaloscopeConfig
from app.utils import crypto
from app.utils.crypto import (
    encrypt,
    fernet_decrypt,
    fernet_encrypt,
    load_key,
    xor_decrypt,
    xor_encrypt,
)


def configuration(**values: object) -> KaloscopeConfig:
    """Build a minimal configuration substitute for secret-key tests.

    Args:
        **values: Configuration attributes exercised by the current scenario.

    Returns:
        Structural substitute exposing the requested configuration attributes.
    """
    return cast(KaloscopeConfig, SimpleNamespace(**values))


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
    """Keep encrypted text created with the default key compatible."""
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


@pytest.mark.parametrize(
    "plain_text",
    ["Hello, World!", "", "你好世界 🌍 Hello!", "A" * 1000],
    ids=["ascii", "empty", "unicode", "long"],
)
def test_fernet_roundtrip(tmp_path, plain_text):
    key_path = tmp_path / "database" / "secret.key"

    ciphertext = fernet_encrypt(plain_text, key_path, create_key=True)
    key = key_path.read_bytes()
    second = fernet_encrypt(plain_text, key_path)

    assert second != ciphertext
    assert fernet_decrypt(ciphertext, key_path) == plain_text
    assert fernet_decrypt(second, key_path) == plain_text
    assert key_path.read_bytes() == key
    assert len(key) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert list(key_path.parent.glob(".secret-key-*")) == []


def test_fernet_existing_ciphertext(tmp_path):
    key_path = tmp_path / "secret.key"
    key_path.write_bytes(bytes(range(32)))
    ciphertext = (
        "gAAAAABq0fOmU42kEtc0wAWCVwvOvA1YAajLswPlTNN_udlg_YTGQeMukNEtU1tQR9AK"
        "N3chfydcDDO6m5020ZtUBZa1G3ac1cVucEL-J5XRGV8Fp8mz5Pw="
    )

    assert fernet_decrypt(ciphertext, key_path) == "legacy AI credential"


def test_shared_initialization(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_KEY_BYTES", crypto._DEFAULT_KEY_BYTES)
    key_path = tmp_path / "secret.key"
    config = configuration(secret_key_enabled=True, secret_key_path=key_path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(fernet_encrypt, f"key-{i}", key_path, create_key=True)
            if i % 2
            else executor.submit(load_key, config, create=True)
            for i in range(8)
        ]
        results = [future.result() for future in futures]

    key = key_path.read_bytes()
    assert len(key) == 32
    assert key == crypto._KEY_BYTES
    for i, result in enumerate(results):
        if i % 2:
            assert isinstance(result, str)
            assert fernet_decrypt(result, key_path) == f"key-{i}"
        else:
            assert result == key


@pytest.mark.parametrize("failure", ["missing", "invalid", "wrong", "tampered"])
def test_fernet_unavailable(tmp_path, failure):
    key_path = tmp_path / "credentials.key"
    ciphertext = fernet_encrypt("test-api-key", key_path, create_key=True)
    if failure == "missing":
        key_path.unlink()
    elif failure == "invalid":
        key_path.write_bytes(b"invalid")
    elif failure == "wrong":
        other = tmp_path / "other.key"
        fernet_encrypt("other", other, create_key=True)
        key_path.write_bytes(other.read_bytes())
    else:
        ciphertext = ciphertext[:40] + "!" + ciphertext[41:]

    with pytest.raises(RuntimeError) as error:
        fernet_decrypt(ciphertext, key_path)

    assert "test-api-key" not in str(error.value)
    assert ciphertext not in str(error.value)
    if failure == "missing":
        assert not key_path.exists()


def test_fernet_missing_key(tmp_path):
    key_path = tmp_path / "credentials.key"

    with pytest.raises(RuntimeError, match="missing or invalid"):
        fernet_encrypt("replacement", key_path)

    assert not key_path.exists()


@pytest.mark.parametrize("key", [b"", b"invalid", b"x" * 44])
def test_fernet_invalid_key(tmp_path, key):
    key_path = tmp_path / "credentials.key"
    key_path.write_bytes(key)

    with pytest.raises(RuntimeError, match="missing or invalid"):
        fernet_encrypt("replacement", key_path, create_key=True)

    assert key_path.read_bytes() == key


def test_fernet_rejects_xor(tmp_path):
    key_path = tmp_path / "credentials.key"
    fernet_encrypt("initialize", key_path, create_key=True)
    ciphertext = xor_encrypt("legacy secret")

    with pytest.raises(RuntimeError, match="cannot be decrypted"):
        fernet_decrypt(ciphertext, key_path)

    assert xor_decrypt(ciphertext) == "legacy secret"


@pytest.mark.parametrize("enabled", [False, True])
def test_shared_key(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(crypto, "_KEY_BYTES", crypto._DEFAULT_KEY_BYTES)
    instance_path = tmp_path / "secret.key"
    instance_key = bytes(range(32))
    instance_path.write_bytes(instance_key)
    config = configuration(secret_key_enabled=enabled, secret_key_path=instance_path)
    loaded = load_key(config)
    xor_ciphertext = xor_encrypt("existing variable")
    password_hash = encrypt("existing password")

    ciphertext = fernet_encrypt("AI credential", instance_path, create_key=True)

    assert loaded == (instance_key if enabled else crypto._DEFAULT_KEY_BYTES)
    assert instance_path.read_bytes() == instance_key
    assert xor_encrypt("existing variable") == xor_ciphertext
    assert xor_decrypt(xor_ciphertext) == "existing variable"
    assert encrypt("existing password") == password_hash

    load_key(configuration(secret_key_enabled=False))

    assert fernet_decrypt(ciphertext, instance_path) == "AI credential"


def test_existing_secret_key(tmp_path, monkeypatch):
    monkeypatch.setattr(crypto, "_KEY_BYTES", crypto._DEFAULT_KEY_BYTES)
    key_path = tmp_path / "secret.key"
    existing_key = bytes(range(32))
    key_path.write_bytes(existing_key)
    modified_at = key_path.stat().st_mtime_ns
    config = configuration(secret_key_enabled=True, secret_key_path=key_path)

    assert load_key(config, create=True) == existing_key
    ciphertext = fernet_encrypt("new credential", key_path, create_key=True)

    assert key_path.read_bytes() == existing_key
    assert key_path.stat().st_mtime_ns == modified_at
    assert xor_decrypt("bGRlYmd8JnRtanhueA==") == "legacy secret"
    assert xor_encrypt("legacy secret") == "bGRlYmd8JnRtanhueA=="
    assert encrypt("compatibility-test-password") == (
        "ecd19109156fe32f21c331d50a9755579a1b0667f98c9bc8769965e9ddfbaac9"
    )
    assert fernet_decrypt(ciphertext, key_path) == "new credential"


@pytest.mark.parametrize("first", ["startup", "fernet"])
def test_key_creation_order(tmp_path, monkeypatch, first):
    monkeypatch.setattr(crypto, "_KEY_BYTES", crypto._DEFAULT_KEY_BYTES)
    key_path = tmp_path / "database" / "secret.key"
    config = configuration(secret_key_enabled=True, secret_key_path=key_path)
    if first == "startup":
        load_key(config, create=True)
    else:
        fernet_encrypt("first credential", key_path, create_key=True)
    original = key_path.read_bytes()

    loaded = load_key(config, create=True)
    ciphertext = fernet_encrypt("credential", key_path, create_key=True)

    assert len(original) == 32
    assert loaded == original == key_path.read_bytes()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert xor_decrypt(xor_encrypt("existing variable"), key=original) == (
        "existing variable"
    )
    assert fernet_decrypt(ciphertext, key_path) == "credential"


def test_interrupted_key_creation(tmp_path, monkeypatch):
    key_path = tmp_path / "database" / "secret.key"

    def fail_replace(source, target):
        raise OSError("Simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(crypto.os, "replace", fail_replace)
        with pytest.raises(OSError, match="Simulated write failure"):
            fernet_encrypt("credential", key_path, create_key=True)

    assert not key_path.exists()
    assert list(key_path.parent.glob(".secret-key-*")) == []

    ciphertext = fernet_encrypt("credential", key_path, create_key=True)

    assert fernet_decrypt(ciphertext, key_path) == "credential"


def test_password_compatibility(monkeypatch):
    monkeypatch.setattr(crypto, "_KEY_BYTES", crypto._DEFAULT_KEY_BYTES)

    assert encrypt("compatibility-test-password") == (
        "476ef15906a9585d2e51334ab01013b18d43ca2f4b5881eb9fbd7718dde91730"
    )
