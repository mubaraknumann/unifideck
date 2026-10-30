"""itch.io store: a CLI-archetype store whose CLI is a daemon.

Same shape as Epic/GOG/Amazon: a bundled binary (``butler``, in
``bin/butler/``) owns the sign-in state, the library, installs, uninstalls and
updates, and Unifideck launches the game itself through the ordinary launcher
(native Linux builds directly, Windows builds under umu). The one structural
difference is the transport: butler serves its API as a JSON-RPC daemon
(``butlerd.py``), not as streamed stdout. butler's database holds the profile
and API key, the way legendary's ``user.json`` does, so this store keeps no
credential file of its own.

Sign-in is the standard Edge auth window (``auth.py``).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unifideck.auth.browser import OAuthBrowserMonitor
from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.auth.url_file import write_url_file_atomically
from unifideck.core.store_urls import storefront_target_path
from unifideck.core.types import AuthResult, CLITool, Events, Game, InstallResult, Result, StoreInfo
from unifideck.stores.shared.browser_auth_rebuild import BrowserAuthRebuildMixin
from unifideck.stores.shared.store_base import StoreBase

from .auth import ItchAuthFlow
from .butlerd import ButlerDaemon, ButlerdError
from .exe import resolve_launch_target, upload_platform
from .install import CLAIM_REQUIRED, ItchInstaller
from .library import ItchLibraryReader
from .progress import ProgressCallback
from .uploads import choose_upload

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

#: Lists every bundle the user bought, each linking to its claim page.
BUNDLES_PAGE_URL = "https://itch.io/my-purchases/bundles"

_DEFAULTS: dict[str, Any] = {
    "butler_db": "~/.local/share/unifideck/butler/butler.db",
    "default_install_root": "~/Games/itch",
    # Seconds with no Progress advance and no butler log line before an
    # install is declared stalled. Long on purpose: see install.py.
    "stall_timeout_seconds": 900,
}


class ItchStore(BrowserAuthRebuildMixin, StoreBase):
    """itch.io store."""

    store_info = StoreInfo(
        name="itch",
        display_name="itch.io",
        auth_method="oauth",
        icon_asset="itch.png",
        supports_install=True,
    )
    CLI_TOOL = CLITool(name="butler", search_paths=["bin/butler/butler"])

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
        browser_monitor: OAuthBrowserMonitor | None = None,
    ) -> None:
        """Initialize the instance."""
        super().__init__(bus, cache, plugin_dir, config)
        cfg = {**_DEFAULTS, **((config.get("stores.itch") if config else None) or {})}
        self.cli_path: str | None = self._find_binary(self.CLI_TOOL)
        if not self.cli_path:
            logger.warning(
                "[ItchStore] butler not found at bin/butler/butler, so itch.io "
                "sign-in, library and installs are unavailable",
            )
        self._daemon = ButlerDaemon(self.cli_path, str(cfg["butler_db"]))
        self._library = ItchLibraryReader(self._daemon, on_ready=self._library_ready)
        self._installer = ItchInstaller(
            self._daemon, self._library,
            default_install_root=str(Path(str(cfg["default_install_root"])).expanduser()),
            stall_s=float(cfg["stall_timeout_seconds"]),
        )
        self._browser_monitor = browser_monitor
        self._warned_key_rejected = False
        self._auth: ItchAuthFlow | None = None
        self._rebuild_auth_after_injection()

    def _build_auth_flow(self, orchestrator: AuthOrchestrator) -> ItchAuthFlow:
        """itch.io's half of ``BrowserAuthRebuildMixin``."""
        return ItchAuthFlow(
            self._bus, orchestrator, self._daemon, lambda: getattr(self, "_edge", None),
        )

    async def is_available(self) -> bool:
        """Signed in = butler holds a profile (a local read, no network)."""
        if not self.cli_path or not self._daemon.has_database:
            self._cached_available = False
            return False
        try:
            ok = await self._library.profile_id() is not None
        except (ButlerdError, OSError, TimeoutError) as e:
            logger.warning("[ItchStore] could not ask butler for its profile: %s", e)
            ok = False
        self._cached_available = ok
        return ok

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Open the Edge sign-in window (or reuse a still-valid saved login)."""
        self._rebuild_auth_after_injection()
        if self._auth is None:
            return AuthResult(success=False, error="auth_not_configured", store="itch")
        edge = getattr(self, "_edge", None)
        if edge is None or not edge.is_installed:
            return AuthResult(success=False, error="edge_not_installed", store="itch")
        return cast("AuthResult", await self._auth.start_auth())

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """Nothing to complete: the Edge flow finishes on its own."""
        if await self.is_available():
            return AuthResult(success=True, store="itch")
        return AuthResult(success=False, error="not_authenticated", store="itch")

    async def logout(self) -> Result:
        """Forget butler's profile."""
        if self._auth is None:
            return Result(success=True, store="itch")
        return await self._auth.logout()

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """Owned games; None (keep shortcuts) whenever butler cannot answer.

        A key itch.io refuses (403) costs the user the whole itch.io library,
        so it raises one toast per streak rather than a log line on every
        sync. Other unreadable states stay quiet, as for every store.
        """
        if not self.cli_path:
            return None
        games = await self._library.get_library()
        if not self._library.key_rejected:
            self._warned_key_rejected = False
        elif not self._warned_key_rejected:
            self._warned_key_rejected = True
            await self._bus.emit(
                Events.LAUNCHER_STAGE,
                severity="warning",
                i18n_title_key="toasts.header.itch",
                i18n_key="toasts.itchKeyRejectedMessage",
                duration_ms=12000,
                store="itch",
            )
        return games

    async def _library_ready(self) -> None:
        """A library read a sync stopped waiting for has finished: sync again."""
        await self._emit(Events.STORE_LIBRARY_READY, store="itch")

    async def install_precondition(self, game_id: str) -> str | None:
        """``itch_claim_required`` for a bundle game not claimed yet."""
        if not self.cli_path:
            return None
        return CLAIM_REQUIRED if await self._installer.claim_needed(game_id) else None

    async def begin_claim(self, game_id: str) -> dict[str, Any] | None:
        """Point the next itch.io shop window at the user's bundles page.

        The claim itself is the user's: they find the bundle, then press
        the game's Download button, which turns it into an owned key.
        Opening the bundle's own download page directly needs its private
        URL, which the API does not give (``Bundle.url`` is the public
        page); ``my-purchases/bundles`` lists it.
        """
        profile = await self._library.profile_id()
        if profile is None:
            return None
        title = await self._library.bundle_title(profile, game_id)
        target = str(storefront_target_path("itch"))
        if not await write_url_file_atomically(target, BUNDLES_PAGE_URL):
            return None
        logger.info("[ItchStore] claim page armed for %s (bundle %r)", game_id, title)
        return {"url": BUNDLES_PAGE_URL, "bundle_title": title}

    async def finish_claim(self, game_id: str) -> bool:
        """True once *game_id* is an owned key (the claim window closed).

        Re-reads the owned keys, which also stores the new key in butler's
        database, so the install that follows takes the ordinary path.
        """
        try:
            profile = await self._library.profile_id()
            if profile is None:
                return False
            await self._library.refresh_owned_keys(profile)
            claimed = not await self._installer.claim_needed(game_id, profile)
        except (ButlerdError, OSError, TimeoutError) as e:
            logger.warning("[ItchStore] could not check the claim of %s: %s", game_id, e)
            return False
        logger.info("[ItchStore] %s %s", game_id, "claimed" if claimed else "still unclaimed")
        return claimed

    async def install_game(
        self, game_id: str, base_path: str | None = None,
        progress_cb: ProgressCallback | None = None, **kwargs: Any,
    ) -> InstallResult:
        """Install the best upload through butlerd."""
        return await self._installer.install(game_id, base_path, progress_cb)

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Uninstall through butlerd, then tell the rest of the plugin.

        ``GAME_UNINSTALLED`` is what flips the shortcut back to "Not
        Installed" and drops the games.map row (``ShortcutService``); without
        it the files were gone while Steam still offered Play (measured on
        the first device run). The Proton prefix is the uninstall RPC's job
        (``core.safe_delete.delete_game_prefix``), shared by every store.
        """
        result = await self._installer.uninstall(game_id)
        if not result.success:
            return result
        await self._emit(Events.GAME_UNINSTALLED, store="itch", game_id=game_id)
        return result

    async def update_game(
        self, game_id: str, progress_cb: ProgressCallback | None = None, **kwargs: Any,
    ) -> InstallResult:
        """Apply butler's update (wharf patches when the game has builds)."""
        return await self._installer.update(game_id, progress_cb)

    async def check_for_updates(self) -> list[str]:
        """Installed game ids with an update available."""
        try:
            return await self._installer.check_for_updates()
        except ButlerdError as e:
            logger.warning("[ItchStore] update check failed: %s", e)
            return []

    async def get_game_size(self, game_id: str) -> int | None:
        """Download size of the upload an install would pick."""
        try:
            result = await self._daemon.call(
                "Fetch.GameUploads",
                {"gameId": int(game_id), "compatible": False, "fresh": True},
            )
        except (ButlerdError, ValueError):
            return None
        upload = choose_upload(result.get("uploads") or [])
        return int(upload["size"]) if upload and upload.get("size") else None

    async def get_installed_path(self, game_id: str) -> str | None:
        """butler's install folder for *game_id*."""
        try:
            cave = (await self._library.installed_map()).get(game_id)
        except ButlerdError:
            return None
        return str(cave["install_path"]) if cave else None

    async def find_installed_exe(self, install_path: str, game_id: str) -> str | None:
        """Launch target for the games.map row (``installed_game.py`` hook)."""
        try:
            cave = (await self._library.installed_map()).get(game_id) or {}
        except ButlerdError:
            cave = {}
        platform = upload_platform(cave.get("upload") or {})
        title = str(cave.get("title") or game_id)
        return await asyncio.to_thread(resolve_launch_target, install_path, platform, title)

    async def shutdown(self) -> None:
        """Plugin unload: stop butlerd politely (``--destiny-pid`` is the backstop)."""
        await self._library.cancel_read()
        await self._daemon.shutdown()
