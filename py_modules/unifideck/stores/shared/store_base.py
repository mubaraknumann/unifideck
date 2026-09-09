import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from unifideck.core.binaries import binary_resolver
from unifideck.core.exe_finder import exe_finder
from unifideck.core.types import (
    AuthResult,
    CLITool,
    Events,
    Game,
    InstallResult,
    Result,
    StoreInfo,
)

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus import EventBus
logger = logging.getLogger(__name__)
class StoreBase(ABC):
    """Store base."""
    store_info: StoreInfo = StoreInfo(
        name="unknown",
        display_name="Unknown",
        auth_method="manual",
        icon_asset="",
    )
    def __init__(
        self,
        bus: "EventBus",
        cache: "CacheManager",
        plugin_dir: str | None = None,
        config: Optional["ConfigManager"] = None,
    ) -> None:
        """Initialize the instance."""
        self._bus = bus
        self._cache = cache
        self._plugin_dir = plugin_dir
        self._config = config
        self._cached_available: bool = False
    @property
    def store_name(self) -> str:
        """Store name."""
        return self.store_info.name
    @abstractmethod
    async def is_available(self) -> bool:
        """Check whether available."""
        ...
    @abstractmethod
    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Start auth."""
        ...
    @abstractmethod
    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """Complete auth."""
        ...
    @abstractmethod
    async def logout(self) -> Result:
        """Logout."""
        ...
    @abstractmethod
    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """Get library (``force`` requests a cache-bypassing refresh)."""
        ...

    @abstractmethod
    async def install_game(
        self, game_id: str, **kwargs: Any,
    ) -> InstallResult:
        """Install game."""
        ...
    @abstractmethod
    async def uninstall_game(
        self, game_id: str, **kwargs: Any,
    ) -> Result:
        """Uninstall game."""
        ...
    @abstractmethod
    async def update_game(
        self, game_id: str, **kwargs: Any,
    ) -> InstallResult:
        """Update game."""
        ...
    @abstractmethod
    async def check_for_updates(self) -> list[str]:
        """Check for updates."""
        ...
    @abstractmethod
    async def get_game_size(self, game_id: str) -> int | None:
        """Get game size."""
        ...

    async def get_installed_path(self, game_id: str) -> str | None:
        """Resolve the on-disk install directory for an installed game.

        Used to compute the exact "Installed size" when the sync cache's
        ``install_path`` is missing or stale. Default ``None`` (unknown);
        stores that track installs locally override this — e.g. Epic
        reads legendary's ``installed.json``.
        """
        return None

    def get_prefix_path(self, game_id: str) -> str | None:
        """The Wine prefix a game lives in, for stores where that is the install.

        Only meaningful for a **wrapper store**: its vendor client runs inside
        the prefix and installs the game into it, so the prefix is the game's
        real footprint and what uninstalling reclaims. Every other store
        downloads outside its prefix and leaves this ``None`` — the default —
        which is what keeps ``resolve_size_root`` a shared rule keyed on
        ``prefix_owns_game_install`` rather than a store-name branch.

        Synchronous: for both wrapper stores this is an in-memory id-map read,
        and it is called from a size lookup that is already off the hot path.
        """
        return None
    def _find_binary(self, tool: CLITool) -> str | None:
        """Find binary.

        The shared :class:`BinaryResolver` Tier-1 lookup requires
        every entry in ``tool.search_paths`` to be absolute (it
        rejects relative paths via ``Path.is_absolute()``). Stores
        idiomatically declare *relative* search paths like
        ``"bin/legendary"`` so the descriptor stays portable across
        install layouts. Absolutise them against ``self._plugin_dir``
        before delegating — otherwise the bundled CLI in
        ``<plugin>/bin/`` is silently skipped and the resolver
        falls through to ``PATH`` / ``~/.local/bin`` where the
        binary doesn't exist.
        """
        if self._plugin_dir:
            absolutised = [
                p if Path(p).is_absolute()
                else str(Path(self._plugin_dir) / p)
                for p in tool.search_paths
            ]
            tool = CLITool(
                name=tool.name,
                search_paths=absolutised,
            )
        return binary_resolver.resolve(tool)
    def _find_exe(
        self,
        install_path: str,
        hints: list[str] | None = None,
    ) -> str | None:
        """Find exe."""
        return exe_finder.find(install_path, hints)
    async def _emit(self, event: Events, **kwargs: Any) -> None:
        """Emit a bus event with arbitrary kwargs payload."""
        await self._bus.emit(event, **kwargs)

    # NOTE: there is deliberately no ``_run_cli`` here. One existed and was
    # never called by anything — every CLI store spawns
    # ``asyncio.create_subprocess_exec`` directly, because they all need to
    # stream stdout for progress rather than collect it at the end. Deleted
    # rather than adopted (audit §3.2, register item 10). If you add a
    # shared subprocess entry point, make it streaming; the scrubbed
    # environment it used to provide is available on its own as
    # ``core.binaries.clean_cli_env``, which every CLI store already calls.
