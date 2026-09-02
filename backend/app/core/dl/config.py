import yaml
from pydantic import ValidationError
from sanic.log import logger

from app.core.config import KaloscopeConfig
from app.core.dl.openlist import OpenListConfig, OpenListDriver
from app.core.dl.rpc import RpcConfig, RpcDriver
from app.core.exceptions import ErrorCode, KaloscopeException
from app.utils.crypto import xor_decrypt, xor_encrypt

_CONFIG_FACTORIES = {"rpc": RpcConfig, "openlist": OpenListConfig}
_DRIVER_FACTORIES = {"rpc": RpcDriver, "openlist": OpenListDriver}


def encrypt_config(config: str) -> str:
    """Encrypt the complete downloader YAML when the secret key is enabled.

    Args:
        config: The downloader YAML configuration.

    Returns:
        The encrypted configuration, or the original configuration when disabled.
    """
    if not KaloscopeConfig.get().secret_key_enabled:
        return config
    return xor_encrypt(config)


def decrypt_config(config: str) -> str:
    """Decrypt the complete downloader YAML when the secret key is enabled.

    Args:
        config: The stored downloader YAML configuration.

    Returns:
        The decrypted configuration, or the original configuration when disabled.
    """
    if not KaloscopeConfig.get().secret_key_enabled:
        return config
    return xor_decrypt(config)


def load_config(config: str) -> RpcConfig | OpenListConfig:
    """Load a downloader configuration selected by its `driver` field.

    Args:
        config: The downloader YAML configuration.

    Raises:
        KaloscopeException: If the configuration cannot be parsed or validated.

    Returns:
        The validated downloader configuration.
    """
    try:
        yaml_config = yaml.safe_load(config)
    except yaml.YAMLError:
        logger.error("Failed to parse the downloader YAML configuration.")
        raise KaloscopeException(ErrorCode.INVALID_YAML_CONFIG) from None

    try:
        if not isinstance(yaml_config, dict):
            raise ValueError("Downloader configuration must be a mapping")

        driver = yaml_config.get("driver", "rpc")
        if not isinstance(driver, str) or driver not in _CONFIG_FACTORIES:
            raise ValueError(f"Unsupported downloader driver: {driver!r}")
        return _CONFIG_FACTORIES[driver].model_validate(yaml_config)
    except (ValueError, ValidationError):
        logger.error("Failed to validate the downloader configuration.")
        raise KaloscopeException(ErrorCode.INVALID_YAML_CONFIG) from None


def load_driver(config: str) -> RpcDriver | OpenListDriver:
    """Load the downloader driver selected by its configuration.

    Args:
        config: The downloader YAML configuration.

    Returns:
        The downloader driver created from the configuration.
    """
    downloader_config = load_config(config)
    return _DRIVER_FACTORIES[downloader_config.driver](downloader_config)
