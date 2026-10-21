from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from unifideck.security import SecureTokenStore

from .endpoint import TokenState
from .oauth import OAuthMixin
from .persistence import PersistenceMixin
from .xbl_chain import XBLChainMixin, _UserToken

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.microsoft.microsoft_config import MicrosoftConfig
    from unifideck.stores.microsoft.xodus.ticket import XodusTicket
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
        #: The one sign-in: with Xbox PC on and Xodus signed in, the Microsoft
        #: ticket comes from the Xodus credential, not from OAuth.
        self.xodus_ticket: XodusTicket | None = None
        self._using_ticket = False
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

    def _ticket_mode(self) -> bool:
        source = self.xodus_ticket
        return source is not None and source.active()

    async def load(self) -> bool:
        """Load the sign-in: the Xodus ticket when it applies, else OAuth."""
        if self._ticket_mode():
            assert self.xodus_ticket is not None
            ticket = await self.xodus_ticket.get()
            self._using_ticket = ticket is not None
            if ticket is not None:
                self._ms_access_token = ticket
                return True
            self._refresh_error = self.xodus_ticket.last_error
            return False
        self._using_ticket = False
        return await super().load()

    async def refresh_if_stale(self) -> TokenState:
        """Refresh the sign-in. A ticket the service cannot renew is transient:
        the Xodus credential itself is only judged by the Xodus sign-in."""
        if self._ticket_mode():
            return TokenState.FRESH if await self.load() else TokenState.TRANSIENT
        return await super().refresh_if_stale()

