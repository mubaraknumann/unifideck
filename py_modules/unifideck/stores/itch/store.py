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
from unifideck.core.safe_delete import canonical_prefix, safe_rmtree
from unifideck.core.types import AuthResult, CLITool, Events, Game, InstallResult, Result, StoreInfo
from unifideck.stores.shared.browser_auth_rebuild import BrowserAuthRebuildMixin
from unifideck.stores.shared.store_base import StoreBase

from .auth import ItchAuthFlow
from .butlerd import ButlerDaemon, ButlerdError
from .exe import resolve_launch_target, upload_platform
from .install import ItchInstaller
from .library import ItchLibraryReader
from .progress import ProgressCallback
from .uploads import choose_upload

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

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
        self._library = ItchLibraryReader(self._daemon)
        self._installer = ItchInstaller(
            self._daemon, self._library,
            default_install_root=str(Path(str(cfg["default_install_root"])).expanduser()),
            stall_s=float(cfg["stall_timeout_seconds"]),
        )
        self._browser_monitor = browser_monitor
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
        """Owned games; None (keep shortcuts) whenever butler cannot answer."""
        if not self.cli_path:
            return None
        return await self._library.get_library()

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
        the first device run). ``delete_prefix`` removes the per-game Proton
        prefix, the same contract Amazon and Epic honour.
        """
        result = await self._installer.uninstall(game_id)
        if not result.success:
            return result
        if kwargs.get("delete_prefix"):
            await asyncio.to_thread(safe_rmtree, canonical_prefix(game_id))
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
        await self._daemon.shutdown()
