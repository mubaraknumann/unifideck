"""Epic Games Store — Layer-4 implementation of the unified store interface.

``EpicStore`` is the orchestration class that wires every Epic
sub-component together and exposes them through the ``StoreBase``
contract. It owns one instance each of :

* ``EpicAuthFlow`` — OAuth via embedded browser.
* ``EpicLibraryReader`` — owned-games library reader.
* ``EpicInstaller`` — install/uninstall pipeline.
* ``EpicUpdateChecker`` — periodic update polling.
* ``EpicExeResolver`` — locate the launchable .exe.

Epic Games uses ``legendary`` (a community CLI replacement for the
Epic Games Launcher, written in Python) for all download/install
operations. The store class is the high-level coordinator that
orchestrates token lifecycle, library fetch, install pipeline,
update detection, and post-install exe resolution.

Implements the standard ``StoreBase`` API : ``store_info``,
``is_authed``, ``auth``, ``logout``, ``library``, ``install``,
``uninstall``, ``launch``, etc. — each method delegates to the
appropriate sub-component.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from unifideck.auth.browser import OAuthBrowserMonitor
from unifideck.auth.orchestrator import AuthOrchestrator
from unifideck.core.binaries import read_cli_timeouts
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
from unifideck.stores.shared.store_base import StoreBase
from unifideck.utils.config_helpers import get_cfg

from . import sdl
from .achievements import EpicAchievements
from .auth import EpicAuthFlow
from .exe_resolver import EpicExeResolver
from .install import (
    EpicInstaller,
    ProgressCallback,
)
from .library import EpicLibraryReader
from .sessions import EpicSessions
from .uninstall import read_legendary_install_path
from .updates import EpicUpdateChecker

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

class EpicStore(BrowserAuthRebuildMixin, StoreBase):
    """Epic store."""

    store_info = StoreInfo(
        name="epic",
        display_name="Epic Games",
        auth_method="oauth",
        icon_asset="epic.png",
        supports_install=True,
    )

    CLI_TOOL = CLITool(
        name="legendary",
        search_paths=["bin/legendary"],
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
            logger.warning("[EpicStore] legendary binary not found")
        self._shortcut_service = shortcut_service
        self._timeouts = read_cli_timeouts(config)
        epic_cfg = config.get("stores.epic") if config else None
        if epic_cfg is None:
            raise KeyError("config.stores.epic is required")
        self._build_cli_submodules(bus, epic_cfg)
        self._build_auth_submodule(browser_monitor)

    def _build_cli_submodules(self, bus: EventBus, epic_cfg: dict[str, Any]) -> None:
        """Build cli submodules."""
        self._library = EpicLibraryReader(
            cli_path=self.cli_path,
            library_timeout=self._timeouts["library_fetch"],
        )
        self._exe_resolver = EpicExeResolver(
            cli_path=self.cli_path,
            find_exe=self._find_exe,
            info_timeout_seconds=epic_cfg["info_timeout_seconds"],
        )
        self._installer = EpicInstaller(
            bus=bus,
            cli_path=self.cli_path,
            library=self._library,
            exe_resolver=self._exe_resolver,
            default_install_root=epic_cfg["default_install_root"],
        )
        self._updates = EpicUpdateChecker(
            bus=bus,
            cli_path=self.cli_path,
            library=self._library,
            list_updates_timeout=epic_cfg["list_updates_timeout_seconds"],
            size_cache_ttl=epic_cfg["size_cache_ttl_seconds"],
            info_timeout=epic_cfg["info_timeout_seconds"],
        )
        user_file = str(Path(get_cfg(
            self._config,
            "stores.epic.user_file",
            "~/.config/legendary/user.json",
        )).expanduser())
        self._achievements = EpicAchievements(
            cli_path=self.cli_path,
            user_file=user_file,
            info_timeout=epic_cfg["info_timeout_seconds"],
        )
        self._sessions = EpicSessions(
            cli_path=self.cli_path,
            user_file=user_file,
            machine_id=socket.gethostname() or "steamdeck",
            info_timeout=epic_cfg["info_timeout_seconds"],
        )

    def _build_auth_submodule(self, browser_monitor: OAuthBrowserMonitor | None) -> None:
        """Build auth submodule.

        Auth is now late-bound : at boot the ``browser_monitor`` is
        ``None`` (auto-discovery doesn't have the service container
        yet). ``store_injector`` sets ``_browser_monitor``
        post-discovery and calls ``_rebuild_auth_after_injection``
        so the flow is wired against the just-injected monitor.
        """
        self._browser_monitor = browser_monitor
        self._auth: EpicAuthFlow | None = None
        self._rebuild_auth_after_injection()

    def _build_auth_flow(self, orchestrator: AuthOrchestrator) -> EpicAuthFlow:
        """Epic's half of ``BrowserAuthRebuildMixin``."""
        return EpicAuthFlow(
            bus=self._bus,
            orchestrator=orchestrator,
            cli_path=self.cli_path,
            cli_timeout_seconds=self._timeouts["auth_check"],
        )

    async def is_available(self) -> bool:
        """Check whether available."""
        ok = self._check_legendary_authenticated()
        self._cached_available = ok
        return ok

    def _check_legendary_authenticated(self) -> bool:
        """Check LEGENDARY authenticated.

        The read, the permission hardening and the audit emits are shared
        with Amazon in ``stores/shared/cli_credentials`` — the two were
        near-identical readers differing only in the store label, the config
        key and the predicate below (audit §3.2).
        """
        return read_cli_user_json(
            "epic",
            self.cli_path,
            str(Path(get_cfg(
                self._config,
                "stores.epic.user_file",
                "~/.config/legendary/user.json",
            )).expanduser()),
            self._bus,
            validate=lambda data: "access_token" in data,
        )

    async def get_game_achievements(
        self, game_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """An Epic game's achievements (definitions + this user's unlock status).

        Read-only display, via Epic's storefront GraphQL (reverse-engineered).
        Unlocking is handled in-game by the EOS overlay, not here. Raises
        ``EpicAchievementsError`` on auth/network/no-sandbox failure; a game
        with no EGS achievements is a normal empty payload. ``force`` bypasses
        the TTL cache.
        """
        return await self._achievements.get_game_achievements(
            game_id, force=force,
        )

    def invalidate_achievements(self, game_id: str) -> None:
        """Drop a game's cached achievements."""
        self._achievements.invalidate(game_id)

    async def report_play_session(
        self, game_id: str, started_at_unix: int, duration_secs: int,
    ) -> bool:
        """Report one finished play session to Epic (``library-service``).

        Used by ``PlaytimeSyncService`` so the Epic launcher's "Time Played" /
        other devices reflect time played here. ``game_id`` is the Epic
        ``artifactId`` (== legendary app_name). ``True`` on success; ``False``
        (never raises) on auth/network failure so the caller can retry.
        """
        return await self._sessions.report_session(
            game_id, started_at_unix, duration_secs,
        )

    async def get_play_total_secs(self, game_id: str) -> int | None:
        """Epic's authoritative total time played for ``game_id``, in seconds."""
        return await self._sessions.get_total_secs(game_id)

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Start auth."""
        if self._auth is None:
            return AuthResult(
                success=False,
                error="auth_not_configured",
                store="epic",
            )
        # Edge prerequisite : the launcher subprocess opens
        # the legendary OAuth URL inside Microsoft Edge.
        # Returning a structured `edge_not_installed` here
        # lets the frontend spawn the install modal instead
        # of letting the launcher subprocess crash with a
        # generic DependencyMissingError.
        edge = getattr(self, "_edge", None)
        if edge is None or not edge.is_installed:
            logger.info(
                "[EpicStore] Edge not installed — prompting user",
            )
            return AuthResult(
                success=False,
                error="edge_not_installed",
                store="epic",
            )
        # Clear Epic session cookies so the user always sees a
        # fresh login form. Stale browser sessions from a prior
        # auth attempt would otherwise auto-login and bypass the
        # OAuth redirect entirely.
        edge.clear_store_cookies("epicgames.com")
        return cast("AuthResult", await self._auth.start_auth())

    async def complete_auth(self, code: str = "", **kwargs: Any) -> AuthResult:
        """Complete auth."""
        if await self.is_available():
            return AuthResult(success=True, store="epic")
        return AuthResult(
            success=False,
            error="not_authenticated",
            store="epic",
        )

    async def logout(self) -> Result:
        """Logout."""
        if self._auth is None:
            await self._emit(Events.STORE_LOGOUT, store="epic")
            return Result(success=True)
        return await self._auth.logout()

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """Get library."""
        if not self.cli_path:
            return []
        try:
            owned = await self._library.read_owned_games()
            installed = await self._library.read_installed_map()
            # Defaults throughout: legendary records ``install_path``, and
            # its installed.json can outlive the directory, so the shared
            # helper's disk re-check is exactly what this store wants.
            return merge_install_status(owned, installed)
        except Exception:
            logger.exception("[EpicStore] get_library failed")
            return []

    async def find_installed_exe(
        self, install_path: str, game_id: str | None = None,
    ) -> str | None:
        """Resolve the launchable exe for an installed Epic game.

        Used by ``DownloadWorker._build_installed_game`` to populate
        ``games.map``. Delegates to ``EpicExeResolver``, which reads
        legendary's manifest ``launch_exe`` (the authoritative target)
        and falls back to the heuristic ``.exe`` finder — much better
        than the generic ``StoreBase._find_exe`` the worker would
        otherwise use. ``game_id`` is required for the manifest lookup.
        """
        if game_id:
            try:
                result = await self._exe_resolver.resolve(game_id)
                exe = result.get("executable")
                if (
                    isinstance(exe, str)
                    and exe
                    and await asyncio.to_thread(Path(exe).is_file)
                ):
                    return exe
            except Exception:
                logger.warning(
                    "[EpicStore] exe resolve failed for %s", game_id,
                    exc_info=True,
                )
        return self._find_exe(
            install_path, [game_id] if game_id else None,
        )

    async def get_install_language_options(self, game_id: str) -> dict[str, str]:
        """Return ``{sdl_tag: display_name}`` install-language choices.

        Empty unless the title is one of legendary's Selective Downloads
        titles offering more than the base game — see
        :mod:`unifideck.stores.epic.sdl`.
        """
        return await sdl.resolve_language_options(game_id)

    async def install_game(self, game_id: str, base_path: str | None = None,
                           progress_cb: ProgressCallback | None = None, **kwargs: Any) -> InstallResult:
        """Install game."""
        return await self._installer.install_game(
            game_id,
            base_path,
            progress_cb,
            language=kwargs.get("language"),
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Uninstall game."""
        return await self._installer.uninstall_game(
            game_id, delete_prefix=bool(kwargs.get("delete_prefix", False)),
        )

    async def update_game(
        self,
        game_id: str,
        progress_cb: ProgressCallback | None = None,
        **kwargs: Any,
    ) -> InstallResult:
        """Update game."""
        return await self._updates.update_game(
            game_id,
            installer=self._installer,
            progress_cb=progress_cb,
        )

    async def check_for_updates(self) -> list[str]:
        """Check for updates."""
        return await self._updates.check_for_updates()

    async def get_game_size(self, game_id: str) -> int | None:
        """Get game size."""
        return await self._updates.get_game_size(game_id)

    async def get_installed_path(self, game_id: str) -> str | None:
        """On-disk install dir, read from legendary's ``installed.json``.

        The sync cache often lands Epic installs with ``install_path =
        None`` (they're detected during sync, not via our worker), so
        the App-Details "Installed size" needs this to find the real
        directory and measure it. Local file read, off the event loop.
        """
        return await asyncio.to_thread(read_legendary_install_path, game_id)
