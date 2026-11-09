"""itch.io sign-in through the standard Edge auth window.

The credential butler needs is an **API key**: a key generated with the
settings page's own button is unscoped and can download (itch.io server-side
docs), while OAuth tokens issued to third-party apps cannot (itch.io staff,
itch.io/t/1588368). So the "code" this flow captures is the key itself, read
off ``itch.io/user/settings/api-keys`` once the user has signed in, and the
"exchange" is butlerd ``Profile.LoginWithAPIKey``.

**Not every key on that page can read the library.** The page also lists keys
other tools made, and those can be scoped: a reporter's first key signed in
fine (``/profile`` only needs ``profile:me``) and then every sync answered
``403 api key does not permit `profile:owned```. So the capture takes every
key on the page, and the exchange keeps the first one that can read owned
keys (:func:`key_reads_library`). A saved login gets the same check. Lutris, GameHub and itch-dl
all use keys from that page; Lutris moved to it after its embedded-browser
login looped on Cloudflare Turnstile.

What the page does, measured 2026-09-23:

* itch.io re-confirms the password (``/sudo``) before showing the page.
* A user with no key sees "You currently don't have any API keys." and one
  **Generate new API key** button. The user presses it; nothing is clicked on
  their behalf.
* Each key (40 alphanumerics) sits in a ``td.key_cell code.full_key``, which
  is **hidden** until "View" is clicked, so it is not in ``innerText``. The
  capture joins those elements' ``textContent`` with commas instead.
* ``return_to`` on the login URL is dropped by a password reset (the test
  user landed on ``/my-feed``). A small steering task sends any signed-in
  itch.io page that is not part of the login flow to the API-keys page.

Sign-out forgets butler's profile. The key itself stays valid on itch.io
until the user revokes it there.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from unifideck.auth.browser_types import ContentCapture
from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.core.types import AuthResult, Events, Result
from unifideck.event_bus.event_bus import EventBus
from unifideck.security import audit_auth_flow

from .butlerd import ButlerDaemon, ButlerdError

logger = logging.getLogger(__name__)

STORE = "itch"
API_KEYS_URL = "https://itch.io/user/settings/api-keys"
LOGIN_URL = (
    "https://itch.io/login?return_to=https%3A%2F%2Fitch.io%2Fuser%2Fsettings%2Fapi-keys"
)
AUTH_URL_FILE = "~/.local/share/unifideck/itch_auth_url.txt"

KEY_CAPTURE = ContentCapture(
    trigger_url="itch.io/user/settings/api-keys",
    regex=r"^\s*([A-Za-z0-9]{40}(?:,[A-Za-z0-9]{40})*)\s*$",
    expression=(
        "Array.from(document.querySelectorAll('td.key_cell code.full_key'))"
        ".map(e => (e.textContent || '').trim())"
        ".filter(k => /^[A-Za-z0-9]{40}$/.test(k)).join(',')"
    ),
)

#: The sign-in error when no key on the page can read the library.
KEY_LIMITED = "itch_key_limited"

#: itch.io paths that belong to signing in, where steering must not interrupt.
_LOGIN_FLOW_PREFIXES = (
    "/login", "/sudo", "/register", "/user/forgot-password",
    "/user/reset-password", "/totp", "/user/settings/api-keys",
)
_STEER_POLL_S = 2.0
_STEER_MAX = 3
_STEER_DEADLINE_S = 600.0


def should_steer(url: str) -> bool:
    """A signed-in itch.io page outside the login flow (e.g. ``/my-feed``)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "itch.io":
        return False
    return not parsed.path.startswith(_LOGIN_FLOW_PREFIXES)


async def key_reads_library(daemon: ButlerDaemon, profile_id: int) -> bool:
    """False when itch.io refuses this profile's key the owned-keys read.

    A 403 is the scoped-key answer (and a revoked key's). Any other failure
    is not evidence against the key, so it does not reject it.
    """
    try:
        await daemon.call("Fetch.ProfileOwnedKeys", {
            "profileId": profile_id, "limit": 1, "fresh": True,
        })
    except ButlerdError as e:
        if e.api_status == 403:
            return False
        logger.warning("[itch] could not check the key's library access: %s", e)
    return True


class ItchAuthFlow:
    """Edge sign-in → API key → butler profile."""

    def __init__(
        self, bus: EventBus, orchestrator: AuthOrchestrator, daemon: ButlerDaemon,
        edge: Callable[[], Any],
    ) -> None:
        self._bus = bus
        self._orch = orchestrator
        self._daemon = daemon
        self._edge = edge
        self._steer_task: asyncio.Task[None] | None = None

    @audit_auth_flow(store=STORE, method="oauth_cli")
    async def start_auth(self) -> AuthResult:
        """Reuse a still-valid saved login, else open the Edge flow."""
        if not self._daemon.available:
            return AuthResult(success=False, error="butler_not_found", store=STORE)
        if await self._saved_login_valid():
            logger.info("[itch] saved login still valid; skipping sign-in")
            await self._bus.emit(Events.STORE_AUTH_COMPLETE, store=STORE)
            return AuthResult(success=True, store=STORE)
        result = await self._orch.run_flow(
            get_url=self._login_url,
            allowed_uris=[],
            exchange_code=self._login_with_key,
            background=True,
            write_url_file=AUTH_URL_FILE,
            content=KEY_CAPTURE,
        )
        if result.success:
            self._start_steering()
        return result

    async def logout(self) -> Result:
        """Forget every butler profile; the key stays valid on itch.io."""
        self._stop_steering()
        try:
            listed = await self._daemon.call("Profile.List")
            for profile in listed.get("profiles") or []:
                await self._daemon.call("Profile.Forget", {"profileId": profile["id"]})
        except ButlerdError as e:
            return Result(success=False, store=STORE, error=f"itch_logout_failed: {e}")
        return Result(success=True, store=STORE)

    async def _login_url(self) -> str:
        return LOGIN_URL

    async def _login_with_key(self, captured: str) -> AuthResult:
        """The orchestrator's exchange step: the first captured key that works.

        ``captured`` is every key on the page, comma-separated. A key butler
        accepts but that cannot read owned keys is forgotten and the next
        one is tried.
        """
        self._stop_steering()
        keys = list(dict.fromkeys(k for k in captured.split(",") if k))
        error = KEY_LIMITED
        # A scoped key is the more useful thing to report than a bad one.
        limited = False
        for index, key in enumerate(keys, start=1):
            try:
                result = await self._daemon.call("Profile.LoginWithAPIKey", {"apiKey": key})
                profile = result.get("profile") or {}
                if await key_reads_library(self._daemon, int(profile["id"])):
                    user = profile.get("user") or {}
                    logger.info("[itch] signed in as %s with key %d of %d",
                                user.get("username") or "<unknown>", index, len(keys))
                    return AuthResult(success=True, store=STORE, tokens_cached=True)
                logger.warning("[itch] key %d of %d cannot read the library "
                               "(scoped or revoked); trying the next", index, len(keys))
                await self._daemon.call("Profile.Forget", {"profileId": profile["id"]})
                limited = True
            except ButlerdError as e:
                logger.warning("[itch] key %d of %d rejected: %s", index, len(keys), e)
                error = f"itch_login_failed: {e}"
        return AuthResult(success=False, store=STORE, error=KEY_LIMITED if limited else error)

    async def _saved_login_valid(self) -> bool:
        """True when butler holds a profile whose key can still read the library."""
        try:
            listed = await self._daemon.call("Profile.List")
            profiles = listed.get("profiles") or []
            if not profiles:
                return False
            profile_id = int(profiles[0]["id"])
            await self._daemon.call("Profile.UseSavedLogin", {"profileId": profile_id})
            if not await key_reads_library(self._daemon, profile_id):
                logger.info("[itch] saved key cannot read the library; signing in again")
                await self._daemon.call("Profile.Forget", {"profileId": profile_id})
                return False
        except ButlerdError as e:
            logger.info("[itch] saved login rejected (%s); signing in again", e)
            return False
        return True

    def _start_steering(self) -> None:
        self._stop_steering()
        self._steer_task = asyncio.create_task(self._steer(), name="itch-auth-steer")

    def _stop_steering(self) -> None:
        if self._steer_task is not None and not self._steer_task.done():
            self._steer_task.cancel()
        self._steer_task = None

    async def _steer(self) -> None:
        """Send a signed-in tab that is not on the key page to the key page."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _STEER_DEADLINE_S
        steered = 0
        while steered < _STEER_MAX and loop.time() < deadline:
            await asyncio.sleep(_STEER_POLL_S)
            edge = self._edge()
            if edge is None:
                return
            with contextlib.suppress(Exception):
                # list_targets is a blocking urllib probe (1 s timeout).
                targets = await asyncio.to_thread(edge.list_targets)
                pages = [t for t in targets if t.get("type") == "page"]
                if pages and should_steer(str(pages[0].get("url") or "")):
                    steered += 1
                    logger.info("[itch] signed in; opening the API-keys page")
                    await edge.navigate_tab(API_KEYS_URL)
