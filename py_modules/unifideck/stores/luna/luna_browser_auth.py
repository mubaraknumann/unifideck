"""LunaBrowserAuth: observes the interactive Amazon sign-in over CDP and
confirms the Amazon session cookies persisted into the Luna browser profile.

This is the Luna analogue of ``microsoft_browser_auth.py`` — but Luna has
**no OAuth**: there is no authorize/token endpoint, no authorization-code
exchange and no token file (design "LunaBrowserAuth / LunaAuth", Req 1.3).
"Signed in" means the interactive Amazon sign-in in the streaming browser
left non-expired Amazon session cookies in the Luna browser profile (F2).

:class:`LunaBrowserAuth` therefore does two things, both read-only:

1. **Observe the signed-in state over CDP.** It inspects the live page
   targets exposed on the browser-game CDP port. The browser starts on the
   Amazon sign-in page; the sign-in is observed as *complete* once no live
   page target is still showing an Amazon sign-in page (the inverse of
   :meth:`LunaSessionMonitor.looks_like_signin_page`) and at least one page
   target has navigated back under the Luna host. This is the CDP signal
   that the interactive sign-in finished (Req 1.3, first clause).

2. **Confirm the cookies persisted.** Observing the UI leaving the sign-in
   page is necessary but not sufficient — Req 1.3's second clause requires
   that Amazon session cookies are actually present in the Luna profile.
   That is :class:`LunaSessionMonitor`'s usability predicate
   (:meth:`LunaSessionMonitor.is_usable`), which this class delegates to so
   there is a single source of truth for "is the session usable?".

It owns no browser process and does not drive the sign-in — :class:`LunaAuth`
(task 10.2) opens the browser at the sign-in page with the spoofer attached
and polls this class. The one I/O seam is the :class:`EdgeCDPClient` passed
in, so :meth:`observe_signed_in` is drivable against a stub CDP client in
tests without a browser.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig
    from unifideck.stores.luna.luna_session_monitor import LunaSessionMonitor

logger = logging.getLogger(__name__)

#: URL markers that mean a live page target is still showing an Amazon
#: sign-in page. Mirrors ``LunaSessionMonitor._SIGNIN_URL_MARKERS`` — the
#: inverse signal: while any page carries one of these, the interactive
#: sign-in has not completed (Req 1.3).
_SIGNIN_URL_MARKERS: tuple[str, ...] = ("/ap/signin", "/ap/oa", "ap/signin")


@dataclass(frozen=True)
class ObservedAuthState:
    """The outcome of observing the sign-in over CDP and the cookie store.

    ``signed_in`` is true only when the CDP signal shows the sign-in UI has
    completed (no page target on an Amazon sign-in page, and a page target
    has reached the Luna host) **and** :class:`LunaSessionMonitor` confirms
    non-expired Amazon session cookies are present in the Luna profile
    (Req 1.3). ``cdp_left_signin`` and ``cookies_present`` record the two
    halves so the caller/log can tell "UI still on sign-in" apart from
    "signed in but no cookies persisted" (Req 1.6).
    """

    signed_in: bool
    cdp_left_signin: bool
    cookies_present: bool


class LunaBrowserAuth:
    """Observe the Amazon sign-in over CDP and confirm cookies persisted.

    Constructed from the :class:`LunaSessionMonitor` (the cookie-store
    source of truth) and the :class:`LunaConfig` (the Luna host used to
    recognise a signed-in page). No OAuth, no token exchange, no browser
    process ownership.
    """

    def __init__(
        self, session: LunaSessionMonitor, config: LunaConfig,
    ) -> None:
        """Bind to the session monitor and resolved Luna config."""
        self._session = session
        self._config = config

    def observe_signed_in(self, cdp: EdgeCDPClient) -> ObservedAuthState:
        """Observe one snapshot of the sign-in state over CDP (Req 1.3).

        Reads the live CDP page targets (read-only) to decide whether the
        interactive sign-in UI has completed, then confirms via
        :class:`LunaSessionMonitor` that Amazon session cookies are present
        in the Luna profile. Both must hold for ``signed_in`` to be true.
        Does not drive the browser and does not block; :class:`LunaAuth`
        calls this repeatedly until it reports signed-in or its own timeout
        fires (Req 1.8).
        """
        cdp_left_signin = self._cdp_shows_signed_in(cdp)
        cookies_present = self._session.is_usable()
        signed_in = cdp_left_signin and cookies_present
        if signed_in:
            logger.info(
                "[LunaBrowserAuth] sign-in observed complete: UI left the "
                "Amazon sign-in page and Amazon session cookies persisted",
            )
        return ObservedAuthState(
            signed_in=signed_in,
            cdp_left_signin=cdp_left_signin,
            cookies_present=cookies_present,
        )

    def is_signed_in(self, cdp: EdgeCDPClient) -> bool:
        """True iff the sign-in is observed complete and cookies persisted.

        Convenience over :meth:`observe_signed_in` for the polling caller
        (:class:`LunaAuth`).
        """
        return self.observe_signed_in(cdp).signed_in

    def _cdp_shows_signed_in(self, cdp: EdgeCDPClient) -> bool:
        """Whether the CDP page targets show the sign-in UI has completed.

        Signed-in iff **no** live page target is still on an Amazon sign-in
        page (the inverse of
        :meth:`LunaSessionMonitor.looks_like_signin_page`) **and** at least
        one page target has reached the Luna host — the post-sign-in landing
        under ``luna.amazon.*``. Best-effort and read-only: if the targets
        cannot be read, or no page target has reached the Luna host yet, it
        reports not-signed-in so the caller keeps polling rather than
        falsely confirming.
        """
        try:
            targets = cdp.list_targets()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("[LunaBrowserAuth] could not list CDP targets: %s", e)
            return False

        page_urls = [
            str(t.get("url", "")).lower()
            for t in targets
            if t.get("type") == "page"
        ]
        if not page_urls:
            return False
        if any(self._is_signin_url(url) for url in page_urls):
            return False
        return any(self._is_luna_host_url(url) for url in page_urls)

    @staticmethod
    def _is_signin_url(url: str) -> bool:
        """Whether *url* is an Amazon sign-in page (lower-cased input)."""
        return any(marker in url for marker in _SIGNIN_URL_MARKERS)

    def _is_luna_host_url(self, url: str) -> bool:
        """Whether *url* is under the configured Luna host (lower-cased input).

        Signals the browser has returned to Luna after the Amazon sign-in.
        An unset Luna host makes this unmatchable, so the caller keeps
        polling rather than confirming on a bare cookie read.
        """
        host = (self._config.luna_host or "").lower()
        return bool(host) and host in url
