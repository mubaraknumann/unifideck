"""What happens when the saved Microsoft sign-in stops working.

Two things end a saved sign-in:

- **The session died**: Microsoft rejected the refresh token (revoked,
  expired, password changed). The tokens are useless and are deleted, and
  ``STORE_LOGOUT`` flips the QAM row to signed out.
- **The client changed**: the saved tokens were issued to a different OAuth
  client than the configured one, as after the 0.7.6 switch to xbox.com's
  client. They are kept (a config rollback revives them), and the store just
  reads as signed out.

The user sees exactly that: the store is signed out and offers "Sign in".
No toast: Steam truncates toast text to a few words, which cannot carry the
reason, so the reason goes to the log, where a support bundle can read it.

Nothing here touches the Edge profile's cookies. Xbox Cloud Gaming streams
on those cookies, not on our tokens, so streaming keeps working until the
user signs in again.
"""
from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from unifideck.core.types import Events

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager

logger = logging.getLogger(__name__)


class SessionHealth:
    """Turns a lost Microsoft sign-in into a signed-out store and one log line."""

    def __init__(self, bus: EventBus, tokens: MicrosoftTokenManager) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._tokens = tokens

    async def on_session_dead(self, reason: str) -> None:
        """Microsoft rejected the saved sign-in: delete it, show signed out."""
        logger.warning(
            "[MicrosoftStore] Microsoft no longer accepts the saved sign-in "
            "(%s). Xbox library sync and Steam Store ownership stop until the "
            "user signs in again from the QAM; xCloud shortcuts are kept.",
            reason or "unknown",
        )
        await self._tokens.clear()
        with contextlib.suppress(Exception):
            await self._bus.emit(Events.STORE_LOGOUT, store="microsoft")

    async def on_client_changed(self) -> None:
        """The saved sign-in belongs to another client: log it once."""
        if not self._tokens.client_mismatch or self._tokens.mismatch_notified:
            return
        logger.warning(
            "[MicrosoftStore] the Microsoft sign-in method changed; the saved "
            "sign-in cannot be used and the store shows signed out. Xbox "
            "library sync and Steam Store ownership stop until the user signs "
            "in again from the QAM; xCloud shortcuts and streaming keep working.",
        )
        await self._tokens.mark_mismatch_notified()
