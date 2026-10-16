"""Provide password hashing, legacy XOR encoding, and Fernet encryption.

Keep XOR ciphertext and the active instance key compatible with existing data.
Fernet reads the same raw 32-byte `secret.key` and converts it to Base64 only in
memory. It never falls back to XOR or the application-name key.
"""

import base64
import hashlib
import os
import secrets
from pathlib import Path
from tempfile import NamedTemporaryFile

from cryptography.fernet import Fernet, InvalidToken
from filelock import FileLock

from app.core.config import KaloscopeConfig
from app.core.constants import APP_NAME, ENCODING

_DEFAULT_KEY_BYTES = APP_NAME.encode(ENCODING)
_KEY_BYTES = _DEFAULT_KEY_BYTES
_KEY_SIZE = 32


def load_key(app_config: KaloscopeConfig, *, create: bool = False) -> bytes:
    """Configure the active secret key from the application configuration.

    Args:
        app_config: Application configuration.
        create: Whether to create the key file when absent. Defaults to `False`.

    Returns:
        Active secret key bytes.
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
    """Initialize a raw 32-byte secret key atomically with private permissions.

    Args:
        path: Shared secret key file; existing contents are never replaced.

    Raises:
        OSError: If the directory or key cannot be created or secured.
        Timeout: If another process holds the initialization lock too long.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(f"{path}.lock", timeout=10):
        if path.exists():
            path.chmod(0o600)
            return
        with NamedTemporaryFile(
            dir=path.parent, prefix=".secret-key-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(secrets.token_bytes(_KEY_SIZE))
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)


def _key_bytes(key: str | bytes | None) -> bytes:
    """Resolve explicit key bytes or use the active instance key.

    Args:
        key: Text or byte key; `None` uses the active instance key.

    Returns:
        Nonempty key bytes.

    Raises:
        ValueError: If an explicit key is empty.
    """
    if key is None:
        return _KEY_BYTES
    if isinstance(key, str):
        key = key.encode(ENCODING)
    if not key:
        raise ValueError("Secret key cannot be empty")
    return key


def encrypt(password: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256.

    Args:
        password: Password to hash.

    Returns:
        Hexadecimal password hash.
    """
    salt = _KEY_BYTES
    hash = hashlib.pbkdf2_hmac("sha256", password.encode(ENCODING), salt, 100000)
    return hash.hex()


def xor(input_bytes: bytes, *, key: str | bytes | None = None) -> bytes:
    """XOR the input bytes with the key bytes.

    Args:
        input_bytes: Input bytes.
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
        plain_text: Plain text to encrypt.
        key: An optional custom key. The active secret key is used by default.

    Returns:
        Encrypted string in Base64 format.
    """
    encrypted_bytes = xor(plain_text.encode(ENCODING), key=key)
    return base64.b64encode(encrypted_bytes).decode(ENCODING)


def xor_decrypt(encrypted_text: str, *, key: str | bytes | None = None) -> str:
    """Decrypt the encrypted text using XOR operation from Base64 format.

    Args:
        encrypted_text: Encrypted text in Base64 format.
        key: An optional custom key. The active secret key is used by default.

    Returns:
        Decrypted plain text.
    """
    encrypted_bytes = base64.b64decode(encrypted_text)
    return xor(encrypted_bytes, key=key).decode(ENCODING)


def _load_fernet_cipher(key_path: Path, *, create_key: bool) -> Fernet:
    """Load a Fernet cipher from the existing raw secret key format.

    Args:
        key_path: File containing the raw 32-byte instance key used by XOR.
        create_key: Whether initialization is allowed when the file is missing.
            Callers must disable this when existing ciphertext depends on the key.

    Returns:
        Authenticated cipher using the same key bytes, encoded only in memory.

    Raises:
        RuntimeError: If the key is missing or malformed.
        OSError: If the key cannot be read or written.
        Timeout: If another process holds the initialization lock too long.
    """
    if create_key:
        _create_key(key_path)
    try:
        return Fernet(base64.urlsafe_b64encode(key_path.read_bytes()))
    except (FileNotFoundError, ValueError):
        raise RuntimeError("Fernet key is missing or invalid") from None


def fernet_encrypt(plain_text: str, key_path: Path, *, create_key: bool = False) -> str:
    """Encrypt text using Fernet and an explicitly selected key file.

    Args:
        plain_text: Text to encrypt.
        key_path: Shared raw 32-byte `secret.key`, also usable by the XOR helpers.
        create_key: Whether a missing key may be initialized. Defaults to `False`;
            enable only when no stored ciphertext relies on this key.

    Returns:
        URL-safe Base64-encoded Fernet token.

    Raises:
        RuntimeError: If the key is missing or malformed.
        OSError: If the key cannot be read or written.
        Timeout: If another process holds the initialization lock too long.
    """
    return (
        _load_fernet_cipher(key_path, create_key=create_key)
        .encrypt(plain_text.encode(ENCODING))
        .decode("ascii")
    )


def fernet_decrypt(encrypted_text: str, key_path: Path) -> str:
    """Decrypt a Fernet token without creating keys or trying legacy XOR.

    Args:
        encrypted_text: Fernet token containing UTF-8 text.
        key_path: Shared raw 32-byte `secret.key` used to encrypt the token.

    Returns:
        Original plaintext after successful authentication.

    Raises:
        RuntimeError: If the key is missing, malformed, or cannot decrypt the token.
        OSError: If the key cannot be read.
    """
    cipher = _load_fernet_cipher(key_path, create_key=False)
    try:
        return cipher.decrypt(encrypted_text).decode(ENCODING)
    except (InvalidToken, UnicodeError):
        raise RuntimeError("Fernet ciphertext cannot be decrypted") from None
