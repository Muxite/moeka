import json
import socket
from unittest.mock import patch

from nanobot.config.loader import load_config, save_config
from nanobot.security.network import validate_url_target


def _fake_resolve(host: str, results: list[str]):
    """Return a getaddrinfo mock that maps the given host to fake IP results."""
    def _resolver(hostname, port, family=0, type_=0):
        if hostname == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in results]
        raise socket.gaierror(f"cannot resolve {hostname}")
    return _resolver


def test_load_config_migrates_legacy_my_tool_keys(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "myEnabled": False,
                    "mySet": True,
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.tools.my.enable is False
    assert config.tools.my.allow_set is True


def test_save_config_rewrites_legacy_my_tool_keys(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "myEnabled": False,
                    "mySet": True,
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config, config_path)
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    tools = saved["tools"]
    assert "myEnabled" not in tools
    assert "mySet" not in tools
    assert tools["my"] == {"enable": False, "allowSet": True}


def test_new_my_tool_keys_take_precedence_over_legacy(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "myEnabled": False,
                    "mySet": False,
                    "my": {"enable": True, "allowSet": True},
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.tools.my.enable is True
    assert config.tools.my.allow_set is True


def test_load_config_resets_ssrf_whitelist_when_next_config_is_empty(tmp_path) -> None:
    whitelisted = tmp_path / "whitelisted.json"
    whitelisted.write_text(
        json.dumps({"tools": {"ssrfWhitelist": ["100.64.0.0/10"]}}),
        encoding="utf-8",
    )
    defaulted = tmp_path / "defaulted.json"
    defaulted.write_text(json.dumps({}), encoding="utf-8")

    load_config(whitelisted)
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
        ok, err = validate_url_target("http://ts.local/api")
        assert ok, err

    load_config(defaulted)
    with patch("nanobot.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
        ok, _ = validate_url_target("http://ts.local/api")
        assert not ok


def test_load_config_defaults_local_service_access_to_enabled(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"tools": {}}), encoding="utf-8")

    config = load_config(config_path)

    assert config.tools.webui_allow_local_service_access is True


def test_load_config_accepts_legacy_local_preview_access(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"tools": {"allowLocalPreviewAccess": False}}),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.tools.webui_allow_local_service_access is False


def test_load_config_ignores_retired_remote_package_install_key(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"tools": {"webuiAllowRemotePackageInstall": True}}),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert not hasattr(config.tools, "webui_allow_remote_package_install")
