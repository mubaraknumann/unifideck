from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from unifideck.stores.shared.config_reader import StoreConfigReader

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)
_MS_CONFIG_PREFIX = "stores.microsoft"
_DEFAULT_TOKEN_FILE = "~/.config/unifideck/microsoft_tokens.json"  # file path, not a token value  # noqa: S105 — filename constant, not a credential
#: Sign-in flows. ``device_code`` is xbox.com's own client (a code shown in
#: the Edge window, confirmed by the user); ``browser_redirect`` is the
#: login.live.com client Unifideck used up to 0.7.5, kept as the rollback.
AUTH_FLOW_DEVICE_CODE = "device_code"
AUTH_FLOW_BROWSER_REDIRECT = "browser_redirect"
_AUTH_FLOWS = (AUTH_FLOW_DEVICE_CODE, AUTH_FLOW_BROWSER_REDIRECT)
_LEGACY_PREFIX = "legacy_browser."


@dataclass(frozen=True)
class MicrosoftConfig:
    """Microsoft config, resolved for the active sign-in flow.

    ``client_id``, ``scope``, ``token_url`` and the browser fields always
    describe the client the active flow signs in with:
    ``from_config_manager`` reads them from the top level for
    ``device_code`` and from ``legacy_browser`` for ``browser_redirect``.
    Every reader (refresh, persistence, the browser flow) therefore uses
    the right client without knowing which flow is on. Switching back is
    one config key: ``stores.microsoft.auth_flow``.
    """
    auth_flow: str = AUTH_FLOW_BROWSER_REDIRECT
    client_id: str = ""
    scope: str = ""
    auth_url: str = ""
    token_url: str = ""
    devicecode_url: str = ""
    redirect_uri: str = ""
    allowed_redirect_uris: list[str] = field(default_factory=list)
    xbl_auth_url: str = ""
    xsts_url: str = ""
    xcloud_catalog_url: str = ""
    xcloud_titles_url: str = ""
    xcloud_launch_url: str = ""
    gssv_relying_party: str = "http://gssv.xboxlive.com/"
    marketplace_relying_party: str = "http://mp.microsoft.com/"
    subscription_check_url: str = (
    "https://xgpuweb.gssv-play-prod.xboxlive.com/v2/login/user"
    )
    token_file: str = _DEFAULT_TOKEN_FILE
    token_refresh_threshold_seconds: int = 2400
    xbl_user_agent: str = "XboxReplay; XboxLiveAuth/3.0"
    catalog_user_agent: str = "Unifideck/1.0"

    @property
    def uses_device_code(self) -> bool:
        """True when signing in with xbox.com's client (device code)."""
        return self.auth_flow == AUTH_FLOW_DEVICE_CODE

    @classmethod
    def from_config_manager(cls, config: ConfigManager | None) -> MicrosoftConfig:
        """From config manager."""
        cfg = StoreConfigReader(config, _MS_CONFIG_PREFIX)
        flow = cfg.text("auth_flow", AUTH_FLOW_BROWSER_REDIRECT)
        if flow not in _AUTH_FLOWS:
            logger.warning(
                "[MicrosoftConfig] unknown auth_flow %r — using %s",
                flow, AUTH_FLOW_BROWSER_REDIRECT,
            )
            flow = AUTH_FLOW_BROWSER_REDIRECT
        client = _client_keys(cfg, flow)
        return cls(
            auth_flow=flow,
            **client,
            xbl_auth_url=cfg.text("xbl_auth_url"),
            xsts_url=cfg.text("xsts_url"),
            xcloud_catalog_url=cfg.text("xcloud_catalog_url"),
            xcloud_titles_url=cfg.text("xcloud_titles_url"),
            xcloud_launch_url=cfg.text("xcloud_launch_url"),
            gssv_relying_party=cfg.text(
                "gssv_relying_party", "http://gssv.xboxlive.com/",
            ),
            marketplace_relying_party=cfg.text(
                "marketplace_relying_party", "http://mp.microsoft.com/",
            ),
            subscription_check_url=cfg.text(
                "subscription_check_url",
                "https://xgpuweb.gssv-play-prod.xboxlive.com/v2/login/user",
            ),
            token_file=cfg.text("token_file", _DEFAULT_TOKEN_FILE),
            token_refresh_threshold_seconds=cfg.number(
                "token_refresh_threshold_seconds", 2400,
            ),
            xbl_user_agent=cfg.text(
                "xbl_user_agent",
                "XboxReplay; XboxLiveAuth/3.0",
            ),
            catalog_user_agent=cfg.text(
                "catalog_user_agent", "Unifideck/1.0",
            ),
        )

    def _required(self) -> dict[str, str]:
        common = {
            "client_id": self.client_id,
            "scope": self.scope,
            "token_url": self.token_url,
            "xbl_auth_url": self.xbl_auth_url,
            "xsts_url": self.xsts_url,
            "xcloud_catalog_url": self.xcloud_catalog_url,
            "xcloud_launch_url": self.xcloud_launch_url,
        }
        if self.uses_device_code:
            return {**common, "devicecode_url": self.devicecode_url}
        return {
            **common,
            "auth_url": self.auth_url,
            "redirect_uri": self.redirect_uri,
        }

    def is_valid(self) -> bool:
        """Check whether the keys the active sign-in flow needs are set."""
        missing = [name for name, val in self._required().items() if not val]
        if missing:
            logger.warning(
                "[MicrosoftConfig] missing required keys for %s: %s",
                self.auth_flow, ", ".join(missing),
            )
            return False
        return True
    def describe(self) -> str:
        """Describe."""
        return (
            f"MicrosoftConfig(auth_flow={self.auth_flow}, "
            f"client_id={self.client_id[:6]}…, "
            f"scope={self.scope!r}, "
            f"token_file={self.token_file})"
        )


def _client_keys(cfg: StoreConfigReader, flow: str) -> dict[str, Any]:
    """The OAuth client fields for ``flow``: top level, or ``legacy_browser``."""
    if flow == AUTH_FLOW_DEVICE_CODE:
        return {
            "client_id": cfg.text("client_id"),
            "scope": cfg.text("scope"),
            "token_url": cfg.text("token_url"),
            "devicecode_url": cfg.text("devicecode_url"),
        }
    primary_redirect = cfg.text(_LEGACY_PREFIX + "redirect_uri")
    allowed = cfg.text_list(_LEGACY_PREFIX + "allowed_redirect_uris")
    if not allowed and primary_redirect:
        allowed = [primary_redirect]
    return {
        "client_id": cfg.text(_LEGACY_PREFIX + "client_id"),
        "scope": cfg.text(_LEGACY_PREFIX + "scope"),
        "auth_url": cfg.text(_LEGACY_PREFIX + "auth_url"),
        "token_url": cfg.text(_LEGACY_PREFIX + "token_url"),
        "redirect_uri": primary_redirect,
        "allowed_redirect_uris": allowed,
    }
