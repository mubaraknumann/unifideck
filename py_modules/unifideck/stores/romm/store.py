"""RomM store: the Linux and Windows games on one RomM server.

``RomMStore`` wires the shared archive pipeline to one server:

* :class:`RomMAuth`: pairing attempts, then the config file.
* :class:`RomMCatalog`: the platform-filtered game list, covers and sizes.
* :class:`RomMArchiveSource`: one ROM streamed into a staging directory.

Config section (``defaults/config.json`` → ``stores.romm``):
    config_file:          path to the persisted connection JSON
    default_install_root: default directory for extracted games
    download_dir:         staging directory for ROM downloads, deleted after
                          extraction
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from unifideck.core.types import AuthResult, Game, Result, StoreInfo
from unifideck.stores.shared.archive_install import ArchiveInstaller
from unifideck.stores.shared.archive_library import ArchiveLibraryReader
from unifideck.stores.shared.archive_store import ArchiveStoreMixin
from unifideck.stores.shared.store_base import StoreBase

from .auth import STORE_NAME, RomMAuth
from .client import RomMAPIError
from .library import RomMCatalog
from .normalize import normalize_game_directory
from .sources import RomMArchiveSource

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.core.cache_manager import CacheManager
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_FILE = "~/.local/share/unifideck/romm_config.json"
_DEFAULT_INSTALL_ROOT = "~/Games/RomM"
_DEFAULT_DOWNLOAD_DIR = "~/.local/share/unifideck/romm_downloads"


class RomMStore(ArchiveStoreMixin, StoreBase):
    """RomM connector: Linux and Windows games from the user's own server.

    Connection state lives in :class:`RomMAuth` (pairing attempts, then the
    config file), the game list in :class:`RomMCatalog`, and installation in
    the shared :class:`ArchiveInstaller` pipeline. This class wires them
    together and translates their failures into what the sync expects.
    """

    # ``name`` is a literal: scripts/validate_architecture.py reads it
    # statically and matches it against the directory name.
    store_info = StoreInfo(
        name="romm",
        display_name="RomM",
        auth_method="manual",
        icon_asset="",
        supports_install=True,
    )

    def __init__(
        self,
        bus: EventBus,
        cache: CacheManager,
        plugin_dir: str | None = None,
        config: ConfigManager | None = None,
    ) -> None:
        super().__init__(bus, cache, plugin_dir, config)
        cfg = (config.get("stores.romm") if config else None) or {}
        self._auth = RomMAuth(config_file=cfg.get("config_file", _DEFAULT_CONFIG_FILE))
        self._catalog = RomMCatalog(self._auth)
        source = RomMArchiveSource(
            self._auth, self._catalog,
            download_dir=cfg.get("download_dir", _DEFAULT_DOWNLOAD_DIR),
        )
        self._installer = ArchiveInstaller(
            store=STORE_NAME,
            source=source,
            default_install_root=cfg.get("default_install_root", _DEFAULT_INSTALL_ROOT),
            normalize=normalize_game_directory,
        )
        self._library_reader = ArchiveLibraryReader(
            installer=self._installer, catalog=self._catalog,
        )

    # ── Connection ──────────────────────────────────────────────────

    async def is_available(self) -> bool:
        """Whether the store can be synced. Never contacts the server.

        A saved connection counts as available unless a library fetch was
        rejected with a 401 and no connection has succeeded since; an outage is
        not a logout. Reachability is checked lazily, by the sync, and being
        unavailable only means the store is not fetched this run, which is what
        keeps a Deck that boots before the network from having its shortcuts
        swept: the sweep only considers stores that answered.
        """
        return self._auth.is_authenticated()

    async def start_auth(self, **kwargs: Any) -> AuthResult:
        """Begin an attempt: validate a pasted token, or start a pairing.

        Takes ``attempt_id``, ``server_url``, ``token``, ``verify_ssl`` and
        ``download_dir``. Nothing is committed here.
        """
        return await self._auth.begin(
            str(kwargs.get("attempt_id", "")),
            server_url=str(kwargs.get("server_url", "")),
            token=str(kwargs.get("token", "")),
            verify_ssl=bool(kwargs.get("verify_ssl", True)),
            download_dir=str(kwargs.get("download_dir") or ""),
        )

    async def poll_auth(self, **kwargs: Any) -> AuthResult:
        """Check whether the user has approved the pairing.

        Returns ``action="pending"`` until they do, and never returns the
        device code.
        """
        return await self._auth.poll(str(kwargs.get("attempt_id", "")))

    async def complete_auth(self, **kwargs: Any) -> AuthResult:
        """Commit the attempt, with the platform variants the user excluded."""
        excluded = kwargs.get("excluded_fs_slugs") or []
        return await self._auth.complete(
            str(kwargs.get("attempt_id", "")),
            excluded_fs_slugs=[str(s) for s in excluded],
        )

    async def cancel_auth(self, attempt_id: str = "", **_: Any) -> Result:
        """Cancel one attempt, or every pending one when no ID is given.

        Cancelling the attempt that committed the current connection deletes
        that connection, and reports failure if the config file could not be
        removed.
        """
        return await self._auth.cancel(attempt_id)

    async def logout(self) -> Result:
        """Delete the saved connection.

        Local only: Unifideck never revokes the token, so it stays valid in
        RomM and a paired device stays listed until the user removes it there.
        """
        return await self._auth.logout()

    async def shutdown(self) -> None:
        """Cancel whatever is still pending when the plugin unloads."""
        await self._auth.shutdown()

    # ── Library ─────────────────────────────────────────────────────

    async def get_library(self, *, force: bool = False) -> list[Game] | None:
        """Every supported game, or ``None`` when the server could not answer.

        ``None``, never ``[]``: the sync reads an empty list as a real answer
        and sweeps the shortcuts accordingly. A 401 additionally marks the
        connection unavailable in memory, which keeps the store out of the
        next sync without deleting the saved credentials.
        """
        try:
            return await self._library_reader.get_library(force=force)
        except RomMAPIError as exc:
            if exc.status == 401:
                self._auth.mark_unauthorized()
            logger.warning("[RomMStore] get_library failed (%s)", exc.code)
            return None
        except Exception:
            logger.exception("[RomMStore] get_library failed")
            return None
