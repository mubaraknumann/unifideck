from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from unifideck.auth.browser import OAuthBrowserMonitor
from unifideck.auth.edge_browser import EdgeBrowser
from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.auth.url_file import remove_url_file
from unifideck.core.types import (
    AuthResult,
    Events,
    Game,
    InstallResult,
    Result,
    StoreInfo,
)
from unifideck.services.shortcut import ShortcutService
from unifideck.stores.shared.browser_auth_rebuild import (
    BrowserAuthRebuildMixin,
)
from unifideck.stores.shared.store_base import StoreBase
from unifideck.utils.locale import get_unifideck_locale

from .library_gate import GateVerdict, check_subscription_gate
from .microsoft_browser_auth import MicrosoftBrowserAuth
from .microsoft_catalog import MicrosoftCatalogReader
from .microsoft_config import MicrosoftConfig
from .microsoft_device_auth import MS_AUTH_URL_FILE, MicrosoftDeviceAuth
from .ownership import MicrosoftOwnershipReader, OwnedFetch
from .pc_library.library import PC_ONLY, XboxPcLibrary, has_pc_rows
from .session_health import SessionHealth
from .tokens import MicrosoftTokenManager, TokenState
from .xodus import auto_components, single_signin
from .xodus.store_hooks import XboxPcStoreHooks
from .xodus.ticket import XodusTicket
from .xodus.xbox_pc import XboxPc

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.services.microsoft_subscription import MicrosoftSubscriptionService
logger = logging.getLogger(__name__)


@dataclass
class _XcloudRead:
    """What ``_read_xcloud`` found. ``games`` None = could not read."""

    games: list[Game] | None
    verdict: GateVerdict | None = None
    market: str | None = None
    #: The Xbox PC library read, started next to the xCloud read.
    pending: asyncio.Task[Any] | None = None


def _cancel(task: asyncio.Task[Any] | None) -> None:
    if task is not None and not task.done():
        task.cancel()


class MicrosoftStore(BrowserAuthRebuildMixin, StoreBase):
    """Microsoft store."""
    store_info = StoreInfo(
        name="microsoft",
        display_name="Microsoft",
        auth_method="oauth",
        icon_asset="microsoft.png",
        supports_install=True,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
        browser_monitor: OAuthBrowserMonitor | None = None,
        shortcut_service: ShortcutService | None = None,
        edge_browser: EdgeBrowser | None = None,
        subscription_service: MicrosoftSubscriptionService | None = None,
    ) -> None:

        """Initialize the instance."""
        super().__init__(bus, cache, plugin_dir, config)
        self._ms_config: MicrosoftConfig = (
            MicrosoftConfig.from_config_manager(config)
        )
        logger.info(
            "[MicrosoftStore] %s",
            self._ms_config.describe(),
        )
        self._config_manager = config
        self._cached_available = False
        #: The last xCloud session market (the Xbox PC market hint).
        self._last_market: str | None = None
        self._shortcut_service = shortcut_service
        self._edge = edge_browser
        self._subscription_service = subscription_service
        self._tokens = MicrosoftTokenManager(
            config=self._ms_config,
            locale_fn=lambda: get_unifideck_locale(
                self._config_manager,
            ),
            bus=bus,
        )
        self._build_token_users(bus)
        self._catalog = MicrosoftCatalogReader(
            config=self._ms_config,
            config_manager=self._config_manager,
        )
        self._build_xbox_pc_parts(bus, cache)
        # Auth is late-bound : at boot ``browser_monitor`` is
        # ``None`` (auto-discovery doesn't see the service
        # container yet). The injector sets ``_browser_monitor``
        # post-discovery and calls
        # ``_rebuild_auth_after_injection``.
        self._browser_monitor = browser_monitor
        self._auth: MicrosoftBrowserAuth | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._rebuild_auth_after_injection()
    def _build_token_users(self, bus: EventBus) -> None:
        """The parts that share the token manager: sign-in health, the
        device-code sign-in, and the ownership reader."""
        self._health = SessionHealth(bus, self._tokens)
        self._ownership = MicrosoftOwnershipReader(self._tokens)
        # A getter, not ``self._edge``: Edge is injected after __init__.
        self._device_auth = MicrosoftDeviceAuth(
            bus, self._tokens, self._ms_config, lambda: self._edge,
        )
        # Xbox PC (Xodus). Its sign-in and ours share the Edge auth window,
        # so each refuses while the other runs.
        self._xbox_pc = XboxPc(
            self._config_manager, lambda: self._edge, self._tokens,
            lambda: self._device_auth.running, bus,
            xcloud_signed_in=lambda: self._cached_available,
            market_hint=lambda: self._last_market,
        )
        # The one sign-in: with Xbox PC on, the Microsoft ticket comes from
        # the Xodus credential. Its sign-out drops the cached ticket.
        ticket = XodusTicket(self._xbox_pc.cli, self._xbox_pc.enabled)
        self._tokens.xodus_ticket = ticket
        self._xbox_pc.sign_out_listeners.append(ticket.forget)

    def _build_xbox_pc_parts(self, bus: EventBus, cache: CacheManager) -> None:
        """The PC library and the install hooks, over the one ``XboxPc``."""
        self._pc_library = XboxPcLibrary(
            self._config_manager, self._xbox_pc, self._xbox_pc.cli, cache=cache, bus=bus,
        )
        self._xbox_pc.sign_out_listeners.append(self._pc_library.clear)
        self._xbox_hooks = XboxPcStoreHooks(
            bus, self._xbox_pc, self._xbox_pc.cli, self._pc_library.title_for,
        )

    @property
    def xbox_pc(self) -> XboxPc:
        """The Xbox PC facade (read by ``XboxPcRPCMixin``)."""
        return self._xbox_pc

    def _build_auth_flow(
        self, orchestrator: AuthOrchestrator,
    ) -> MicrosoftBrowserAuth:
        """Microsoft's half of ``BrowserAuthRebuildMixin``."""
        return MicrosoftBrowserAuth(
            bus=self._bus,
            orchestrator=orchestrator,
            tokens=self._tokens,
            config=self._ms_config,
            config_manager=self._config_manager,
        )

    # ── Background token refresh ─────────────────────────────────
    #
    # Refresh was previously ONLY on-demand, inside get_library() — a
    # token nobody happened to fetch a library with (e.g. sitting behind
    # an unrelated bug, or just because the user hadn't opened the
    # xCloud tab in a while) never got refreshed at all, and Microsoft's
    # server-side handling of a long-unused refresh_token is opaque to
    # us. This is a best-effort attempt to keep the session alive by
    # exercising it periodically instead — deliberately conservative
    # (well under the 2400s/40min access-token staleness threshold, so
    # it never fires more than once per cycle) so the real-world effect
    # can be observed rather than assumed.
    TOKEN_POLL_INTERVAL_SECONDS = 1800

    def start_token_refresh_polling(self) -> None:
        """Start the periodic background token-refresh loop."""
        if self._poll_task is not None:
            return
        self._poll_task = asyncio.create_task(
            self._token_poll_loop(), name="microsoft-token-refresh",
        )
        logger.info(
            "[MicrosoftStore] background token refresh started (every %ds)",
            self.TOKEN_POLL_INTERVAL_SECONDS,
        )

    async def stop_token_refresh_polling(self) -> None:
        """Cancel the background token-refresh loop."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

    async def shutdown(self) -> None:
        """Plugin unload: stop the token-refresh loop, any sign-in and Xbox PC."""
        await self.stop_token_refresh_polling()
        await self._device_auth.cancel()
        await self._xbox_pc.shutdown()

    async def _token_poll_loop(self) -> None:
        """Internal loop: refresh (if stale) every poll interval.

        It checks the sign-in once at start. Until then the cached answer
        reads "signed out", and the Xbox PC install refused with "connect
        Microsoft first" after every restart (ISSUES.md I-036).
        """
        try:
            await self.is_available()
        except Exception:
            logger.exception("[MicrosoftStore] startup sign-in check failed")
        while True:
            try:
                await asyncio.sleep(self.TOKEN_POLL_INTERVAL_SECONDS)
                if not await self._tokens.load():
                    continue  # not signed in -- nothing to refresh
                state = await self._tokens.refresh_if_stale()
                if state is TokenState.DEAD:
                    await self._health.on_session_dead(
                        self._tokens.last_token_error,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[MicrosoftStore] token refresh poll cycle error",
                )

    async def is_available(self) -> bool:
        """Check whether available.

        Validates the token, not just its presence: a stored
        ``refresh_token`` that Microsoft has actually revoked/expired
        used to still report ``True`` here (only ``get_library`` did a
        real refresh), so the QAM could show "logged in" for a session
        that was already dead until the next sync exposed it.
        ``refresh_if_stale`` is cheap when the access token isn't
        actually due for renewal (an in-memory age check, no network
        call), so this stays fast on the common path.

        Only a ``DEAD`` answer signs the user out. ``TRANSIENT`` (no
        network, a Microsoft outage) keeps the session: the user is still
        signed in, Microsoft just could not be asked. Treating the two
        alike signed out every Deck that refreshed while offline.
        """
        if not self._ms_config.is_valid():
            return self._set_available(False)
        loaded = await self._tokens.load()
        if not loaded:
            if self._tokens.client_mismatch:
                await self._health.on_client_changed()
            return self._set_available(False)
        state = await self._tokens.refresh_if_stale()
        if state is TokenState.DEAD:
            await self._health.on_session_dead(self._tokens.last_token_error)
            return self._set_available(False)
        return self._set_available(True)

    def _set_available(self, available: bool) -> bool:
        """Cache the answer; the Xbox PC status shows it, so tell it on a flip.

        A flip to signed in also brings the Xbox PC components up to date
        (``auto_components``): this is the first moment the backend knows
        Microsoft is connected, which the frontend's boot step cannot wait for.
        """
        if available != self._cached_available:
            self._cached_available = available
            self._xbox_pc.notify()
            if available:
                auto_components.schedule(self._xbox_pc)
        return available

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Start the sign-in for the configured flow (see ``MicrosoftConfig``).

        Both flows run in the Edge auth window, because Xbox Cloud Gaming
        streams on that profile's Microsoft cookies. Only the legacy flow
        clears them first: it needs a fresh login form to capture a code
        from, while the device-code flow is happy to reuse the session.
        """
        if self._xbox_pc.sign_in_running:
            return AuthResult(success=False, error="auth_in_progress", store="microsoft")
        if self._edge is None or not self._edge.is_installed:
            logger.info(
                "[MicrosoftStore] Edge not installed — "
                "prompting user to install",
            )
            return AuthResult(
                success=False,
                error="edge_not_installed",
                store="microsoft",
                url=None,
                metadata={"needs_2fa": False},
            )
        EdgeBrowser.ensure_controller_permissions()
        if self._xbox_pc.enabled():
            # One sign-in: with Xbox PC on, connecting Microsoft is the Xbox
            # PC sign-in, and xCloud runs on its ticket (``xodus/ticket.py``).
            return await single_signin.start_connect(self._xbox_pc, self._bus)
        if self._ms_config.uses_device_code:
            return cast("AuthResult", await self._device_auth.start_auth())
        if self._auth is None:
            return AuthResult(
                success=False,
                error="auth_not_configured",
                store="microsoft",
            )
        self._edge.clear_store_cookies("microsoft.com")
        self._edge.clear_store_cookies("live.com")
        return cast("AuthResult", await self._auth.start_auth())

    async def cancel_auth(self) -> Result:
        """Abandon a device-code sign-in in progress (no event)."""
        await self._device_auth.cancel()
        return Result(success=True)

    async def get_owned_products(self) -> OwnedFetch:
        """Every Xbox product the account owns (Steam Store ribbon only).

        Independent of the Game Pass gate: purchases are owned with or
        without a subscription. Needs xbox.com's client, because
        Collections answers the legacy login.live.com client with an
        empty list (``ownership.collections_api``).
        """
        if not self._ms_config.uses_device_code:
            return OwnedFetch.failed("the legacy Microsoft sign-in cannot read purchases")
        if not await self.is_available():
            return OwnedFetch.failed("not signed in to Microsoft")
        return await self._ownership.fetch()

    async def complete_auth(
        self, code: str = "", **kwargs: Any,
    ) -> AuthResult:
        """Complete auth."""
        if await self.is_available():
            return AuthResult(success=True, store="microsoft")
        return AuthResult(
            success=False,
            error="not_authenticated",
            store="microsoft",
        )
    async def logout(self) -> Result:
        """Logout. Stops an Xbox PC sign-in first: it uses the Edge we kill."""
        await self._xbox_pc.on_microsoft_logout()
        await self._device_auth.cancel()
        if self._auth is not None:
            result = await self._auth.logout()
        else:
            await self._tokens.clear()
            await self._bus.emit(
                Events.STORE_LOGOUT, store="microsoft",
            )
            result = Result(success=True)
        await remove_url_file(MS_AUTH_URL_FILE)
        if self._edge is not None:
            try:
                self._edge.kill()
                self._edge.clear_cookies()
                EdgeBrowser.clear_profile_data()
            except Exception as e:
                logger.warning(
                    "[MicrosoftStore] Edge cleanup error: "
                    "%s", e,
                )
        self._set_available(False)
        return result

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """The xCloud titles plus the Xbox PC titles, or ``None`` when unread.

        A list, even an empty one, is authoritative: the post-sync
        reconcile deletes every Microsoft shortcut missing from it. So every
        "could not read" path returns ``None`` (keep the shortcuts), and
        ``[]`` is reserved for Microsoft saying the account has no
        subscription (see ``library_gate``) and no PC rows remaining. The
        PC rows come from ``pc_library``, which needs the xCloud sign-in.
        """
        started = self._pc_library.now()
        read = await self._read_xcloud()
        if read.verdict is GateVerdict.EMPTY:
            rows = await self._pc_library.compose([], market=self._last_market, started=started)
            if has_pc_rows(rows):
                await self._pc_library.notify_once(PC_ONLY)
            return rows
        if read.games is None:
            _cancel(read.pending)
            return None
        return await self._pc_library.compose(
            read.games, market=read.market, pending_read=read.pending, started=started,
        )

    async def _read_xcloud(self) -> _XcloudRead:
        """The entitled xCloud titles (``games`` None when unread).

        1. Tokens must be loaded + not stale.
        2. The subscription gate must report an active tier. This also
           captures the xCloud session (gsToken + regions) for the catalog.
        3. The catalog fetches ``/v2/titles`` and keeps the entitled titles
           (Game Pass + owned Play Anywhere).

        The Xbox PC read starts as soon as the session market is known.
        """
        if not await self.is_available():
            logger.info("[MicrosoftStore] not authenticated; library not read")
            return _XcloudRead(None)
        state = await self._tokens.refresh_if_stale()
        if state is TokenState.DEAD:
            await self._health.on_session_dead(self._tokens.last_token_error)
            return _XcloudRead(None)
        if state is TokenState.TRANSIENT:
            logger.warning(
                "[MicrosoftStore] could not refresh the Microsoft sign-in "
                "(offline?); library not read, xCloud shortcuts kept",
            )
            return _XcloudRead(None)
        verdict = await check_subscription_gate(
            self._subscription_service, self._tokens, self._bus,
        )
        if verdict in (GateVerdict.EMPTY, GateVerdict.UNREADABLE):
            return _XcloudRead(None, verdict)
        session = await self._xcloud_session()
        if session is None:
            return _XcloudRead(None, verdict)
        self._last_market = session.market
        pending = self._pc_library.start_read(session.market)
        try:
            games = await self._catalog.fetch_games(session)
        except Exception:
            logger.exception("[MicrosoftStore] get_library failed")
            _cancel(pending)
            return _XcloudRead(None, verdict)
        if games is not None and self._catalog.has_subscription is False:
            # The Game Pass part of the library is what was skipped: say so,
            # or a lapsed subscriber sees their shortcuts vanish unexplained.
            logger.info(
                "[MicrosoftStore] no Game Pass plan on this account — "
                "synced %d title(s) it can stream for free", len(games),
            )
            await self._bus.emit(
                Events.SYNC_SKIPPED, store="microsoft", reason="free_titles_only",
            )
        return _XcloudRead(games, verdict, session.market, pending)

    async def _xcloud_session(self) -> Any:
        """The xCloud session with a gsToken, or None (logged)."""
        if self._subscription_service is None:
            logger.warning(
                "[MicrosoftStore] no subscription_service injected "
                "— cannot get xCloud session; library not read",
            )
            return None
        session = await self._subscription_service.get_session(self._tokens)
        if session is None or not session.gs_token:
            logger.warning(
                "[MicrosoftStore] no usable xCloud session "
                "(no gsToken); library not read",
            )
            return None
        return session

    # ── Install lifecycle: Xbox PC titles, through Xodus ─────────────────
    #
    # A stream-only title (no Xbox PC facts, no record) is refused with
    # ``not_supported`` by the hooks, never a phantom success.

    async def install_precondition(self, game_id: str) -> str | None:
        """Why ``game_id`` cannot be queued yet (``xbox_pc_*``), or None."""
        return await self._xbox_hooks.precondition(game_id)

    async def install_game(
        self, game_id: str, base_path: str | None = None, progress_cb: Any = None, **kwargs: Any,
    ) -> InstallResult:
        """Install an Xbox PC title through Xodus."""
        return await self._xbox_hooks.install(game_id, base_path, progress_cb)

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Uninstall an Xbox PC title. Not gated by the toggle or the sign-in."""
        return await self._xbox_hooks.uninstall(
            game_id, delete_prefix=bool(kwargs.get("delete_prefix")),
        )

    async def update_game(
        self, game_id: str, progress_cb: Any = None, **kwargs: Any,
    ) -> InstallResult:
        """Update an installed Xbox PC title."""
        return await self._xbox_hooks.update(game_id, progress_cb)

    async def check_for_updates(self) -> list[str]:
        """The installed Xbox PC titles with a newer package."""
        return await self._xbox_hooks.check_for_updates()

    async def get_game_size(self, game_id: str) -> int | None:
        """The planned download size of an Xbox PC title, or None."""
        return self._pc_library.size_for(game_id)

    async def get_installed_path(self, game_id: str) -> str | None:
        """The record's ``installRoot``, or None."""
        return await asyncio.to_thread(self._pc_library.installed_path, game_id)

    async def find_installed_exe(self, install_path: str, game_id: str) -> str | None:
        """The record's absolute exe, so nothing guesses one."""
        return await asyncio.to_thread(self._pc_library.installed_exe, game_id)

    async def install_edge(self) -> Result:
        """Install edge."""
        if self._edge is None:
            return Result(
                success=False,
                error="edge_browser_not_configured",
            )
        raw = await self._edge.install()
        return Result(
            success=bool(raw.get("success")),
            error=raw.get("error"),
        )
    def is_edge_installed(self) -> bool:
        """Check whether edge installed."""
        return (
            self._edge is not None
            and self._edge.is_installed
        )
