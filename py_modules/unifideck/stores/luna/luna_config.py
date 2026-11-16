"""LunaConfig: typed view over the plugin config for Luna (profile dir,
sign-in / player URLs, stream hosts, spoof settings). Built via
``LunaConfig.from_config_manager``.

Mirrors ``microsoft_config.py`` but carries **no** OAuth keys: Luna signs
in with browser session cookies only, so there is no ``client_id``, no
authorize/token endpoint, no redirect URI, no scope and no token file
(Req 12.3). It also carries no games.map-specific launch-routing key —
launch routing is the ``browser`` tag plus ``metadata["browser_url"]`` in
``library_cache.json``. Instead it reads ``allowed_stream_hosts`` for the
``is_allowed_browser_url`` allow-list and resolves the browser-game CDP
flavour. The catalog endpoints are not configurable: they come from the
regions table in :mod:`unifideck.stores.luna.luna_api.regions`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from unifideck.auth.edge_browser.edge import WEB_GAME_PROFILE_DIR
from unifideck.stores.shared.config_reader import StoreConfigReader

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

logger = logging.getLogger(__name__)

_LUNA_CONFIG_PREFIX = "stores.luna"

#: The streaming-quality levels Luna's web client knows (its own
#: ``VideoQuality`` enum, read from the site bundle): LIMITED caps the
#: stream at 6-11 Mbit/s, STANDARD at 6-24 Mbit/s, BEST is uncapped.
LUNA_VIDEO_QUALITIES = ("LIMITED", "STANDARD", "BEST")

#: Config key of the user's chosen quality. Empty means "Auto": leave
#: whatever Luna itself has stored.
VIDEO_QUALITY_KEY = f"{_LUNA_CONFIG_PREFIX}.video_quality"


def normalize_video_quality(value: object) -> str:
    """Return ``value`` as a known quality level, or ``""`` (Auto)."""
    text = str(value or "").strip().upper()
    return text if text in LUNA_VIDEO_QUALITIES else ""


#: The spoofed User-Agent template must leave the browser version as a
#: runtime-substituted placeholder (Req 12.6, 14.8); a hardcoded version
#: lets Luna reject the client as outdated.
_VERSION_PLACEHOLDER = "{version}"


@dataclass(frozen=True)
class LunaConfig:
    """Luna config resolved from ``stores.luna`` in settings.json.

    All hosts, browser parameters and spoofing parameters live here so
    they can change without code changes (Req 12.1). No OAuth keys are
    read (Req 12.3).
    """

    luna_host: str = ""
    allowed_stream_hosts: list[str] = field(default_factory=list)
    signin_path: str = ""
    player_path: str = ""
    profile_dir: str | None = None
    browser_game_cdp_flavour: str = ""
    cloud_browser_flags: list[str] = field(default_factory=list)
    gamepad_flags: list[str] = field(default_factory=list)
    spoof_ua_template: str = ""
    spoof_uach_platform: str = ""
    spoof_navigator_platform: str = ""
    spoof_webgl_renderer: str = ""
    #: One of :data:`LUNA_VIDEO_QUALITIES`, or ``""`` for Auto (optional).
    video_quality: str = ""

    @classmethod
    def from_config_manager(cls, config: ConfigManager | None) -> LunaConfig:
        """Read the Luna config under ``stores.luna``.

        Reads no OAuth keys (Req 12.3) and no games.map launch-routing key.
        ``profile_dir`` defaults to the shared ``WEB_GAME_PROFILE_DIR`` when
        unset (Req 12.8).
        """
        cfg = StoreConfigReader(config, _LUNA_CONFIG_PREFIX)
        profile_dir = cfg.text("profile_dir") or WEB_GAME_PROFILE_DIR
        return cls(
            luna_host=cfg.text("luna_host"),
            allowed_stream_hosts=cfg.text_list("allowed_stream_hosts"),
            signin_path=cfg.text("signin_path"),
            player_path=cfg.text("player_path"),
            profile_dir=profile_dir,
            browser_game_cdp_flavour=cfg.text("browser_game_cdp_flavour"),
            cloud_browser_flags=cfg.text_list("cloud_browser_flags"),
            gamepad_flags=cfg.text_list("gamepad_flags"),
            spoof_ua_template=cfg.text("spoof_ua_template"),
            spoof_uach_platform=cfg.text("spoof_uach_platform"),
            spoof_navigator_platform=cfg.text("spoof_navigator_platform"),
            spoof_webgl_renderer=cfg.text("spoof_webgl_renderer"),
            video_quality=normalize_video_quality(cfg.text("video_quality")),
        )

    def is_valid(self) -> tuple[bool, list[str]]:
        """Whether every required key is set, and the names of those not.

        A key is invalid when it is missing, empty, or malformed for its
        type (Req 12.4, 12.5). ``spoof_ua_template`` is additionally
        malformed when it lacks the ``{version}`` placeholder, since a
        fixed version lets Luna reject the client as outdated (Req 12.6).
        """
        invalid: list[str] = []
        for name, text_val in self._required_text().items():
            if not text_val:
                invalid.append(name)
        for name, list_val in self._required_lists().items():
            if not list_val:
                invalid.append(name)
        if self.spoof_ua_template and _VERSION_PLACEHOLDER not in self.spoof_ua_template:
            invalid.append("spoof_ua_template")
        if invalid:
            logger.warning(
                "[LunaConfig] missing or invalid required keys: %s",
                ", ".join(invalid),
            )
        return (not invalid, invalid)

    def _required_text(self) -> dict[str, str]:
        return {
            "luna_host": self.luna_host,
            "signin_path": self.signin_path,
            "player_path": self.player_path,
            "profile_dir": self.profile_dir or "",
            "browser_game_cdp_flavour": self.browser_game_cdp_flavour,
            "spoof_ua_template": self.spoof_ua_template,
            "spoof_uach_platform": self.spoof_uach_platform,
            "spoof_navigator_platform": self.spoof_navigator_platform,
            "spoof_webgl_renderer": self.spoof_webgl_renderer,
        }

    def _required_lists(self) -> dict[str, list[str]]:
        return {"allowed_stream_hosts": self.allowed_stream_hosts}

    def describe(self) -> str:
        """Describe."""
        return (
            f"LunaConfig(luna_host={self.luna_host!r}, "
            f"profile_dir={self.profile_dir}, "
            f"browser_game_cdp_flavour={self.browser_game_cdp_flavour!r})"
        )
