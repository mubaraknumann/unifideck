"""LunaStore: the Amazon Luna store (cloud-streaming only).

This is the Luna analogue of ``microsoft_store.py`` (xCloud). It subclasses
:class:`StoreBase` with the same ``(bus, cache, plugin_dir, config)``
constructor the auto-discovery registry instantiates, and composes the Luna
components implemented by the earlier tasks:

* :class:`LunaConfig` — the typed view over ``stores.luna`` config;
* :class:`LunaBrowser` — the thin config over the reused ``EdgeBrowser``;
* :class:`LunaSessionMonitor` — the cookie-store source of truth that
  **replaces** the Microsoft token manager (Luna has no tokens);
* :class:`LunaAuth` — drives the interactive Amazon sign-in;
* :class:`LunaCatalog` over :class:`~unifideck.stores.luna.luna_api.LunaApiClient`
  — the catalog read through the Luna web API and the
  catalog→library-entry reconciliation (built per sync for the configured
  host's region, injectable for tests);
* the play-activator **factory** (not an instance): it runs in the launcher
  subprocess and is constructible from :class:`LunaConfig` alone, so the
  store only needs to hand the factory down.

The method surface is split across focused sibling mixin modules so no single
file exceeds the volumetry cap — ``get_library`` + region lookup
(``luna_store_library_sync``), the retry/recovery loop + Steam Input request
(``luna_store_recovery``), and the streaming-only install refusals + browser
prerequisite (``luna_store_install``); the shared constants/status vocabulary
live in ``luna_store_constants``. This module keeps the constructor, the
availability/auth surface, lifecycle, and status helpers, and re-exports the
public names so ``from unifideck.stores.luna.luna_store import <name>`` is
unchanged.

``Store_Status`` is reported to the frontend through the ``metadata`` payload
of the :class:`Result` / :class:`AuthResult` each operation returns, keyed
``store_status`` (mirroring how Microsoft surfaces ``needs_2fa`` etc.). The
full value set the design mandates lives in :class:`LunaStatus`.

``LunaStore`` is discoverable with no registry edit: ``StoreRegistry
.auto_discover`` matches ``stores/luna/luna_store.py`` and instantiates the
single ``StoreBase`` subclass exporting ``store_info`` — exactly how Microsoft
and the other stores register.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.auth.edge_browser import EdgeBrowser
from unifideck.core.types import AuthResult, Events, Result, StoreInfo
from unifideck.stores.luna.luna_auth import LunaAuth
from unifideck.stores.luna.luna_browser import LunaBrowser
from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_play_activator import LunaPlayActivator
from unifideck.stores.luna.luna_session_monitor import LunaSessionMonitor
from unifideck.stores.luna.luna_store_constants import (
    _RETRY_BACKOFF_SECONDS,
    BROWSER_INSTALL_BUDGET_SECONDS,
    INSTALLATION_FAILED,
    LUNA_STORE,
    MAX_ATTEMPTS,
    MAX_RETRIES,
    STATUS_KEY,
    STEAM_INPUT_LAYOUT_BUDGET_SECONDS,
    LunaOperationError,
    LunaStatus,
)
from unifideck.stores.luna.luna_store_install import _LunaInstallMixin
from unifideck.stores.luna.luna_store_library_sync import _LunaLibrarySyncMixin
from unifideck.stores.luna.luna_store_recovery import _LunaRecoveryMixin
from unifideck.stores.shared.store_base import StoreBase

if TYPE_CHECKING:
    from collections.abc import Callable

    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.luna.luna_catalog import LunaCatalog

logger = logging.getLogger(__name__)

__all__ = [
    "BROWSER_INSTALL_BUDGET_SECONDS",
    "INSTALLATION_FAILED",
    "LUNA_STORE",
    "MAX_ATTEMPTS",
    "MAX_RETRIES",
    "STATUS_KEY",
    "STEAM_INPUT_LAYOUT_BUDGET_SECONDS",
    "_RETRY_BACKOFF_SECONDS",
    "LunaOperationError",
    "LunaStatus",
    "LunaStore",
]


class LunaStore(
    _LunaLibrarySyncMixin,
    _LunaRecoveryMixin,
    _LunaInstallMixin,
    StoreBase,
):
    """Amazon Luna store (cloud-streaming only).

    Composition mirrors ``MicrosoftStore.__init__`` but swaps the token
    manager for :class:`LunaSessionMonitor` and adds the activator
    **factory** instead of an OAuth subscription service. Every collaborator
    defaults to its real implementation and is injectable so the whole store
    is drivable in tests without a browser or a real config.

    The library/region, retry/recovery, and install surfaces live in the
    mixins this class combines; see each mixin's module for those methods.
    """

    store_info = StoreInfo(
        name="luna",
        display_name="Amazon Luna",
        auth_method="browser-session",
        icon_asset="luna.png",
        supports_install=False,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
        browser: LunaBrowser | None = None,
        shortcut_service: Any = None,
        edge_browser: EdgeBrowser | None = None,
        session: LunaSessionMonitor | None = None,
        catalog: LunaCatalog | None = None,
        auth: LunaAuth | None = None,
        activator_factory: Callable[[LunaConfig], LunaPlayActivator] | None = None,
    ) -> None:
        """Compose the Luna store over its collaborators (see module docstring)."""
        super().__init__(bus, cache, plugin_dir, config)
        self._luna_config: LunaConfig = LunaConfig.from_config_manager(config)
        logger.info("[LunaStore] %s", self._luna_config.describe())
        self._config_manager = config
        self._shortcut_service = shortcut_service
        self._edge = edge_browser or EdgeBrowser()
        self._browser = browser or LunaBrowser(self._luna_config, edge=self._edge)
        # LunaSessionMonitor replaces the Microsoft token manager: no tokens,
        # no refresh — "signed in" is non-expired Amazon cookies in the Luna
        # profile.
        self._session = session or LunaSessionMonitor(self._luna_config)
        # ``None`` = build the API-backed catalog per sync for the configured
        # host's region (see ``_LunaLibrarySyncMixin._catalog_for_region``).
        self._catalog = catalog
        self._auth = auth or LunaAuth(
            bus, self._browser, self._session, self._luna_config,
        )
        # The activator is constructed per launch in the launcher subprocess;
        # the store only carries its factory so a test can swap it without a
        # real CDP browser.
        self._activator_factory = activator_factory or LunaPlayActivator
        # The last status the store settled on while NOT retrying — the
        # "stable connection state" Req 11.1 restores to after a failed
        # operation. Starts at not_connected (no session proven yet) and is
        # advanced by every operation that reaches a terminal, non-recovery
        # outcome.
        self._last_stable_status: str = LunaStatus.NOT_CONNECTED

    # ── Availability ─────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """Whether Luna is *connected* right now (Req 1.4).

        ``check_store_status`` renders this as the store's login badge, so it
        must mean "signed in", exactly as ``MicrosoftStore.is_available``
        does — not merely "a browser is installed". Luna is connected only
        when the streaming browser is present (the feature rides the CDP
        spoof, Req 9.1) **and** the Luna profile holds a usable Amazon
        session (:class:`LunaSessionMonitor`). The browser-installed-only
        gate stays available separately via :meth:`is_browser_installed`
        (the needs-browser install prompt) and ``start_auth``.
        """
        if not self._edge.is_installed:
            self._cached_available = False
            logger.info(
                "[LunaStore] no supported browser installed — needs_browser",
            )
            return False
        usable = self._session.is_usable()
        self._cached_available = usable
        if not usable:
            logger.info(
                "[LunaStore] browser present but no usable Luna session — "
                "not connected",
            )
        return usable

    # ── Auth ──────────────────────────────────────────────────────────

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Prepare the interactive Luna sign-in (Req 1).

        In Gaming Mode a window from a process Steam did not launch has no
        gamescope session and never renders, so the actual kiosk sign-in runs
        in the ``unifideck-launcher`` process that Steam starts via a
        temporary shortcut (``UNIFIDECK_LUNA_ACTION=auth`` →
        ``launcher/flows/luna_auth.py``), exactly like the OAuth stores. This
        backend ``start`` therefore only *prepares* and reports ``pending``;
        it does **not** spawn the browser here (that would open an invisible
        second window). The frontend ``AuthDispatcher`` sees ``pending`` and
        launches the Luna auth shortcut.

        Needs-browser short-circuits before anything (Req 9.1). An already
        usable session reports connected immediately (no shortcut needed).
        """
        if not self._edge.is_installed:
            logger.info("[LunaStore] start_auth: browser not installed")
            return self._auth_result(
                success=False, error=LunaStatus.NEEDS_BROWSER,
                status=LunaStatus.NEEDS_BROWSER,
            )
        if self._session.is_usable():
            logger.info("[LunaStore] start_auth: already connected")
            return self._auth_result(
                success=True, status=LunaStatus.CONNECTED,
            )
        # Prepare only; the launcher's gamescope-visible flow does the sign-in.
        logger.info(
            "[LunaStore] start_auth: pending — sign-in runs via Steam shortcut",
        )
        return AuthResult(
            success=True, store=LUNA_STORE, tokens_cached=False,
            metadata={STATUS_KEY: LunaStatus.NOT_CONNECTED, "pending": True},
        )

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """Confirm a usable Luna session exists (Req 1.3–1.6, 2).

        Unlike OAuth there is nothing to exchange: "complete" means the
        interactive sign-in left non-expired Amazon cookies in the Luna
        profile, which :class:`LunaSessionMonitor` is the source of truth for.
        A readable-but-empty/expired store is ``session_expired``; an
        unreadable store is ``session_unknown`` with a retry (Req 2.6).
        """
        state = self._session.read_session()
        if state.status == "usable":
            return self._auth_result(
                success=True, status=LunaStatus.CONNECTED,
            )
        status = (
            LunaStatus.SESSION_UNKNOWN if state.status == "unknown"
            else LunaStatus.SESSION_EXPIRED
        )
        return self._auth_result(
            success=False, error=status, status=status,
        )

    async def logout(self) -> Result:
        """Clear the Luna profile's Amazon cookies (Req 8.7–8.9).

        The kiosk is killed first (it owns the cookie DB), then the clear goes
        through :class:`LunaSessionMonitor`, which uses ``cookie_writer.py``.
        A clear failure retains the previously persisted session state and
        reports it (Req 8.8); success reports ``not_connected`` and emits
        ``STORE_LOGOUT`` so the frontend and sync layer react (Req 8.7).
        """
        try:
            self._browser.kill()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[LunaStore] browser kill during logout: %s", exc)
        try:
            cleared = self._session.clear_cookies()
        except Exception as exc:
            logger.warning("[LunaStore] logout clear failed: %s", exc)
            cleared = False
        if not cleared:
            return Result(
                success=False, store=LUNA_STORE, error="logout_failed",
                metadata={STATUS_KEY: LunaStatus.CONNECTED},
            )
        await self._bus.emit(Events.STORE_LOGOUT, store=LUNA_STORE)
        return Result(
            success=True, store=LUNA_STORE,
            metadata={STATUS_KEY: LunaStatus.NOT_CONNECTED},
        )

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Plugin unload: make sure no Luna kiosk is left running."""
        try:
            self._browser.kill()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("[LunaStore] shutdown browser kill error: %s", exc)

    # ── Status helpers ──────────────────────────────────────────────────

    @staticmethod
    def _auth_error_to_status(error: str | None) -> str:
        """Map a :class:`LunaAuth` error reason to a :class:`LunaStatus`.

        ``LunaAuth`` already uses the ``platform_spoofing_failure`` wire value
        for its spoof-config error; every other not-connected reason (early
        close, no cookies, timeout, profile permission/not-persisted, no
        browser) collapses to ``not_connected`` for the panel — the specific
        reason stays on ``AuthResult.error`` for diagnostics.
        """
        if error == LunaStatus.PLATFORM_SPOOFING_FAILURE:
            return LunaStatus.PLATFORM_SPOOFING_FAILURE
        return LunaStatus.NOT_CONNECTED

    def _auth_result(
        self, *, success: bool, status: str, error: str | None = None,
    ) -> AuthResult:
        """Build an :class:`AuthResult` carrying its :class:`LunaStatus`."""
        return AuthResult(
            success=success, store=LUNA_STORE, error=error,
            tokens_cached=success, metadata={STATUS_KEY: status},
        )

    @staticmethod
    def _set_status(result: AuthResult, status: str) -> None:
        """Stamp ``status`` onto an existing result's ``metadata`` payload."""
        result.metadata[STATUS_KEY] = status
