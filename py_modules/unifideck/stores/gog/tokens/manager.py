"""GOG token manager — orchestration facade.

``GOGTokenManager`` is the public token API for the GOG store.
Responsibilities:

* lazy-load tokens from encrypted on-disk storage at construction or
  on first access (``has_tokens``);
* refresh tokens when stale (``refresh_if_stale``) — delegates to the
  OAuth sub-module (``oauth.py``);
* persist updated tokens after every successful refresh
  (``storage.py``);
* provide a temporary gogdl-credentials directory for subprocess calls
  (``gogdl_credentials.py``) which gogdl reads instead of
  Unifideck's encrypted store;
* expose the authenticated user info (``GOGUserInfo``) for the
  UI.

Decoupling the three sub-modules from this facade keeps the storage
encryption, the OAuth protocol, and the gogdl mirror independently
testable.
"""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.security import SecureTokenStore

from .gogdl_credentials import _GogdlCreds
from .oauth import ExchangeOutcome, _TokenOAuth
from .storage import _TokenStorage
from .user_info import GOGUserInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from unifideck.stores.gog.config import GOGConfig
logger = logging.getLogger(__name__)

class GOGTokenManager:
    """Gogtoken manager."""

    def __init__(
        self,
        config: GOGConfig,
        secure_store: SecureTokenStore | None = None,
        bus: Any = None,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._bus = bus
        self._secure_store = secure_store or SecureTokenStore(
            bus=bus,
        )
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._user_info = GOGUserInfo()
        self._storage = _TokenStorage(
            config=config,
            bus=bus,
            secure_store=self._secure_store,
        )
        self._oauth = _TokenOAuth(
            config=config,
            save_callback=self.save,
        )
        self._gogdl = _GogdlCreds(config=config)

    @property
    def access_token(self) -> str | None:
        """Access token."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Refresh token."""
        return self._refresh_token

    @property
    def user_info(self) -> GOGUserInfo:
        """User info."""
        return self._user_info

    @property
    def has_tokens(self) -> bool:
        """Check whether tokens."""
        return bool(
            self._access_token and self._refresh_token,
        )

    def get_token_age_seconds(self) -> float:
        """Get token age seconds."""
        path = str(Path(self._config.token_file).expanduser())
        if not Path(path).is_file():
            return float("inf")
        try:
            return time.time() - Path(path).stat().st_mtime
        except OSError:
            return float("inf")

    async def load(self) -> bool:
        """Load."""
        result = await self._storage.load()
        if result is None:
            return False
        access, refresh, user_info = result
        self._access_token = access
        self._refresh_token = refresh
        self._user_info = user_info
        return True

    async def save(self, access_token: str, refresh_token: str) -> bool:
        """Save."""
        new_user_info = await self._oauth.fetch_user_info(
            access_token,
            self._user_info,
        )
        ok = await self._storage.persist(
            access_token,
            refresh_token,
            new_user_info,
        )
        if not ok:
            return False
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._user_info = new_user_info
        return True

    async def clear(self) -> None:
        """Clear."""
        self._access_token = None
        self._refresh_token = None
        self._user_info = GOGUserInfo()
        await self._storage.clear_files()

    async def exchange_code(self, auth_code: str) -> ExchangeOutcome:
        """Exchange code (delegates; see :class:`ExchangeOutcome`)."""
        return await self._oauth.exchange_code(auth_code)

    async def refresh_if_stale(self) -> bool:
        """Refresh if stale."""
        return await self._oauth.refresh_if_stale(
            access_token=self._access_token,
            refresh_token=self._refresh_token,
            age_seconds=self.get_token_age_seconds(),
        )

    @contextlib.asynccontextmanager
    async def gogdl_credentials(self) -> AsyncIterator[tuple[dict[str, str], str]]:
        """Gogdl credentials."""
        env, creds_path, cleanup = await self.acquire_gogdl_creds()
        try:
            yield env, creds_path
        finally:
            await cleanup()

    async def acquire_gogdl_creds(
        self,
    ) -> tuple[
        dict[str, str],
        str,
        Any,
    ]:
        """Acquire GOGDL creds.

        Returns ``(env, creds_path, cleanup)``. ``creds_path`` is the
        absolute path to the freshly-written ``gog_credentials.json`` —
        callers MUST pass this verbatim to gogdl's ``--auth-config-path``
        flag (do not use ``GOGConfig.auth_config_path`` for that
        purpose, as it points at the legacy persistent location).
        """
        if not self._access_token or not self._refresh_token:
            raise RuntimeError(
                "acquire_gogdl_creds called without authenticated tokens",
            )
        return await self._gogdl.acquire(
            self._access_token,
            self._refresh_token,
        )
