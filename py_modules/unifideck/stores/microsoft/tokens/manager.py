from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from unifideck.security import SecureTokenStore

from .oauth import OAuthMixin
from .persistence import PersistenceMixin
from .xbl_chain import XBLChainMixin, _UserToken

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
logger = logging.getLogger(__name__)
class MicrosoftTokenManager(
    PersistenceMixin,
    OAuthMixin,
    XBLChainMixin,
):
    """Microsoft token manager."""
    def __init__(
        self,
        config: MicrosoftConfig,
        locale_fn: Callable[[], str],
        secure_store: SecureTokenStore | None = None,
        bus: EventBus | None = None,
    ) -> None:
        """Initialize the instance."""
        self._config = config
        self._locale_fn = locale_fn
        self._bus = bus
        self._secure_store = (
            secure_store or SecureTokenStore(bus=bus)
        )
        self._ms_access_token: str | None = None
        self._ms_refresh_token: str | None = None
        self._token_saved_at: float = 0.0
        self._refresh_lock = asyncio.Lock()
        self._transient_until: float = 0.0
        self._refresh_error: str = ""
        self._client_mismatch = False
        self._mismatch_notified = False
        self._loaded_payload: dict[str, Any] | None = None
        self._xbl_user: _UserToken | None = None
    @property
    def access_token(self) -> str | None:
        """Access token."""
        return self._ms_access_token
    @property
    def has_refresh_token(self) -> bool:
        """Check whether refresh token."""
        return bool(self._ms_refresh_token)
    @property
    def last_token_error(self) -> str:
        """Why the last token request failed, or ``""``."""
        return self._refresh_error
