from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import KaloscopeConfig
from app.core.dl import config as config_module
from app.core.dl.config import load_config
from app.core.dl.openlist.driver import OpenListDriver
from app.core.dl.openlist.models import OpenListAuth, OpenListConfig
from app.core.dl.rpc import RpcConfig, RpcDriver

PRESET_DIR = Path(__file__).parents[1] / "static" / "downloaders"
CONFIG_PATH = Path(__file__).parents[1] / "app" / "config.toml"


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    monkeypatch.setattr(KaloscopeConfig, "_config", KaloscopeConfig(CONFIG_PATH))


@pytest.mark.parametrize(
    ("filename", "name"),
    [
        ("aria2.yaml", "aria2"),
        ("qBittorrent.yaml", "qBittorrent"),
        ("Transmission.yaml", "Transmission"),
    ],
)
def test_legacy_presets(filename: str, name: str):
    config = load_config((PRESET_DIR / filename).read_text())

    assert config.driver == "rpc"
    assert config.name == name


def test_rpc_loader():
    yaml_config = """driver: rpc
name: test
host: 127.0.0.1
port: 8080
path: /api/
methods: {}
"""

    config = load_config(yaml_config)
    driver = config_module.load_driver(yaml_config)

    assert type(config) is RpcConfig
    assert config.base_url == "http://127.0.0.1:8080/api"
    assert type(driver) is RpcDriver
    assert driver.config == config


def test_openlist_loader():
    yaml_config = """driver: openlist
protocol: https
host: OpenList.Example.COM
port: 443
path: /openlist/api/
auth:
  token: private-token
tool: Future Tool
"""

    config = load_config(yaml_config)
    driver = config_module.load_driver(yaml_config)

    assert type(config) is OpenListConfig
    assert config.tool == "Future Tool"
    assert config.base_url == "https://openlist.example.com/openlist/api"
    assert config.remote_root == "/Kaloscope"
    assert config.remote_cleanup.value == "keep"
    assert (config.poll_interval, config.poll_max_interval) == (10, 60)
    with pytest.raises(ValueError):
        OpenListConfig(
            host="openlist.example.com",
            port=5244,
            auth=OpenListAuth(token=SecretStr("private-token")),
            tool="Future Tool",
            remote_root="/safe/../escape",
        )
    assert isinstance(driver, OpenListDriver)
    assert driver.config == config


def test_openlist_api_path():
    config = OpenListConfig(
        host="openlist.example.com",
        port=5244,
        auth=OpenListAuth(token=SecretStr("private-token")),
        tool="Future Tool",
    )

    assert config.base_url == "http://openlist.example.com:5244/api"
    with pytest.raises(ValueError):
        OpenListConfig(
            host="openlist.example.com",
            port=5244,
            path="/openlist",
            auth=OpenListAuth(token=SecretStr("private-token")),
            tool="Future Tool",
        )
