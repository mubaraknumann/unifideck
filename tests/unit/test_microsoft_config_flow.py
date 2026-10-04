"""The Microsoft config resolves one OAuth client per sign-in flow.

0.7.6 adds xbox.com's own client (device code) next to the login.live.com
client every earlier version used. Both live in ``defaults/config.json``;
``stores.microsoft.auth_flow`` picks one, so rolling back is a single key.
``MicrosoftConfig`` always carries the active client's fields, which is
what lets the refresh, persistence and browser code stay flow-agnostic.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.unit._repo_root import find_repo_file
from unifideck.config import ConfigManager
from unifideck.stores.microsoft.microsoft_config import (
    AUTH_FLOW_BROWSER_REDIRECT,
    AUTH_FLOW_DEVICE_CODE,
    MicrosoftConfig,
)

XBOX_COM_CLIENT = "1f907974-e22b-4810-a9de-d9647380c97e"
LIVE_CLIENT = "000000004C12AE6F"


def _config(tmp_path: Path, override: dict[str, Any] | None = None) -> MicrosoftConfig:
    defaults = find_repo_file("defaults/config.json")
    if defaults is None:
        pytest.skip("defaults/config.json not found (set UNIFIDECK_REPO_ROOT)")
    user = tmp_path / "user.json"
    if override is not None:
        user.write_text(json.dumps({"stores": {"microsoft": override}}))
    manager = ConfigManager(defaults_path=str(defaults), user_path=str(user))
    return MicrosoftConfig.from_config_manager(manager)


def test_browser_redirect_uses_the_live_client(tmp_path: Path) -> None:
    cfg = _config(tmp_path, {"auth_flow": AUTH_FLOW_BROWSER_REDIRECT})
    assert cfg.client_id == LIVE_CLIENT
    assert cfg.token_url == "https://login.live.com/oauth20_token.srf"
    assert cfg.redirect_uri and cfg.auth_url and cfg.allowed_redirect_uris
    assert cfg.is_valid()


def test_device_code_uses_the_xbox_com_client(tmp_path: Path) -> None:
    cfg = _config(tmp_path, {"auth_flow": AUTH_FLOW_DEVICE_CODE})
    assert cfg.client_id == XBOX_COM_CLIENT
    assert cfg.scope == "XboxLive.signin offline_access"
    assert cfg.devicecode_url.endswith("/consumers/oauth2/v2.0/devicecode")
    assert cfg.redirect_uri == ""
    assert cfg.uses_device_code
    assert cfg.is_valid()


def test_unknown_flow_falls_back_to_browser_redirect(tmp_path: Path) -> None:
    cfg = _config(tmp_path, {"auth_flow": "browser_redirect"})
    bogus = MicrosoftConfig(auth_flow="nonsense")
    assert not bogus.uses_device_code
    assert cfg.auth_flow == AUTH_FLOW_BROWSER_REDIRECT


def test_device_code_is_invalid_without_its_endpoint() -> None:
    cfg = MicrosoftConfig(
        auth_flow=AUTH_FLOW_DEVICE_CODE, client_id="c", scope="s",
        token_url="https://t", xbl_auth_url="https://x", xsts_url="https://y",
        xcloud_catalog_url="https://z", xcloud_launch_url="https://w",
    )
    assert not cfg.is_valid()


def test_marketplace_relying_party_is_configured(tmp_path: Path) -> None:
    assert _config(tmp_path).marketplace_relying_party == "http://mp.microsoft.com/"
