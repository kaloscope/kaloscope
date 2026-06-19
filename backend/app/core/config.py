import os
import tomllib
from functools import cached_property
from pathlib import Path
from typing import Any, Self

from sanic import Sanic
from sanic.log import LOGGING_CONFIG_DEFAULTS


class KaloscopeConfig:
    """The configuration class for Kaloscope application."""

    _config: Self | None = None

    def __init__(self, toml: Path, root: Path | None = None):
        """Initializes the configuration class.

        Args:
            toml: The path to the configuration file.
            root: The root path to be used for formatting workspace paths.
        """
        with open(toml, "rb") as f:
            self.toml_config = tomllib.load(f)
        self._workspace = f"{str(root) if root else ''}/workspace"

    def _upper_key(self, obj: dict[str, Any]) -> dict[str, Any]:
        """Converts the keys of the dictionary to uppercase.

        Args:
            obj: The dictionary to convert.

        Returns:
            The dictionary with uppercase keys.
        """
        return {k.upper(): v for k, v in obj.items()}

    @cached_property
    def app(self) -> dict[str, Any]:
        return self._upper_key(self.toml_config["app"])

    @cached_property
    def workspace(self) -> dict[str, Any]:
        workspace = self.toml_config["workspace"]
        return self._upper_key(
            {
                k: (v.format(workspace=self._workspace) if isinstance(v, str) else v)
                for k, v in workspace.items()
            }
        )

    @cached_property
    def tortoise(self) -> dict[str, Any]:
        db_url = f"sqlite://{self.workspace['DATABASE']}/db.sqlite3"
        return {
            "connections": {"default": db_url},
            "apps": {
                "models": {
                    "models": ["app.models"],
                    "default_connection": "default",
                    "migrations": "app.migrations",
                },
            },
            "use_tz": True,
            "timezone": "UTC",
        }

    @cached_property
    def logging(self) -> dict[str, Any]:
        logging_config = LOGGING_CONFIG_DEFAULTS

        def attach_logger(qualname: str):
            logging_config["loggers"][qualname] = {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False,
            }

        # attach the third-party loggers
        attach_logger("tortoise")
        attach_logger("tortoise.db_client")
        attach_logger("httpx")
        attach_logger("httpcore")
        return logging_config

    @cached_property
    def script_strict_mode(self) -> bool:
        """Check if script strict mode is enabled.

        When enabled, Python-type ScriptNode nodes restrict builtins
        to prevent risky operations (e.g. __import__, open, eval, exec).

        Returns:
            True if script strict mode is enabled, False otherwise.
        """
        value = os.environ.get("SCRIPT_STRICT_MODE", "").lower()
        return value not in ("0", "no", "off", "false")

    @cached_property
    def filesystem_trash_mode(self) -> bool:
        """Check if filesystem trash mode is enabled.

        When enabled, filesystem deletes move paths to the OS trash
        instead of permanently deleting them.

        Returns:
            True if filesystem trash mode is enabled, False otherwise.
        """
        value = os.environ.get("FILESYSTEM_TRASH_MODE", "").lower()
        return value not in ("0", "no", "off", "false")

    def configure(self, app: Sanic):
        """Update the Sanic application configuration and store it in the context.

        Args:
            app: The Sanic application instance.
        """
        app.update_config(self.app)
        app.ctx.app_config = self

    @classmethod
    def get(cls) -> Self:
        """Get the configuration instance stored in the application context.

        Returns:
            The configuration instance.
        """
        if cls._config is None:
            config: Self = Sanic.get_app().ctx.app_config
            cls._config = config
        return cls._config

    @classmethod
    def get_workspace(cls, name: str) -> str:
        """Get a specific workspace path by name.

        Args:
            name: The name of the workspace.

        Returns:
            The path to the workspace.
        """
        return cls.get().workspace[name.upper()]
