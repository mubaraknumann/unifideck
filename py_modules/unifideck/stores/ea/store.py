"""EA store: a CLI-archetype store over ``maxima-cli``, our build of Maxima.

Same shape as Epic/GOG/Amazon: a bundled binary owns the EA sign-in, the
library, downloads and licences, and Unifideck launches the game itself under
umu. ``maxima-cli`` is built in the ``unifideck-maxima`` repo (upstream Maxima
plus our patches); ``cli.py`` is the only place it runs.

``stores.ea.cli_path`` points the store at a development build of
``maxima-cli`` (the build repo's ``out/backend/maxima-cli``) instead of the
bundled ``bin/maxima-cli``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from unifideck.auth.browser import OAuthBrowserMonitor
from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.core.types import AuthResult, Events, Game, InstallResult, Result, StoreInfo
from unifideck.launcher.ea import maxima
from unifideck.launcher.ea.records import all_records, read_record
from unifideck.stores.shared.browser_auth_rebuild import BrowserAuthRebuildMixin
from unifideck.stores.shared.store_base import StoreBase

from .achievements import EAAchievements
from .auth import EAAuthFlow
from .cli import MaximaCli
from .ids import STORE
from .install import EAInstaller, ProgressCallback
from .library import EALibraryReader

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)


class EAStore(BrowserAuthRebuildMixin, StoreBase):
    """EA store."""

    store_info = StoreInfo(
        name="ea",
        display_name="EA",
        auth_method="oauth",
        icon_asset="ea.png",
        supports_install=True,
    )

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
        cfg: dict[str, Any] = (config.get("stores.ea") if config else None) or {}
        self.cli_path = self._resolve_cli(str(cfg.get("cli_path") or ""))
        self._cli = MaximaCli(self.cli_path)
        self._library = EALibraryReader(self._cli)
        self._installer = EAInstaller(self._cli, self._library.entry)
        self._achievements = EAAchievements(self._cli)
        self._browser_monitor = browser_monitor
        self._auth: EAAuthFlow | None = None
        self._rebuild_auth_after_injection()

    def _resolve_cli(self, override: str) -> str | None:
        """The development build named in config, else the bundled binary."""
        found = maxima.find_cli(self._plugin_dir, override)
        return str(found) if found else None

    def _build_auth_flow(self, orchestrator: AuthOrchestrator) -> EAAuthFlow:
        """EA's half of ``BrowserAuthRebuildMixin``."""
        return EAAuthFlow(self._bus, orchestrator, self._cli)

    async def is_available(self) -> bool:
        """Whether EA is signed in."""
        ok = self._auth is not None and await self._auth.signed_in()
        self._cached_available = ok
        return ok

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Open the EA sign-in window, unless a stored sign-in still works."""
        return await self._start_browser_sign_in()

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """Report whether the sign-in finished."""
        if await self.is_available():
            return AuthResult(success=True, store=STORE)
        return AuthResult(success=False, error="not_authenticated", store=STORE)

    async def logout(self) -> Result:
        """Forget the stored EA sign-in."""
        self._achievements.clear()
        if self._auth is None:
            return Result(success=True, store=STORE)
        return await self._auth.logout()

    async def get_game_achievements(
        self, game_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """An EA game's achievements and this account's progress (display only).

        Raises ``EAAchievementsError``; a game without EA achievements is an
        empty payload. ``force`` bypasses the TTL cache.
        """
        return await self._achievements.get_game_achievements(game_id, force=force)

    def invalidate_achievements(self, game_id: str) -> None:
        """Drop a game's cached achievements."""
        self._achievements.invalidate(game_id)

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """Owned EA games, or None when they cannot be read."""
        if not self.cli_path:
            return None
        return await self._library.get_library()

    async def install_game(
        self, game_id: str, base_path: str | None = None,
        progress_cb: ProgressCallback = None, **kwargs: Any,
    ) -> InstallResult:
        """Download ``game_id``; Touchup follows at the prefix warmup."""
        return await self._installer.install(game_id, base_path, progress_cb)

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Delete the game folder.

        ``GAME_UNINSTALLED`` flips the shortcut back to "Not Installed"; the
        prefix is the uninstall RPC's job (``core.safe_delete.delete_game_prefix``).
        """
        result = await self._installer.uninstall(game_id)
        if result.success:
            await self._emit(Events.GAME_UNINSTALLED, store=STORE, game_id=game_id)
        return result

    async def update_game(
        self, game_id: str, progress_cb: ProgressCallback = None, **kwargs: Any,
    ) -> InstallResult:
        """Download the live build over the installed one."""
        return await self._installer.update(game_id, progress_cb)

    async def check_for_updates(self) -> list[str]:
        """Installed games whose recorded build is no longer EA's live build."""
        records = await asyncio.to_thread(all_records)
        outdated = []
        for game_id, record in records.items():
            live = await self._installer.live_build(game_id)
            if live and record.build_id and live != record.build_id:
                outdated.append(game_id)
        return outdated

    async def get_installed_path(self, game_id: str) -> str | None:
        """The game folder from its install record."""
        record = await asyncio.to_thread(read_record, game_id)
        return record.install_path if record else None

    async def find_installed_exe(self, install_path: str, game_id: str) -> str | None:
        """The exe EA's offer names, from the install record.

        Asked by the download worker and the change-exe default; without it they
        guess, and a build that ships a trial exe launched the trial.
        """
        record = await asyncio.to_thread(read_record, game_id)
        return record.exe if record and record.exe else None

    async def get_game_size(self, game_id: str) -> int | None:
        """Installed size of the live build, from its archive index (no download)."""
        return await self._installer.size(game_id)
