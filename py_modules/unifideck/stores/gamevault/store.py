"""GameVault store — Layer-4 ``StoreBase`` implementation.

``GameVaultStore`` wires one pipeline to one pair of sources:

* :class:`GameVaultAuth`           — the config file, and remote sign-in.
* :class:`GameVaultLibraryReader`  — catalog → library, install state overlaid.
* :class:`GameVaultInstaller`      — acquire → extract → register.

A GameVault library is either a **self-hosted server** or a **folder of
archives on this device**, and that choice is the only thing that varies. It
is expressed once, in :meth:`_build_pipeline`, by picking which
:class:`~.sources.CatalogSource` and :class:`~.sources.ArchiveSource` to hand
to the two collaborators above. Every method below is then single-path — no
``if self._auth.is_local`` in install, uninstall, library, size or executable
resolution — which is the property that keeps the two modes from drifting
apart the way two parallel implementations would.

Only :meth:`is_available` and :meth:`start_auth` branch, because "is the
connection usable" and "how do I connect" are genuinely different questions
per mode.

This module must contain exactly one ``StoreBase`` subclass:
``StoreRegistry._load_store_class`` picks the first one it finds.

Config section (``defaults/config.json`` → ``stores.gamevault``):
    config_file:          path to the persisted connection JSON
    default_install_root: default directory for extracted games
    download_dir:         *separate* staging directory for archive downloads
                          (remote mode only; deleted after extraction)
    default_vault_dir:    default folder offered for a local vault
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import (
    AuthResult,
    Events,
    Game,
    InstallResult,
    Result,
    StoreInfo,
)
from unifideck.stores.shared.store_base import StoreBase

from .auth import MODE_LOCAL, GameVaultAuth
from .install import GameVaultInstaller
from .library import GameVaultLibraryReader, RemoteCatalog
from .local_catalog import LocalVaultCatalog
from .sources import (
    ArchiveSource,
    CatalogSource,
    LocalArchiveSource,
    ProgressCallback,
    RemoteArchiveSource,
)

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

STORE_NAME = "gamevault"

_DEFAULT_CONFIG_FILE = "~/.local/share/unifideck/gamevault_config.json"
_DEFAULT_INSTALL_ROOT = "~/Games/GameVault"
_DEFAULT_DOWNLOAD_DIR = "~/.local/share/unifideck/gamevault_downloads"
_DEFAULT_VAULT_DIR = "~/Games/UnifideckVault"


class GameVaultStore(StoreBase):
    """GameVault connector — self-hosted server, or a local vault folder."""

    # NOTE: no per-store ``sync_timeout``. Nothing reads one — the sync
    # applies ``PER_STORE_FETCH_TIMEOUT_SECONDS`` (120s) to every store
    # alike — so a field here would be a write-only declaration of the kind
    # audit §3.1 removed. If 120s proves too tight for a large self-hosted
    # library, that is a change to the shared constant with a measurement
    # behind it, not a silent per-store number.

    # No ``uses_wine`` / ``supports_cloud_saves`` here: both were removed
    # from StoreInfo (audit §3.1, register 26/31) and are derived instead —
    # ``client_runs_in_prefix`` from ``WRAPPER_STORES``, the capability flags
    # from ``core.store_capabilities``. Passing either raises TypeError, by
    # design. GameVault is in none of those sets: it is not a wrapper store,
    # and it has no achievements, cloud saves, language picker or browser
    # storefront.
    # ``name`` is a literal, not ``STORE_NAME``: check 3 in
    # ``scripts/validate_architecture.py`` reads this value statically and
    # matches it against the directory name, and it cannot resolve a
    # constant reference.
    store_info = StoreInfo(
        name="gamevault",
        display_name="GameVault",
        auth_method="manual",
        icon_asset="gamevault.png",
        supports_install=True,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
    ) -> None:
        """Initialise the GameVault store connector."""
        super().__init__(bus, cache, plugin_dir, config)

        gv_cfg = (config.get("stores.gamevault") if config else None) or {}

        self._default_install_root: str = gv_cfg.get(
            "default_install_root", _DEFAULT_INSTALL_ROOT,
        )
        self._download_dir: str = gv_cfg.get("download_dir", _DEFAULT_DOWNLOAD_DIR)
        self._default_vault_dir: str = gv_cfg.get(
            "default_vault_dir", _DEFAULT_VAULT_DIR,
        )

        self._auth = GameVaultAuth(
            config_file=gv_cfg.get("config_file", _DEFAULT_CONFIG_FILE),
        )
        self._local_catalog: LocalVaultCatalog | None = None
        self._build_pipeline()

    # ── The one place the two modes differ ──────────────────────────

    def _build_pipeline(self) -> None:
        """Wire the catalog and archive sources for the connected mode.

        Called at construction and again after any connect, because a
        connect can change the mode (or the vault path) under a store object
        that outlives it.
        """
        catalog: CatalogSource
        source: ArchiveSource
        if self._auth.is_local:
            self._local_catalog = LocalVaultCatalog(
                self._auth.vault_dir or self._default_vault_dir,
            )
            catalog = self._local_catalog
            source = LocalArchiveSource(self._local_catalog)
        else:
            self._local_catalog = None
            catalog = RemoteCatalog(self._auth)
            source = RemoteArchiveSource(
                self._auth, download_dir=self._download_dir,
            )

        # One install root for both modes: the configured default, used only
        # when the install RPC supplies no path of its own. It almost always
        # does — ``useInstallFlow`` runs the shared storage picker before
        # every install, for every store — so this is the fallback, not the
        # setting.
        self._installer = GameVaultInstaller(
            source=source, default_install_root=self._default_install_root,
        )
        self._library_reader = GameVaultLibraryReader(
            installer=self._installer, catalog=catalog,
        )

    # ── StoreBase abstract methods ──────────────────────────────────

    async def is_available(self) -> bool:
        """Is this store usable right now?

        Remote: credentials are on record (server reachability is checked
        lazily at sync time). Local: the vault folder is actually present —
        an SD card that has not mounted must keep the store *out* of the
        sync's store set, because a store that is never fetched is never
        swept, and the user's shortcuts survive the reboot.
        """
        if not self._auth.is_authenticated():
            return False
        if self._local_catalog is not None:
            return await self._local_catalog.is_present()
        return True

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Connect, in whichever mode the caller asked for.

        Remote (default) takes ``server_url``, ``username``, ``password``,
        ``verify_ssl`` and optionally ``download_dir``. Local takes
        ``vault_dir`` and nothing else.
        """
        if kwargs.get("mode") == MODE_LOCAL:
            # Falls back to the configured default, so "connect" with an
            # untouched form is a complete action: the folder is created for
            # the user rather than demanded from them.
            result = await self._auth.start_local_auth(
                vault_dir=kwargs.get("vault_dir") or self._default_vault_dir,
            )
        else:
            result = await self._auth.start_auth(
                server_url=kwargs.get("server_url", ""),
                username=kwargs.get("username", ""),
                password=kwargs.get("password", ""),
                verify_ssl=kwargs.get("verify_ssl", True),
                download_dir=kwargs.get("download_dir") or None,
            )
        if result.success:
            self._build_pipeline()
        return result

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """GameVault uses a single-step auth; returns cached auth state."""
        if self._auth.is_authenticated():
            return AuthResult(
                success=True,
                action="authenticated",
                tokens_cached=True,
                store=STORE_NAME,
            )
        return AuthResult(
            success=False,
            error="Not authenticated — call start_auth first",
            store=STORE_NAME,
        )

    async def logout(self) -> Result:
        result = await self._auth.logout()
        # Back to the default (remote) pipeline, so a stale local catalog
        # cannot keep answering after the vault was disconnected.
        self._build_pipeline()
        return result

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        try:
            return await self._library_reader.get_library(force=force)
        except Exception:
            # ``None``, never ``[]``. The sync treats an empty list as a real
            # answer ("this user owns nothing") and the shortcut reconcile
            # sweeps accordingly, so returning one here would delete the
            # user's GameVault shortcuts every time the server was down — or
            # every time an SD card was slow to mount.
            logger.exception("[GameVaultStore] get_library failed")
            return None

    async def install_game(
        self,
        game_id: str,
        base_path: str | None = None,
        progress_cb: ProgressCallback | None = None,
        **kwargs: Any,
    ) -> InstallResult:
        return await self._installer.install_game(
            game_id,
            install_path=base_path or kwargs.get("install_path"),
            progress_callback=progress_cb or kwargs.get("progress_callback"),
        )

    async def uninstall_game(self, game_id: str, **kwargs: Any) -> Result:
        """Remove the game, then announce it like every other store does.

        ``GAME_UNINSTALLED`` is what ``ShortcutService`` subscribes to in
        order to flip the shortcut back to "not installed" while keeping it
        (and its appid, artwork and playtime) in place. Emitting it here —
        rather than having the uninstall RPC call ``mark_uninstalled``
        directly — keeps GameVault on the same path as GOG, Epic, Amazon,
        Ubisoft and Battle.net, instead of adding a second mechanism that
        would fire twice for all of them.
        """
        result = await self._installer.uninstall_game(game_id)
        if result.success:
            await self._emit(
                Events.GAME_UNINSTALLED,
                store=STORE_NAME,
                game_id=game_id,
            )
        return result

    async def update_game(self, game_id: str, **kwargs: Any) -> InstallResult:
        """Re-install the game (GameVault has no delta updates)."""
        return await self.install_game(game_id, **kwargs)

    async def check_for_updates(self) -> list[str]:
        """GameVault does not expose an update API in either mode."""
        return []

    async def get_installed_path(self, game_id: str) -> str | None:
        """Install dir per our own marker, or ``None`` if not installed.

        The hook every store implements. It matters more here than
        elsewhere: it is what lets Change Executable resolve a directory when
        the games.map row is missing, which for this store is the situation
        the picker exists for.
        """
        info = self._installer.get_install_info(game_id)
        path = (info or {}).get("install_path")
        return path if isinstance(path, str) and path else None

    async def get_game_size(self, game_id: str) -> int | None:
        return await self._installer.get_game_size(game_id)

    # ── Extra helpers called by main.py (backward-compat surface) ──

    def _get_install_info(self, game_id: str) -> dict[str, Any] | None:
        """Return the persisted install marker dict for *game_id*."""
        return self._installer.get_install_info(game_id)

    async def get_installed(self) -> dict[str, dict[str, Any]]:
        """Return {game_id: install_info} for all installed GameVault games."""
        return self._installer.get_installed()
