"""GOG store configuration — frozen dataclass with deferred path resolution.

``GOGConfig`` is a frozen dataclass holding every tunable parameter
of the GOG sub-package: download directory, gogdl binary path,
OAuth URLs, token file location, gogdl config directory, etc.

The class exposes two kinds of fields:

* **Raw fields** (e.g. ``download_dir``, ``token_file``) — strings as
  configured, may contain ``~``.
* **Expanded properties** (e.g. ``download_dir_expanded``) — same value
  with ``~`` resolved at access time. We defer expansion to property
  access so that a user changing ``$HOME`` mid-session sees the new
  value.

Configuration is loaded via ``from_config_manager(config)`` which reads
the ``stores.gog.*`` namespace of the user config, falling back to the
hard-coded defaults if a key is missing or malformed.

The dataclass is intentionally ``frozen=True`` — any mutation must go
through a new ``GOGConfig`` instance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from unifideck.stores.shared.config_reader import StoreConfigReader

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
logger = logging.getLogger(__name__)
_GOG_CONFIG_PREFIX = "stores.gog"
_DEFAULT_TOKEN_FILE = "~/.config/unifideck/gog_token.json"  # file path, not a token value  # noqa: S105 — filename constant, not a credential
_DEFAULT_GOGDL_CONFIG_DIR = "~/.config/unifideck/gogdl"
_DEFAULT_DOWNLOAD_DIR = "~/GOG Games"
GOG_AUTH_URL_FILE = "~/.local/share/unifideck/gog_auth_url.txt"

@dataclass(frozen=True)
class GOGConfig:
    """Gogconfig."""

    client_id: str = ""
    client_secret: str = ""
    auth_url: str = ""
    token_url: str = ""
    redirect_uri: str = ""
    allowed_redirect_uris: list[str] = field(default_factory=list)
    base_url: str = ""
    api_gog_url: str = ""
    token_file: str = _DEFAULT_TOKEN_FILE
    gogdl_config_dir: str = _DEFAULT_GOGDL_CONFIG_DIR
    download_dir: str = _DEFAULT_DOWNLOAD_DIR
    token_refresh_threshold_seconds: int = 2400
    supported_languages: list[str] = field(
        default_factory=lambda: [
            "en",
            "de",
            "fr",
            "pl",
            "ru",
            "pt",
            "es",
            "it",
            "zh",
            "ko",
            "ja",
        ],
    )
    user_agent: str = "Unifideck/1.0"

    @classmethod
    def from_config_manager(cls, config: ConfigManager | None) -> GOGConfig:
        """From config manager."""

        cfg = StoreConfigReader(config, _GOG_CONFIG_PREFIX)

        primary_redirect = cfg.text("redirect_uri")
        allowed = cfg.text_list("allowed_redirect_uris")
        if not allowed and primary_redirect:
            allowed = [primary_redirect]
        supported = cfg.text_list("supported_languages")
        if not supported:
            supported = [
                "en",
                "de",
                "fr",
                "pl",
                "ru",
                "pt",
                "es",
                "it",
                "zh",
                "ko",
                "ja",
            ]
        return cls(
            client_id=cfg.text("client_id"),
            client_secret=cfg.text("client_secret"),
            auth_url=cfg.text("auth_url"),
            token_url=cfg.text("token_url"),
            redirect_uri=primary_redirect,
            allowed_redirect_uris=allowed,
            base_url=cfg.text("base_url"),
            api_gog_url=cfg.text("api_gog_url"),
            token_file=cfg.text("token_file", _DEFAULT_TOKEN_FILE),
            gogdl_config_dir=cfg.text(
                "gogdl_config_dir",
                _DEFAULT_GOGDL_CONFIG_DIR,
            ),
            download_dir=cfg.text("download_dir", _DEFAULT_DOWNLOAD_DIR),
            token_refresh_threshold_seconds=cfg.number(
                "token_refresh_threshold_seconds",
                2400,
            ),
            supported_languages=supported,
            user_agent=cfg.text("user_agent", "Unifideck/1.0"),
        )

    def is_valid(self) -> bool:
        """Check whether valid."""
        required = (
            ("client_id", self.client_id),
            ("client_secret", self.client_secret),
            ("auth_url", self.auth_url),
            ("token_url", self.token_url),
            ("redirect_uri", self.redirect_uri),
            ("base_url", self.base_url),
            ("api_gog_url", self.api_gog_url),
        )
        missing = [name for name, val in required if not val]
        if missing:
            logger.warning(
                "[GOGConfig] missing required keys: %s",
                ", ".join(missing),
            )
            return False
        return True

    @property
    def auth_config_path(self) -> str:
        """Auth config path."""

        return str(Path(str(Path(self.gogdl_config_dir).expanduser())) / "gog_credentials.json")

    def describe(self) -> str:
        """Describe."""
        return (
            f"GOGConfig(client_id={self.client_id[:6]}…, "
            f"base_url={self.base_url}, "
            f"token_file={self.token_file}, "
            f"download_dir={self.download_dir})"
        )
