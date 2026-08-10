"""Password hashing and lightweight encoding utilities."""

import base64
import hashlib
import os
import secrets
from pathlib import Path

from app.core.config import KaloscopeConfig
from app.core.constants import APP_NAME, ENCODING

_DEFAULT_KEY_BYTES = APP_NAME.encode(ENCODING)
_KEY_BYTES = _DEFAULT_KEY_BYTES
_KEY_SIZE = 32


def load_key(app_config: KaloscopeConfig, *, create: bool = False) -> bytes:
    """Configure the active secret key from the application configuration.

    Args:
        app_config: The application configuration.
        create: Whether to create the key file when absent.

    Returns:
        The active secret key bytes.
    """
    global _KEY_BYTES
    if not app_config.secret_key_enabled:
        _KEY_BYTES = _DEFAULT_KEY_BYTES
        return _KEY_BYTES
    path = app_config.secret_key_path
    if create:
        _create_key(path)
    _KEY_BYTES = _key_bytes(path.read_bytes())
    return _KEY_BYTES


def _create_key(path: Path):
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        path.chmod(0o600)
        return
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(secrets.token_bytes(_KEY_SIZE))
        path.chmod(0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _key_bytes(key: str | bytes | None) -> bytes:
    if key is None:
        return _KEY_BYTES
    if isinstance(key, str):
        key = key.encode(ENCODING)
    if not key:
        raise ValueError("Secret key cannot be empty")
    return key


def encrypt(password: str) -> str:
    """Encrypt the password using PBKDF2-HMAC-SHA256.

    Args:
        password: The password to encrypt.

    Returns:
        The encrypted password.
    """
    salt = _KEY_BYTES
    hash = hashlib.pbkdf2_hmac("sha256", password.encode(ENCODING), salt, 100000)
    return hash.hex()


def xor(input_bytes: bytes, *, key: str | bytes | None = None) -> bytes:
    """XOR the input bytes with the key bytes.

    Args:
        input_bytes: The input bytes.
        key: An optional custom key. The active secret key is used by default.

    Returns:
        The XOR-ed bytes.
    """
    key_bytes = _key_bytes(key)
    return bytes(
        value ^ key_bytes[index % len(key_bytes)]
        for index, value in enumerate(input_bytes)
    )


def xor_encrypt(plain_text: str, *, key: str | bytes | None = None) -> str:
    """Encrypt the plain text using XOR operation with Base64 encoding.

    Args:
        plain_text: The plain text to encrypt.
        key: An optional custom key. The active secret key is used by default.

    Returns:
        The encrypted string in Base64 format.
    """
    encrypted_bytes = xor(plain_text.encode(ENCODING), key=key)
    return base64.b64encode(encrypted_bytes).decode(ENCODING)


def xor_decrypt(encrypted_text: str, *, key: str | bytes | None = None) -> str:
    """Decrypt the encrypted text using XOR operation from Base64 format.

    Args:
        encrypted_text: The encrypted text in Base64 format.
        key: An optional custom key. The active secret key is used by default.

    Returns:
        The decrypted plain text.
    """
    encrypted_bytes = base64.b64decode(encrypted_text)
    return xor(encrypted_bytes, key=key).decode(ENCODING)
