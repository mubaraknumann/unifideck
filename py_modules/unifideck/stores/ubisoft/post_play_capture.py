"""stores/ubisoft/post_play_capture.py — carry the rotated token home.

UPC rotates its refresh token during a play session and writes the new one
into the GAME prefix. The Play path runs in the out-of-process launcher, which
cannot reach the backend session facade, so nothing carries that token back to
``.upc-auth`` unless this handler does it on ``GAME_STOPPED``.

Its own module for two reasons: ``store.py`` is at its size cap, and the
budget arithmetic below only makes sense next to the handler it bounds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events
from unifideck.event_bus.event_bus_devex import subscribe
from unifideck.stores.shared.wrapper_session_hooks import (
    _EXIT_WAIT_SECONDS,
    await_client_exit,
)
from unifideck.stores.ubisoft.session.lock import _ACQUIRE_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
    from unifideck.stores.ubisoft.session import UbisoftSession

logger = logging.getLogger(__name__)

#: Budget for :meth:`UbisoftStore._capture_upc_session_on_stop`, derived from
#: the bounded waits it actually performs rather than picked.
#:
#: The handler waits for UPC to exit (``_EXIT_WAIT_SECONDS``), then takes the
#: cross-process session lock (``_ACQUIRE_TIMEOUT_SECONDS``), then copies the
#: vault and fans it out across every Ubisoft prefix. Under the bus watchdog's
#: 5s ``DEFAULT_HANDLER_TIMEOUT_SEC`` that is a deadline inversion: the inner
#: waits alone can reach 30s, so the watchdog cancelled the handler long
#: before the lock's own "proceed unserialised" fallback could ever run, and
#: the fallback was therefore dead code.
#:
#: Measured on-device 2026-09-05: of four consecutive post-play captures, the
#: two where UPC had already exited finished in 204ms and 149ms, and the two
#: where it had not were cancelled at 4.998s and 4.965s. Both cancellations
#: silently dropped a rotated refresh token, which left ``.upc-auth`` on a
#: token Ubisoft had already retired — the next fresh install then opened
#: signed-out. Nothing waits on this handler, so a generous budget costs
#: nothing; being cancelled costs a login.
#:
#: ``tests/unit/test_ubisoft_capture_budget.py`` asserts this stays above the
#: waits it is derived from, so lengthening either constant cannot silently
#: reintroduce the inversion.
_CAPTURE_SLACK_SECONDS = 15.0
_CAPTURE_TIMEOUT_SECONDS = (
    _EXIT_WAIT_SECONDS + _ACQUIRE_TIMEOUT_SECONDS + _CAPTURE_SLACK_SECONDS
)


class PostPlayCaptureMixin:
    """``GAME_STOPPED`` → capture the rotated UPC token into the auth prefix.

    Mixed into :class:`~unifideck.stores.ubisoft.store.UbisoftStore`, which
    supplies these three collaborators; they are declared here so the mixin
    type-checks on its own rather than only in combination.
    """

    _paths: UbisoftPrefixPaths
    _session: UbisoftSession
    _bus: EventBus

    @subscribe(Events.GAME_STOPPED, timeout=_CAPTURE_TIMEOUT_SECONDS)
    async def _capture_upc_session_on_stop(self, **kwargs: Any) -> None:
        """Capture the token UPC rotated during play back to the auth prefix.

        The Play path runs in the launcher subprocess, which can't reach the
        backend session facade — so a token UPC rotates while the game runs is
        written only into the game prefix and never makes it back to
        ``.upc-auth``. Left uncaptured, the auth prefix ends up on a
        server-stale token and the next FRESH install (or a game installed to a
        new prefix) opens signed-out. Capturing on game-stop keeps auth current
        after every play. ``capture()`` is guarded (auth-only, skips a
        logged-out / smaller source), so a normal exit that didn't rotate — or
        an explicit logout — is a safe no-op.

        UPC flushes its rotated credential as it shuts down, so the read waits
        (bounded) for the client to actually be gone first. Reading too early
        gets a torn vault, and ``propagate_all_to_all`` below would then push
        it to every Ubisoft prefix.
        """
        if kwargs.get("store") != "ubisoft":
            return
        game_id = kwargs.get("game_id")
        if not isinstance(game_id, str) or not game_id:
            return
        # One capture at a time. A cancelled ``asyncio.to_thread`` returns
        # immediately while its thread runs on holding the cross-process
        # session lock, so a second GAME_STOPPED arriving during the first
        # capture would queue behind that lock and burn its own budget
        # waiting. That is how one timeout became two consecutive ones in the
        # 2026-09-05 bundle. Serialising here keeps the flock uncontended in
        # the common case; the flock still covers the launcher process.
        async with self._capture_gate():
            await self._capture_session_now(game_id)

    def _capture_gate(self) -> asyncio.Lock:
        """The one-at-a-time gate, created on first use.

        Lazy rather than set in ``__init__`` because the store is also built
        via ``__new__`` (the post-play capture tests drive this handler on an
        otherwise-bare instance), and a gate that only exists on the fully
        constructed object would make the handler depend on how it was made.
        """
        gate = getattr(self, "_capture_gate_lock", None)
        if gate is None:
            gate = asyncio.Lock()
            self._capture_gate_lock = gate
        return gate

    async def _capture_session_now(self, game_id: str) -> None:
        """Capture from ``game_id``'s prefix; see the caller for the why."""
        try:
            prefix_path = self._paths.get_prefix_path(game_id)
            await await_client_exit("ubisoft", Path(prefix_path))
            captured = await asyncio.to_thread(
                self._session.capture, prefix_path,
            )
            if captured:
                await asyncio.to_thread(self._session.propagate_all_to_all)
                logger.info(
                    "[UbisoftStore] captured rotated UPC token after play "
                    "for %s → auth refreshed",
                    game_id,
                )
            else:
                await self._warn_if_stored_credential_is_dead(
                    game_id, prefix_path,
                )
        except Exception as e:
            logger.warning(
                "[UbisoftStore] post-play session capture failed: %s", e,
            )

    async def _warn_if_stored_credential_is_dead(
        self, game_id: str, prefix_path: str,
    ) -> None:
        """Tell the user when re-signing-in is the only way out.

        ``capture`` returning nothing is normally a healthy no-op. But when the
        prefix we injected a signed-in credential into comes back signed OUT,
        the stored token is dead server-side and every future install will
        inject it again — a silent loop of sign-in prompts with no UI affordance
        to break it. Say so, and name the one action that fixes it.

        Report-only: the credential is never purged here. Sign-out is
        destructive and stays the user's decision.
        """
        try:
            dead = await asyncio.to_thread(
                self._session.stored_credential_was_rejected, prefix_path,
            )
        except Exception:
            logger.exception("[UbisoftStore] credential health check failed")
            return
        if not dead:
            return
        logger.warning(
            "[UbisoftStore] UPC signed out of %s despite the injected "
            "credential — the stored Ubisoft token is no longer accepted. "
            "Sign out and back in (QAM → Ubisoft) to replace it; until then "
            "every install will keep asking for a sign-in.",
            game_id,
        )
        # LAUNCHER_STAGE, not STORE_AUTH_FAILED. The rule is stated in
        # ``stores/shared/wrapper_auth_monitor.py``: the frontend translates
        # STORE_AUTH_FAILED into a store status of "error", and the Ubisoft
        # row then renders with no auth button at all — this message names
        # "sign out and back in" as the fix while removing the button that
        # does it. LAUNCHER_STAGE is the plugin's only user-facing toast
        # channel (see ``core/types/events.py``), and it changes no status.
        with contextlib.suppress(Exception):
            await self._bus.emit(
                Events.LAUNCHER_STAGE,
                severity="warning",
                i18n_title_key="toasts.ubisoftSignInExpired",
                i18n_key="toasts.ubisoftSignInExpiredMessage",
                duration_ms=10000,
                store="ubisoft",
            )
