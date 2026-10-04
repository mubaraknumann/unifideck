from __future__ import annotations

import asyncio
import logging
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
from .session_health import SessionHealth
from .tokens import MicrosoftTokenManager, TokenState

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.services.microsoft_subscription import MicrosoftSubscriptionService
logger = logging.getLogger(__name__)
class MicrosoftStore(BrowserAuthRebuildMixin, StoreBase):
    """Microsoft store."""
    store_info = StoreInfo(
        name="microsoft",
        display_name="Microsoft",
        auth_method="oauth",
        icon_asset="microsoft.png",
        supports_install=False,
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
        """Plugin unload: stop the token-refresh loop and any sign-in poll."""
        await self.stop_token_refresh_polling()
        await self._device_auth.cancel()

    async def _token_poll_loop(self) -> None:
        """Internal loop: refresh (if stale) every poll interval."""
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
            self._cached_available = False
            return False
        loaded = await self._tokens.load()
        if not loaded:
            if self._tokens.client_mismatch:
                await self._health.on_client_changed()
            self._cached_available = False
            return False
        state = await self._tokens.refresh_if_stale()
        if state is TokenState.DEAD:
            await self._health.on_session_dead(self._tokens.last_token_error)
            self._cached_available = False
            return False
        self._cached_available = True
        return True

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Start the sign-in for the configured flow (see ``MicrosoftConfig``).

        Both flows run in the Edge auth window, because Xbox Cloud Gaming
        streams on that profile's Microsoft cookies. Only the legacy flow
        clears them first: it needs a fresh login form to capture a code
        from, while the device-code flow is happy to reuse the session.
        """
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
        """Logout."""
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
        return result

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """The entitled xCloud titles, or ``None`` when they can't be read.

        A list, even an empty one, is authoritative: the post-sync
        reconcile deletes every xCloud shortcut missing from it. So every
        "could not read" path returns ``None`` (keep the shortcuts), and
        ``[]`` is reserved for Microsoft saying the account has no
        subscription (see ``library_gate``).

        Flow:
          1. Tokens must be loaded + not stale.
          2. The subscription gate must report an active tier — this
             also captures the xCloud session (gsToken + regions)
             into the service for catalog reuse.
          3. The catalog fetches ``/v2/titles`` from the regional core
             endpoint and keeps the entitled titles (Game Pass + owned
             Play Anywhere); ``hasEntitlement`` encodes tier access
             server-side.
        """
        if not await self.is_available():
            logger.info(
                "[MicrosoftStore] not authenticated; library not read",
            )
            return None
        state = await self._tokens.refresh_if_stale()
        if state is TokenState.DEAD:
            await self._health.on_session_dead(self._tokens.last_token_error)
            return None
        if state is TokenState.TRANSIENT:
            logger.warning(
                "[MicrosoftStore] could not refresh the Microsoft sign-in "
                "(offline?); library not read, xCloud shortcuts kept",
            )
            return None
        verdict = await check_subscription_gate(
            self._subscription_service, self._tokens, self._bus,
        )
        if verdict is GateVerdict.EMPTY:
            return []
        if verdict is GateVerdict.UNREADABLE:
            return None
        if self._subscription_service is None:
            logger.warning(
                "[MicrosoftStore] no subscription_service injected "
                "— cannot get xCloud session; library not read",
            )
            return None
        session = await self._subscription_service.get_session(
            self._tokens,
        )
        if session is None or not session.gs_token:
            logger.warning(
                "[MicrosoftStore] no usable xCloud session "
                "(no gsToken); library not read",
            )
            return None
        try:
            return await self._catalog.fetch_games(session)
        except Exception:
            logger.exception("[MicrosoftStore] get_library failed")
            return None

    # ── Install lifecycle: refused, not faked ────────────────────────────
    #
    # This store's titles are Xbox Cloud Gaming streams. There is nothing to
    # download, nothing on disk, and nothing to update — so the three
    # ``StoreBase`` install hooks the ABC requires cannot be satisfied and
    # say so.
    #
    # They used to return ``success=True`` and do nothing, which is a
    # phantom success: the caller is told a game was installed (or
    # uninstalled) that never was.
    #
    # Install and update were unreachable — two independent guards, neither
    # of them ``store_info.supports_install``, which gates nothing at all
    # because it has no readers (audit register item 26). ``usePlaySection``
    # short-circuits any game carrying the ``xcloud`` tag to its own play
    # state before the not-installed branch, so the Install button never
    # mounts; and ``DownloadWorker._execute_install`` carried its own
    # store-name rejection ahead of the dispatch. That second guard is gone
    # now, because a refusal here does its job better (see the note there).
    #
    # **Uninstall never had a backend guard**, only the frontend's
    # ``is_installed`` gate — so it is the one of the three that a caller
    # could actually reach, and the one whose ``success=True`` would have
    # let a shortcut flip out of an install state it never held.
    #
    # The obvious next feature for this store — PC Game Pass, i.e. titles
    # that really do install — is exactly a Microsoft game *without* the
    # ``xcloud`` tag, which is what makes refusing here worth doing: it
    # arrives as a visible failure instead of a phantom success.
    _NOT_SUPPORTED = "not_supported"

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: Any = None,
        **kwargs: Any,
    ) -> InstallResult:
        """Refuse: xCloud titles stream, so there is nothing to install."""
        logger.warning(
            "[MicrosoftInstall] refusing install for game_id=%s — xCloud "
            "titles stream and have no local install", game_id,
        )
        return InstallResult(
            success=False,
            store="microsoft",
            game_id=game_id,
            install_path=None,
            error=self._NOT_SUPPORTED,
            error_code=self._NOT_SUPPORTED,
        )
    async def uninstall_game(
        self, game_id: str, **kwargs: Any,
    ) -> Result:
        """Refuse: nothing was installed, so nothing can be reclaimed.

        Reporting success here told the caller a game had been uninstalled
        and let the shortcut flip out of its installed state, both untrue.
        """
        return Result(
            success=False,
            store="microsoft",
            error=self._NOT_SUPPORTED,
            error_code=self._NOT_SUPPORTED,
        )

    async def update_game(
        self,
        game_id: str,
        progress_cb: Any = None,
        **kwargs: Any,
    ) -> InstallResult:
        """Refuse: the stream is always current, so there is no update."""
        return InstallResult(
            success=False,
            store="microsoft",
            game_id=game_id,
            error=self._NOT_SUPPORTED,
            error_code=self._NOT_SUPPORTED,
        )
    async def check_for_updates(self) -> list[str]:
        """Check for updates."""
        return []
    async def get_game_size(
        self, game_id: str,
    ) -> int | None:
        """Get game size."""
        return None
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
