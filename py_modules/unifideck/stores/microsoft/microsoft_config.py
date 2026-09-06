from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from unifideck.stores.shared.config_reader import StoreConfigReader

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)
_MS_CONFIG_PREFIX = "stores.microsoft"
_DEFAULT_TOKEN_FILE = "~/.config/unifideck/microsoft_tokens.json"  # file path, not a token value  # noqa: S105 — filename constant, not a credential
@dataclass(frozen=True)
class MicrosoftConfig:
    """Microsoft config."""
    client_id: str = ""
    scope: str = ""
    auth_url: str = ""
    token_url: str = ""
    redirect_uri: str = ""
    allowed_redirect_uris: list[str] = field(default_factory=list)
    xbl_auth_url: str = ""
    xsts_url: str = ""
    xcloud_catalog_url: str = ""
    xcloud_titles_url: str = ""
    xcloud_launch_url: str = ""
    gssv_relying_party: str = "http://gssv.xboxlive.com/"
    subscription_check_url: str = (
    "https://xgpuweb.gssv-play-prod.xboxlive.com/v2/login/user"
    )
    token_file: str = _DEFAULT_TOKEN_FILE
    token_refresh_threshold_seconds: int = 2400
    xbl_user_agent: str = "XboxReplay; XboxLiveAuth/3.0"
    catalog_user_agent: str = "Unifideck/1.0"

    @classmethod
    def from_config_manager(cls, config: ConfigManager | None) -> MicrosoftConfig:
        """From config manager."""
        cfg = StoreConfigReader(config, _MS_CONFIG_PREFIX)
        primary_redirect = cfg.text("redirect_uri")
        allowed = cfg.text_list("allowed_redirect_uris")
        if not allowed and primary_redirect:
            allowed = [primary_redirect]
        return cls(
            client_id=cfg.text("client_id"),
            scope=cfg.text("scope"),
            auth_url=cfg.text("auth_url"),
            token_url=cfg.text("token_url"),
            redirect_uri=primary_redirect,
            allowed_redirect_uris=allowed,
            xbl_auth_url=cfg.text("xbl_auth_url"),
            xsts_url=cfg.text("xsts_url"),
            xcloud_catalog_url=cfg.text("xcloud_catalog_url"),
            xcloud_titles_url=cfg.text("xcloud_titles_url"),
            xcloud_launch_url=cfg.text("xcloud_launch_url"),
            gssv_relying_party=cfg.text(
                "gssv_relying_party", "http://gssv.xboxlive.com/",
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

    def is_valid(self) -> bool:

        """Check whether valid."""
        required = (
            self.client_id,
            self.scope,
            self.auth_url,
            self.token_url,
            self.redirect_uri,
            self.xbl_auth_url,
            self.xsts_url,
            self.xcloud_catalog_url,
            self.xcloud_launch_url,
        )
        missing = [
            name for name, val in zip(
                (
                    "client_id", "scope", "auth_url", "token_url",
                    "redirect_uri", "xbl_auth_url", "xsts_url",
                    "xcloud_catalog_url", "xcloud_launch_url",
                ),
                required, strict=False,
            )
            if not val
        ]
        if missing:
            logger.warning(
                "[MicrosoftConfig] missing required keys: %s",
                ", ".join(missing),
            )
            return False
        return True
    def describe(self) -> str:
        """Describe."""
        return (
            f"MicrosoftConfig(client_id={self.client_id[:6]}…, "
            f"scope={self.scope!r}, "
            f"token_file={self.token_file})"
        )
