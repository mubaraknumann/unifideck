"""Unit tests for ``LunaConfig`` validation and ``from_config_manager`` (task 1.3).

Luna signs in with browser session cookies only, so ``LunaConfig`` carries
**no** OAuth keys (Req 12.3). ``from_config_manager`` reads under the
``stores.luna`` prefix, defaults the profile to the shared
``WEB_GAME_PROFILE_DIR`` when unset (Req 12.8), and ``is_valid()`` names every
missing / empty / malformed required key without attempting any operation
(Req 12.4, 12.5). The spoofed User-Agent template is malformed unless it keeps
the ``{version}`` placeholder (Req 12.6).
"""
from __future__ import annotations

from typing import Any

import pytest

from unifideck.auth.edge_browser.edge import WEB_GAME_PROFILE_DIR
from unifideck.stores.luna.luna_config import LunaConfig

# OAuth keys that Luna must never read (Req 12.3). ``from_config_manager``
# with a config carrying these must still produce a config that references
# none of them and reads no such attribute.
_OAUTH_KEYS = (
    "client_id",
    "authorize_url",
    "auth_url",
    "token_url",
    "redirect_uri",
    "scope",
    "token_file",
)

# A fully-populated, valid ``stores.luna`` section.
_VALID: dict[str, Any] = {
    "luna_host": "luna.amazon.de",
    "allowed_stream_hosts": ["luna.amazon.de", "luna.amazon.com"],
    "signin_path": "/signin",
    "player_path": "/player",
    "profile_dir": "/home/deck/luna-profile",
    "browser_game_cdp_flavour": "edge",
    "cloud_browser_flags": ["--kiosk"],
    "gamepad_flags": ["--enable-gamepad"],
    "spoof_ua_template": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/{version}",
    "spoof_uach_platform": "Windows",
    "spoof_navigator_platform": "Win32",
    "spoof_webgl_renderer": "ANGLE (NVIDIA)",
}

# Required string keys (every one must be named when missing/empty).
# ``luna_host`` is not among them: empty means "auto" and resolves to the
# signed-in region (or the sign-in fallback host), see the auto tests below.
_REQUIRED_TEXT_KEYS = (
    "signin_path",
    "player_path",
    "browser_game_cdp_flavour",
    "spoof_ua_template",
    "spoof_uach_platform",
    "spoof_navigator_platform",
    "spoof_webgl_renderer",
)

# Required list keys.
_REQUIRED_LIST_KEYS = (
    "allowed_stream_hosts",
)


class _Cfg:
    """The slice of ``ConfigManager`` that ``from_config_manager`` touches."""

    def __init__(self, section: dict[str, Any]) -> None:
        # ``StoreConfigReader`` reads ``stores.luna.<key>`` through ``.get``.
        self._values = {f"stores.luna.{k}": v for k, v in section.items()}

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


def _config(section: dict[str, Any]) -> LunaConfig:
    return LunaConfig.from_config_manager(_Cfg(section))


@pytest.fixture(autouse=True)
def _no_signed_in_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic: no real browser profile is read for region detection."""
    import unifideck.stores.luna.luna_api.regions as regions

    monkeypatch.setattr(regions, "detect_region", lambda _profile: None)


# ── auto host + language ────────────────────────────────────────
def test_empty_host_before_sign_in_uses_the_geo_redirect_fallback() -> None:
    cfg = _config({**_VALID, "luna_host": ""})
    assert cfg.luna_host == "https://luna.amazon.com"
    assert cfg.is_valid()[0]


def test_empty_host_uses_the_signed_in_region(monkeypatch: pytest.MonkeyPatch) -> None:
    import unifideck.stores.luna.luna_api.regions as regions

    fr = regions.resolve_region("luna.amazon.fr")
    monkeypatch.setattr(regions, "detect_region", lambda _profile: fr)
    cfg = _config({**_VALID, "luna_host": ""})
    assert cfg.luna_host == "https://luna.amazon.fr"


def test_configured_host_wins_over_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    import unifideck.stores.luna.luna_api.regions as regions

    monkeypatch.setattr(
        regions, "detect_region", lambda _p: pytest.fail("no detection when set"),
    )
    assert _config(_VALID).luna_host == "luna.amazon.de"


def test_language_follows_the_unifideck_ui_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    import unifideck.utils.locale as ui_locale

    monkeypatch.setattr(ui_locale, "get_unifideck_locale", lambda _c: "de-DE")
    assert _config(_VALID).locale == "de_DE"
    monkeypatch.setattr(ui_locale, "get_unifideck_locale", lambda _c: "ja-JP")
    # Not offered by Luna: the country site's own default.
    assert _config(_VALID).locale == "de_DE"
    monkeypatch.setattr(ui_locale, "get_unifideck_locale", lambda _c: "en-US")
    assert _config(_VALID).locale == "en_US"


# ── valid config ────────────────────────────────────────────────
def test_removed_catalog_keys_are_not_read() -> None:
    """The catalog endpoints come from the regions table, not from config:
    leftover keys from an older settings.json are ignored, not required."""
    section = {
        **_VALID,
        "catalog_endpoint": "/catalog",
        "subscription_endpoint": "/subscription",
        "detail_url_pattern": "{luna_host}/game/{product_id}",
        "browser_candidates": ["com.google.Chrome"],
        "flatpak_override_paths": ["/run/udev:ro"],
    }
    cfg = _config(section)
    ok, invalid = cfg.is_valid()
    assert ok, invalid
    for key in ("catalog_endpoint", "subscription_endpoint", "detail_url_pattern",
                "browser_candidates", "flatpak_override_paths"):
        assert not hasattr(cfg, key)


def test_valid_config_round_trips_every_field() -> None:
    cfg = _config(_VALID)
    ok, invalid = cfg.is_valid()
    assert ok
    assert invalid == []
    assert cfg.luna_host == "luna.amazon.de"
    assert cfg.allowed_stream_hosts == ["luna.amazon.de", "luna.amazon.com"]
    assert cfg.gamepad_flags == ["--enable-gamepad"]
    assert cfg.profile_dir == "/home/deck/luna-profile"
    assert cfg.spoof_uach_platform == "Windows"


# ── defaults ────────────────────────────────────────────────────
def test_profile_dir_defaults_to_shared_web_game_profile_when_unset() -> None:
    """Req 12.8: an unset profile dir falls back to ``WEB_GAME_PROFILE_DIR``."""
    section = {k: v for k, v in _VALID.items() if k != "profile_dir"}
    cfg = _config(section)
    assert cfg.profile_dir == WEB_GAME_PROFILE_DIR


def test_empty_profile_dir_also_defaults_to_shared_profile() -> None:
    cfg = _config({**_VALID, "profile_dir": ""})
    assert cfg.profile_dir == WEB_GAME_PROFILE_DIR


def test_missing_section_yields_empty_strings_and_lists() -> None:
    """A store must construct even with no ``stores.luna`` section at all."""
    cfg = LunaConfig.from_config_manager(None)
    # No host configured and nobody signed in: the geo-redirect fallback.
    assert cfg.luna_host == "https://luna.amazon.com"
    assert cfg.allowed_stream_hosts == []
    assert cfg.gamepad_flags == []
    # The profile still defaults rather than staying unset.
    assert cfg.profile_dir == WEB_GAME_PROFILE_DIR


def test_defaults_construct_an_invalid_config_naming_every_required_key() -> None:
    cfg = LunaConfig()
    ok, invalid = cfg.is_valid()
    assert not ok
    for key in (*_REQUIRED_TEXT_KEYS, *_REQUIRED_LIST_KEYS):
        assert key in invalid


# ── missing / empty required keys (one example per key) ─────────
@pytest.mark.parametrize("missing", _REQUIRED_TEXT_KEYS)
def test_each_missing_required_text_key_is_named(missing: str) -> None:
    """Req 12.4: a missing required key is reported by name, no operation run."""
    section = {k: v for k, v in _VALID.items() if k != missing}
    ok, invalid = _config(section).is_valid()
    assert not ok
    assert missing in invalid


@pytest.mark.parametrize("empty", _REQUIRED_TEXT_KEYS)
def test_each_empty_required_text_key_is_named(empty: str) -> None:
    """Req 12.5: a present-but-empty required key is reported by name."""
    ok, invalid = _config({**_VALID, empty: ""}).is_valid()
    assert not ok
    assert empty in invalid


@pytest.mark.parametrize("missing", _REQUIRED_LIST_KEYS)
def test_each_missing_required_list_key_is_named(missing: str) -> None:
    section = {k: v for k, v in _VALID.items() if k != missing}
    ok, invalid = _config(section).is_valid()
    assert not ok
    assert missing in invalid


@pytest.mark.parametrize("empty", _REQUIRED_LIST_KEYS)
def test_each_empty_required_list_key_is_named(empty: str) -> None:
    ok, invalid = _config({**_VALID, empty: []}).is_valid()
    assert not ok
    assert empty in invalid


# ── malformed UA template (Req 12.6) ────────────────────────────
def test_ua_template_without_version_placeholder_is_malformed() -> None:
    """Req 12.6: a hardcoded browser version lets Luna reject the client."""
    bad = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
    ok, invalid = _config({**_VALID, "spoof_ua_template": bad}).is_valid()
    assert not ok
    assert "spoof_ua_template" in invalid


def test_ua_template_with_version_placeholder_is_valid() -> None:
    tmpl = "Mozilla/5.0 (Windows NT 10.0) Chrome/{version} Safari/537.36"
    ok, invalid = _config({**_VALID, "spoof_ua_template": tmpl}).is_valid()
    assert ok
    assert "spoof_ua_template" not in invalid


def test_only_the_offending_key_is_reported_when_otherwise_valid() -> None:
    """An otherwise-valid config names exactly the one bad key, nothing else."""
    ok, invalid = _config({**_VALID, "player_path": ""}).is_valid()
    assert not ok
    assert invalid == ["player_path"]


# ── no OAuth key is ever read (Req 12.3) ────────────────────────
def test_no_oauth_field_exists_on_the_dataclass() -> None:
    cfg = LunaConfig()
    for key in _OAUTH_KEYS:
        assert not hasattr(cfg, key)


def test_oauth_keys_in_config_are_ignored_and_do_not_make_it_valid() -> None:
    """Req 12.3: supplying OAuth keys neither populates fields nor rescues
    an otherwise-missing required Luna key."""
    section = {k: v for k, v in _VALID.items() if k != "signin_path"}
    for key in _OAUTH_KEYS:
        section[key] = "SHOULD-BE-IGNORED"
    cfg = _config(section)
    for key in _OAUTH_KEYS:
        assert not hasattr(cfg, key)
    ok, invalid = cfg.is_valid()
    assert not ok
    assert "signin_path" in invalid


# ── describe() ──────────────────────────────────────────────────
def test_describe_summarises_the_key_identifying_fields() -> None:
    text = _config(_VALID).describe()
    assert text.startswith("LunaConfig(")
    assert "luna.amazon.de" in text
    assert "/home/deck/luna-profile" in text
    assert "edge" in text


def test_describe_on_defaults_shows_the_shared_profile_dir() -> None:
    text = LunaConfig.from_config_manager(None).describe()
    assert WEB_GAME_PROFILE_DIR in text
