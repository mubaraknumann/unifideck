"""Amazon Games store — Layer-4 implementation of the unified store interface.

``AmazonStore`` is the orchestration class that wires every Amazon
sub-component together and exposes them through the ``StoreBase``
contract. It owns one instance each of :

* ``AmazonAuthFlow`` — embedded-browser OAuth flow.
* ``AmazonLibraryReader`` — owned-games library reader.
* ``AmazonInstaller`` — install/uninstall pipeline.
* ``AmazonUpdateChecker`` — periodic update polling.

Amazon Games uses ``nile`` (a community CLI mirror of the Amazon
Games launcher) for the actual downloads ; the store class is the
high-level coordinator that orchestrates token lifecycle, library
fetch, install pipeline, and update detection.

Implements the standard ``StoreBase`` API : ``store_info``,
``is_authed``, ``auth``, ``logout``, ``library``, ``install``,
``uninstall``, ``launch``, etc. — each method delegates to the
appropriate sub-component.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unifideck.auth.browser import OAuthBrowserMonitor
from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.core.types import (
    AuthResult,
    CLITool,
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
from unifideck.stores.shared.cli_credentials import read_cli_user_json
from unifideck.stores.shared.install_status import merge_install_status
from unifideck.stores.shared.installed_path import install_path_from_record
from unifideck.stores.shared.store_base import StoreBase
from unifideck.utils.config_helpers import get_cfg

from .amazon_auth import AmazonAuthFlow
from .amazon_install import AmazonInstaller, ProgressCallback
from .amazon_library import AmazonLibraryReader
from .amazon_updates import AmazonUpdateChecker

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus
logger = logging.getLogger(__name__)

class AmazonStore(BrowserAuthRebuildMixin, StoreBase):
    """Amazon store."""

    store_info = StoreInfo(
        name="amazon",
        display_name="Amazon Games",
        auth_method="oauth",
        icon_asset="amazon.png",
        supports_install=True,
    )
    CLI_TOOL = CLITool(
        name="nile",
        search_paths=["bin/nile"],
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
        browser_monitor: OAuthBrowserMonitor | None = None,
        shortcut_service: ShortcutService | None = None,
    ) -> None:
        """Initialize the instance."""
        super().__init__(bus, cache, plugin_dir, config)
        self.cli_path: str | None = self._find_binary(self.CLI_TOOL)
        if not self.cli_path:
            logger.warning("[AmazonStore] nile binary not found")
        self._shortcut_service = shortcut_service
        amazon_cfg = config.get("stores.amazon") if config else None
        if amazon_cfg is None:
            raise KeyError(
                "config.stores.amazon is required",
            )
        self._library = AmazonLibraryReader(
            config_dir=amazon_cfg["nile_config_dir"],
        )
        self._installer = AmazonInstaller(
            bus=bus,
            cli_path=self.cli_path,
            library=self._library,
            find_exe=self._find_exe,
            default_install_root=amazon_cfg["default_install_root"],
        )
        self._updates = AmazonUpdateChecker(
            bus=bus,
            cli_path=self.cli_path,
            library=self._library,
            list_updates_timeout=amazon_cfg["list_updates_timeout_seconds"],
            get_size_timeout=amazon_cfg["get_size_timeout_seconds"],
            default_install_root=amazon_cfg["default_install_root"],
        )
        # Auth orchestrator + flow are built lazily : at boot the
        # `browser_monitor` is `None` (auto-discovery doesn't have
        # the service container yet). `store_injector` sets
        # `_browser_monitor` post-discovery and then calls
        # `_rebuild_auth_after_injection` so the flow is wired
        # against the just-injected monitor.
        self._browser_monitor = browser_monitor
        self._amazon_cfg = amazon_cfg
        self._auth: AmazonAuthFlow | None = None
        self._rebuild_auth_after_injection()

    def _build_auth_flow(self, orchestrator: AuthOrchestrator) -> AmazonAuthFlow:
        """Amazon's half of ``BrowserAuthRebuildMixin``."""
        return AmazonAuthFlow(
            bus=self._bus,
            orchestrator=orchestrator,
            cli_path=self.cli_path,
            success_markers=self._amazon_cfg[
                "nile_register_success_markers"
            ],
        )

    async def is_available(self) -> bool:
        """Check whether available."""
        ok = self._check_nile_authenticated()
        self._cached_available = ok
        return ok

    def _check_nile_authenticated(self) -> bool:
        """Check NILE authenticated.

        Shares its reader with Epic (``stores/shared/cli_credentials``),
        including the 0600 hardening that also covers
        ``quarantine_corrupt_user_file``'s ``.corrupt-*`` copies — a rename
        preserves the original mode, and those copies still hold live
        credentials.

        One behaviour gained by sharing: the object check. This copy called
        ``data.get`` on whatever ``json.load`` returned, so a ``user.json``
        holding a JSON *array* raised ``AttributeError`` out of the
        store-status path. Epic's copy had the guard; now both do.
        """
        return read_cli_user_json(
            "amazon",
            self.cli_path,
            str(Path(get_cfg(
                self._config,
                "stores.amazon.user_file",
                "~/.config/nile/user.json",
            )).expanduser()),
            self._bus,
            validate=lambda data: "customer_info" in data.get("extensions", {}),
        )

    async def prepare_web_session(self) -> Result:
        """Give the browser a signed-in amazon.com before the shop opens.

        nile signs in through Amazon's *device registration* flow, which
        authorises the device but leaves the shared Edge profile without
        the auth cookies a signed-in amazon.com needs — so the shop
        opened logged out even though the store itself worked. Exchange
        nile's refresh token for website cookies (what Amazon's own apps
        do) and plant them where Edge will read them.

        Only Amazon needs this. The other browser stores sign in through
        ordinary web logins that leave a session behind on their own.

        Best-effort throughout: a failure here means the shop opens with
        whatever session the profile already had, never that the cart
        stops working.
        """
        from unifideck.auth.edge_browser.cookie_writer import write_cookies
        from unifideck.auth.edge_browser.edge import PROFILE_DIR, EdgeBrowser
        from unifideck.stores.amazon.web_session import fetch_website_cookies

        # A running Edge owns the cookie DB and flushes its in-memory
        # copy over it on exit, so anything written underneath is lost.
        # Refuse rather than write something that cannot take effect.
        edge = getattr(self, "_edge", None)
        if edge is not None and any(
            EdgeBrowser.cdp_alive(port)
            for port in (
                edge.cdp_port,
                edge.xcloud_cdp_port(),
                edge.storefront_cdp_port(),
            )
        ):
            logger.info(
                "[AmazonStore] Edge is running — skipping cookie write",
            )
            return Result(success=False, store="amazon", error="edge_running")
        cookies = await fetch_website_cookies()
        if not cookies:
            return Result(success=False, store="amazon", error="no_web_cookies")
        written = await asyncio.to_thread(
            write_cookies, PROFILE_DIR, cookies,
        )
        return Result(success=written > 0, store="amazon")

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Start auth."""
        # Late-bind auth in case injection happened after __init__
        # without the rebuild hook (defensive).
        self._rebuild_auth_after_injection()
        if self._auth is None:
            return AuthResult(
                success=False,
                error="auth_not_configured",
                store="amazon",
            )
        # Edge prerequisite : the launcher subprocess opens
        # the nile OAuth URL inside Microsoft Edge. Returning
        # a structured `edge_not_installed` here lets the
        # frontend spawn the install modal instead of letting
        # the launcher subprocess crash later.
        edge = getattr(self, "_edge", None)
        if edge is None or not edge.is_installed:
            logger.info(
                "[AmazonStore] Edge not installed — prompting user",
            )
            return AuthResult(
                success=False,
                error="edge_not_installed",
                store="amazon",
            )
        edge.clear_store_cookies("amazon.com")
        return cast("AuthResult", await self._auth.start_auth())

    async def complete_auth(self, code: str = "", **kwargs: Any) -> AuthResult:
        """Complete auth."""
        if await self.is_available():
            return AuthResult(success=True, store="amazon")
        return AuthResult(
            success=False,
            error="not_authenticated",
            store="amazon",
        )

    async def logout(self) -> Result:
        """Logout."""
        if self._auth is None:
            await self._emit(
                Events.STORE_LOGOUT,
                store="amazon",
            )
            return Result(success=True)
        return await self._auth.logout()

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """Get library.

        Refreshes nile's ``library.json`` from Amazon first so newly-claimed
        games appear (UD-012). Runs on every sync (parity with Epic/GOG), not
        just ``force`` — gated on auth to avoid a guaranteed-fail sync for
        logged-out users. The refresh is best-effort: on failure we fall
        through to the last-known file.
        """
        if not self.cli_path:
            return []
        try:
            if self._check_nile_authenticated():
                await self._library.sync_library(
                    self.cli_path,
                    self._amazon_cfg["library_sync_timeout_seconds"],
                )
            owned = await self._library.read_owned_games()
            installed = await self._library.read_installed_ids()
            # nile records the directory under ``path``; it can outlive the
            # files, so it is re-checked against disk.
            return merge_install_status(owned, installed, path_key="path")
        except Exception:
            logger.exception("[AmazonStore] get_library failed")
            return []

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: ProgressCallback | None = None,
        **kwargs: Any,
    ) -> InstallResult:
        """Install game."""
        return await self._installer.install_game(
            game_id,
            base_path,
            progress_cb,
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Uninstall game."""
        return await self._installer.uninstall_game(
            game_id,
            delete_prefix=bool(kwargs.get("delete_prefix", False)),
        )

    async def update_game(
        self,
        game_id: str,
        progress_cb: ProgressCallback | None = None,
        **kwargs: Any,
    ) -> InstallResult:
        """Update game via ``nile update`` (in-place patch)."""
        base_path = await self._updates.resolve_current_base_path(game_id)
        return await self._installer.install_game(
            game_id,
            base_path=base_path,
            progress_cb=progress_cb,
            verb="update",
        )

    async def check_for_updates(self) -> list[str]:
        """Check for updates."""
        return await self._updates.check_for_updates()

    async def get_game_size(self, game_id: str) -> int | None:
        """Get game size."""
        return await self._updates.get_game_size(game_id)

    async def get_installed_path(self, game_id: str) -> str | None:
        """On-disk install dir for an installed Amazon game (nile records).

        Lets the App-Details "Installed size" find the real directory
        when the sync cache's ``install_path`` is missing/stale.
        """
        installed = await self._library.read_installed_ids()
        info = installed.get(game_id) if isinstance(installed, dict) else None
        # nile calls the field ``path``; GOG and Ubisoft call it
        # ``install_path``. That literal is the only per-store difference.
        return install_path_from_record(info, key="path")

    async def get_official_url(self, game_id: str) -> str | None:
        """Get official URL."""
        return await self._library.get_official_url(game_id)
