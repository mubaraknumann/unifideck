"""
Ubisoft authentication facade — orchestrates the Steam-shortcut auth flow.

Ubisoft Connect has no headless auth flow: the user must sign in
through the UPC GUI. The trick we use is to create a Steam shortcut
that launches UPC inside a dedicated auth prefix; once the user signs
in, UPC writes credentials to the prefix and we propagate them to
every game prefix afterwards (via ``UbisoftSession``).

``UbisoftAuth`` is the orchestration class that wires together the
sub-modules: ``context`` (UI payload), ``shortcut`` (Steam shortcut
creation), ``direct_signin`` (fallback for already-signed-in installs).
Watching the auth prefix for the credentials UPC writes is no longer one
of them — that is ``stores/shared/wrapper_auth_monitor``, shared with
Battle.net, which also reports the *failure* path this store used to
leave silent.

``UbisoftAuthState`` and ``UbisoftAuthServices`` are frozen dataclasses
holding the dependencies. State is "owned data" (config, paths,
binaries, callbacks); Services are "external system handles"
(shortcut_service, steamgriddb).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from unifideck.core.types import AuthResult, Events, Result
from unifideck.security import audit_auth_flow
from unifideck.stores.shared.wrapper_auth_monitor import WrapperAuthMonitor
from unifideck.stores.ubisoft.binaries import UbisoftBinaryResolver
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
from unifideck.stores.ubisoft.session import UbisoftSession

from .context import _AuthContext
from .direct_signin import _DirectSignIn
from .shortcut import _AuthShortcut
from .shortcut_ops import _ShortcutRegistryOps

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.services.shortcut import ShortcutService
    from unifideck.steam.steamgriddb import SteamGridDBClient
logger = logging.getLogger(__name__)

# Background prefix-deletion tasks. Holding a strong ref keeps them from
# being garbage-collected (which would cancel the delete) before they
# finish; the done-callback drops the ref.
_PURGE_TASKS: set[asyncio.Task[None]] = set()

@dataclass(frozen=True)
class UbisoftAuthState:
    """Ubisoft auth state."""

    config: UbisoftConfig
    paths: UbisoftPrefixPaths
    binaries: UbisoftBinaryResolver
    session: UbisoftSession
    ensure_auth_prefix: Callable[[], Any]
    queue_auth_assets_ensure: Callable[[str], None]

@dataclass(frozen=True)
class UbisoftAuthServices:
    """Ubisoft auth services."""

    plugin_dir: str | None
    shortcut_service: ShortcutService | None
    steamgriddb: SteamGridDBClient | None

class UbisoftAuth:
    """Ubisoft auth."""

    def __init__(
        self,
        bus: EventBus,
        state: UbisoftAuthState,
        services: UbisoftAuthServices,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._config = state.config
        self._paths = state.paths
        self._binaries = state.binaries
        self._session = state.session
        self._ensure_auth_prefix = state.ensure_auth_prefix
        self._queue_auth_assets_ensure = state.queue_auth_assets_ensure
        self._plugin_dir = services.plugin_dir
        self._shortcut_service = services.shortcut_service
        self._steamgriddb = services.steamgriddb
        self._registry_ops = _ShortcutRegistryOps(config=self._config)
        self._monitor = WrapperAuthMonitor(
            store="ubisoft",
            is_signed_in=self._capture_auth_session,
            on_captured=self._on_auth_session_captured,
            bus=self._bus,
        )
        self._direct_signin = _DirectSignIn(
            binaries=self._binaries,
            bus=self._bus,
            config=self._config,
            paths=self._paths,
            session=self._session,
            ensure_auth_prefix=self._ensure_auth_prefix,
            queue_auth_assets_ensure=self._queue_auth_assets_ensure,
        )
        self._shortcut = _AuthShortcut(self)
        self._context = _AuthContext(self)

    async def ensure_auth_shortcut(self) -> int | None:
        """Ensure auth shortcut."""
        return await self._shortcut.ensure_auth_shortcut()

    async def auth_shortcut_exists_in_vdf(self) -> bool:
        """Auth shortcut exists in VDF."""
        return await self._shortcut.auth_shortcut_exists_in_vdf()

    async def fetch_auth_shortcut_artwork(
        self,
        unsigned_id: int,
        force: bool = False,
    ) -> None:
        """Fetch auth shortcut artwork."""
        await self._context.fetch_auth_shortcut_artwork(
            unsigned_id,
            force=force,
        )

    async def get_auth_shortcut_context(self) -> dict[str, Any]:
        """Get auth shortcut context."""
        return await self._context.get_auth_shortcut_context()

    def credential_state(self) -> str:
        """What the ``.upc-auth`` vault says about the user's session.

        Three outcomes, because two of them are not the same answer
        downstream (see ``UbisoftStore.get_library``):

        * ``"signed_in"``  — a vault with an account attached.
        * ``"signed_out"`` — a vault is there, but UPC has signed out of it
          (or the server rejected the token and UPC cleared it). We know the
          user *has* a Ubisoft account; we just cannot speak for its library.
        * ``"absent"``     — no plausible vault at all: never signed in, or
          ``logout()`` purged the prefix. Authoritatively not connected.
        """
        auth_dir = self._config.auth_prefix_dir_expanded
        if self._session.is_signed_in(auth_dir):
            return "signed_in"
        if self._session.has_valid_credentials(auth_dir):
            return "signed_out"
        return "absent"

    async def is_available(self) -> bool:
        """Whether the user is actually signed in to Ubisoft.

        Keyed on the ``.upc-auth`` vault carrying an attached account, which
        is what UPC itself reads at startup to decide whether to show the
        library or a login form.

        This used to ask ``has_valid_credentials`` — does a plausible vault
        FILE exist — which is a different question, and the gap between the
        two was user-visible: UPC signs itself out by rewriting the vault in
        place (6471 bytes signed out vs 7612 signed in, measured on-device
        2026-09-05), so the file survives sign-out and this returned True
        forever after. The QAM then showed Ubisoft connected, with a Sign-out
        button, while clicking the storefront opened UPC on a login screen.
        Reported by a user, reproduced on this device: ``check_store_status``
        answered ``available: true`` against a vault with no account in it.

        ``logout()`` still deletes the whole prefix, so the sign-out path is
        unchanged. Known edge, unchanged: an auth prefix removed out-of-band
        while game prefixes still hold credentials reads False here.
        """
        return self.credential_state() == "signed_in"

    @audit_auth_flow(store="ubisoft", method="wine_installer")
    async def start_auth(self) -> AuthResult:
        """Start auth.

        Returns ``pending=True`` so the frontend's
        AuthDispatcher does NOT fast-path resolve — Ubisoft
        Connect runs in a dedicated Wine prefix, and the
        user must sign in through the UPC GUI before any
        tokens exist. The session monitor emits
        ``STORE_AUTH_COMPLETE`` when credentials are
        detected.
        """
        return AuthResult(
            success=True,
            store="ubisoft",
            metadata={
                "auth_type": "upc_launch",
                "message": "Sign in through Ubisoft Connect",
                "pending": True,
            },
        )

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """Complete auth — succeeds once UPC has captured credentials.

        Ubisoft has no headless / code / 2FA flow: the user signs in
        through the UPC GUI in the auth prefix and the session monitor
        detects the credential files. This just reports whether that
        capture has happened.
        """
        if await self.is_available():
            return AuthResult(
                success=True,
                store="ubisoft",
            )
        return AuthResult(
            success=False,
            store="ubisoft",
            error="not_authenticated",
        )

    async def logout(self) -> Result:
        """Sign out instantly; purge the auth prefix in the background.

        The auth prefix is a full UPC Wine prefix — tens of thousands of
        tiny Chromium-cache files — and a synchronous ``shutil.rmtree``
        of it took ~45s while **blocking the event loop**, so the QAM
        "sign out" button just greyed and looked dead. Instead we rename
        the prefix to a ``.trash-*`` sibling (an atomic, instant metadata
        op that immediately flips ``is_available`` to False = signed
        out), then delete the renamed directory off the event loop
        without making logout wait for it.
        """
        # A sign-in still being watched is moot once the user signs out, and
        # the prefix it is watching is about to be renamed out from under it.
        await self._monitor.stop()
        self._session.clear_session_file()
        auth_dir = self._config.auth_prefix_dir_expanded
        trash = await asyncio.to_thread(self._rename_to_trash, auth_dir)
        if trash:
            self._spawn_background_purge(trash)
        # Removing the auth prefix alone doesn't sign the user out: login
        # propagated the UPC credentials into every game prefix and the
        # template, and ``find_best_credential_source`` falls back to those
        # copies — so the next launch/sign-in silently re-authenticates.
        # Purge them too (off the event loop; deletes across many prefixes).
        try:
            purged = await asyncio.to_thread(
                self._session.purge_credentials_from_all,
            )
            logger.info(
                "[UbisoftAuth] logout purged auth state from %d "
                "prefix entry(ies)",
                purged,
            )
        except Exception:
            logger.exception("[UbisoftAuth] credential purge on logout failed")
        await self._bus.emit(
            Events.STORE_LOGOUT,
            store="ubisoft",
        )
        return Result(success=True)

    @staticmethod
    def _rename_to_trash(auth_dir: str) -> str | None:
        """Atomically move the auth prefix aside; return the path to delete.

        Returns ``None`` when there's nothing to remove. On the rare
        rename failure (e.g. a cross-device edge) it returns the original
        path so the caller still deletes it — just in place.
        """
        src = Path(auth_dir)
        if not src.is_dir():
            return None
        trash = src.with_name(f"{src.name}.trash-{int(time.time() * 1000)}")
        try:
            src.rename(trash)
        except OSError:
            logger.warning(
                "[UbisoftAuth] auth prefix rename failed; deleting in place",
            )
            return str(src)
        return str(trash)

    def _spawn_background_purge(self, path: str) -> None:
        """Fire-and-forget recursive delete of ``path`` off the event loop."""

        async def _purge() -> None:
            await asyncio.to_thread(shutil.rmtree, path, ignore_errors=True)
            logger.info("[UbisoftAuth] purged old auth prefix %s", path)

        task = asyncio.create_task(_purge())
        _PURGE_TASKS.add(task)
        task.add_done_callback(_PURGE_TASKS.discard)

    async def _capture_auth_session(self) -> bool:
        """The monitor's sign-in probe: has UPC written credentials yet?

        Capturing *is* the probe for this store — ``UbisoftSession.capture``
        returns falsy until the credential files exist, and moves them once
        they do. The auth prefix is resolved per call rather than closed over,
        so a config change between sign-ins is picked up.
        """
        return bool(self._session.capture(self._config.auth_prefix_dir_expanded))

    async def _on_auth_session_captured(self) -> None:
        """Fan the freshly captured credentials out to every game prefix."""
        self._session.propagate_all_to_all()
        self._queue_auth_assets_ensure("post-auth-session-capture")

    async def start_auth_session_monitor(self) -> Result:
        """Start auth session monitor."""
        return await self._monitor.start()

    async def stop_auth_session_monitor(self) -> None:
        """Abandon a pending sign-in watch silently."""
        await self._monitor.stop()

    def check_auth_session_status(self) -> dict[str, Any]:
        """Check auth session status."""
        return self._monitor.status()

    async def connect_ubisoft_account(self) -> dict[str, Any]:
        """Connect UBISOFT account."""
        return await self._direct_signin.connect()

    async def _load_registry(self, sm: ShortcutService) -> dict[str, Any]:
        """Load registry."""
        return await self._registry_ops.load(sm)

    async def _register_shortcut(
        self,
        sm: ShortcutService,
        appid: int,
        name: str,
    ) -> None:
        """Register shortcut."""
        await self._registry_ops.register(sm, appid, name)

    async def _clear_compat(
        self,
        sm: ShortcutService,
        appid: int,
    ) -> None:
        """Clear compat."""
        await self._registry_ops.clear_compat(sm, appid)

    async def _cleanup_legacy_registry(self, sm: ShortcutService) -> None:
        """Cleanup legacy registry."""
        await self._registry_ops.cleanup_legacy(sm)
