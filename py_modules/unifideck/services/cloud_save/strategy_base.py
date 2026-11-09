"""services/cloud_save/strategy_base.py — shared store cloud-save strategy.

``CloudSaveStrategy`` is the common base for the per-store strategies (Epic /
GOG). It centralizes everything the two stores should do *identically* so they
stay at par, and leaves only the genuinely store-specific bits (the CLI tool
invocation and the store's own save-location / cloud-listing metadata) to
abstract hooks the subclasses implement:

* **Save-dir resolution scaffolding** — explicit ``games.<id>.save_path``
  override, an in-process memo (resolution can hit the network, so it must run
  once per launch), and the shared enriched-metadata (unifiDB / PCGamingWiki)
  fallback. The store-specific resolution is ``_resolve_store_save_dir``.
* **The sync SAFETY contract** — ``sync_down`` always snapshots the local copy
  before pulling; ``sync_up`` always guards (snapshot + raise
  ``SaveConflictError`` on an empty / regressed local) *and* hard-asserts there
  are real saves before the destructive push. The store CLI runs in
  ``_do_sync_down`` / ``_do_sync_up``.
* **Cloud-info memoization** — the 300 s TTL cache around the (possibly slow)
  real store-cloud query ``_fetch_cloud_info``, invalidated after an upload.

Before this base existed, GOG and Epic each re-implemented all of the above,
which is how they drifted (e.g. one path missing a safety guard the other had).
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# How long the real store-cloud info query is memoized (seconds).
_CLOUD_INFO_TTL = 300


class CloudUnreachableError(Exception):
    """The store's cloud could not be reached, which is not the same as no saves."""



class CloudSaveStrategy(ABC):
    """Abstract base class for store-specific cloud-save synchronization."""

    #: Store id ("epic" / "gog"); used for safety/backup namespacing and logs.
    #: Subclasses MUST set this.
    store_id: str = ""

    def __init__(
        self, local_save_root: str, config: Any = None, cache: Any = None,
    ) -> None:
        self.local_save_root = local_save_root
        self.config = config
        # CacheManager (or None in CLI mode). Used to read enriched
        # save-location metadata (unifiDB/PCGamingWiki) — guard every read.
        self.cache = cache
        # get_local_save_dir is called several times per launch; memoize the
        # resolved dir so the (network) store lookup happens once. Only real
        # (non-None) results are cached so an unresolved dir re-resolves once a
        # prefix exists.
        self._cached_save_dir: dict[str, str] = {}
        # Real store-cloud save info, memoized with a short TTL; cleared on
        # upload (which changes the cloud copy).
        self._cached_cloud_info: dict[str, tuple[float, dict[str, Any]]] = {}

    # ── shared save-dir resolution ───────────────────────────────────
    def _configured_save_dir(self, game_id: str) -> str | None:
        """Explicit ``games.<id>.save_path`` config override, if any."""
        if self.config:
            configured = self.config.get(f"games.{game_id}.save_path")
            if configured:
                return str(configured)
        return None

    def _prefix_root(self, game_id: str) -> Path:
        """Per-game Wine prefix root (``…/prefixes/<game_id>``)."""
        return Path(self.local_save_root).parent / "prefixes" / game_id

    def _resolve_enriched(
        self, game_id: str, *, prefix_path: str, install_path: str = "",
        native_linux: bool = False,
    ) -> str | None:
        """Resolve from enriched save-location metadata (unifiDB/PCGamingWiki).

        Shared by both stores so the enriched tier behaves identically; the
        store id is passed through so the resolver picks the right manifest.
        ``native_linux`` resolves against real Linux home/XDG dirs (GOG native
        builds) instead of the Wine prefix.
        """
        try:
            from unifideck.services.cloud_save.save_location_resolver import (
                resolve_save_dir,
            )
            return resolve_save_dir(
                self.store_id, game_id,
                prefix_path=prefix_path,
                install_path=install_path,
                config=self.config,
                cache=self.cache,
                native_linux=native_linux,
            )
        except Exception as e:
            logger.debug(
                "[%sSync] enriched save-dir resolution failed for %s: %s",
                self.store_id, game_id, e,
            )
            return None

    def get_local_save_dir(self, game_id: str) -> str | None:
        """Resolve (and memoize) the game's local save directory, or ``None``.

        Order: config override → memoized result → store-specific resolution
        (``_resolve_store_save_dir``). ``None`` (no prefix yet) is never cached.
        """
        configured = self._configured_save_dir(game_id)
        if configured:
            return configured
        cached = self._cached_save_dir.get(game_id)
        if cached:
            return cached
        resolved = self._resolve_store_save_dir(game_id)
        if resolved:
            self._cached_save_dir[game_id] = resolved
        return resolved

    @abstractmethod
    def _resolve_store_save_dir(self, game_id: str) -> str | None:
        """Store-specific resolution (after override + memo miss).

        Return the real in-prefix save dir, or ``None`` when it can't be
        resolved (e.g. the prefix doesn't exist yet). Do NOT read the config
        override or memo here — the base handles both.
        """

    # ── shared sync scaffolding (the safety contract) ────────────────
    async def sync_down(self, game_id: str, force: bool = False) -> bool:
        """Pull cloud saves to the local save dir (snapshotting first).

        ``force`` pulls the cloud copy unconditionally (explicit "Use Cloud");
        the automatic path leaves it False so newer local saves aren't
        overwritten. The store CLI runs in ``_do_sync_down``.
        """
        local_dir = self.get_local_save_dir(game_id)
        if not local_dir:
            logger.warning(
                "[%sSync] Cannot sync down: save dir not resolved for %s",
                self.store_id, game_id,
            )
            return False
        # os.makedirs (not Path.mkdir) to match the store strategies' existing
        # pattern — the async sibling Path.mkdir trips ASYNC240.
        os.makedirs(local_dir, exist_ok=True)
        # Snapshot whatever's there before we pull — a bad/destructive
        # download must always be recoverable from a local backup.
        from unifideck.services.cloud_save import safety
        safety.snapshot_backup(local_dir, self.store_id, game_id)
        ok = await self._do_sync_down(game_id, local_dir, force)
        if ok:
            self._relocate_orphaned_saves(local_dir)
        return ok

    async def sync_up(self, game_id: str) -> bool:
        """Push local saves to the store cloud (guarded against a wipe).

        ``guard_before_upload`` snapshots a local backup and raises
        ``SaveConflictError`` (propagated to the service, which surfaces a
        conflict modal) when the local copy is empty or regressed vs the
        last-sync manifest; ``assert_has_saves`` is the final hard gate. The
        store CLI runs in ``_do_sync_up``.
        """
        local_dir = self.get_local_save_dir(game_id)
        if not local_dir:
            logger.warning(
                "[%sSync] Cannot sync up: save dir not resolved for %s",
                self.store_id, game_id,
            )
            return False
        # NOTE: run OUTSIDE any try/except so SaveConflictError propagates to
        # the service (which turns it into a user-facing conflict, never a wipe).
        from unifideck.services.cloud_save import safety
        safety.guard_before_upload(local_dir, self.store_id, game_id)
        # Final hard gate: NEVER push an empty / settings-only dir — uploading
        # nothing wipes the cloud, so that must be impossible.
        safety.assert_has_saves(local_dir, self.store_id, game_id)
        ok = await self._do_sync_up(game_id, local_dir)
        if ok:
            # The cloud copy just changed — drop the memoized cloud-save info.
            self._cached_cloud_info.pop(game_id, None)
        return ok

    @abstractmethod
    async def _do_sync_down(
        self, game_id: str, local_dir: str, force: bool,
    ) -> bool:
        """Run the store's CLI pull into ``local_dir``. Return success."""

    @staticmethod
    def _relocate_orphaned_saves(save_dir: str) -> None:
        """Relocate saves stranded in a platform-ID subfolder.

        Games that ship ``steam_api64.dll`` as a Steamworks wrapper (e.g.
        Tomb Raider I-III Remastered) create saves under a platform user-ID
        subfolder (``TRX/<steam-id>/savegame.dat``) on Windows where the DLL
        initializes successfully.  Under Proton — where the DLL fails to
        connect to a real Steam client — the game falls back to saving
        directly in the root (``TRX/savegame.dat``).

        When cloud saves originate from a Windows session, gogdl/legendary
        faithfully preserve the ``<user-id>/`` subdirectory.  The Proton game
        instance never looks there, so the saves are present on disk but
        invisible to the game.

        This method copies ``savegame.dat`` from a lone numeric-named
        subfolder into the save-dir root so the game finds it.  The three
        guard conditions make false-positives essentially impossible:

          1. A ``savegame.dat`` at the root already ⇒ no-op (game reads it).
          2. Only purely numeric directory names are considered (platform IDs).
          3. That directory must contain a ``savegame.dat``.

        ``copy2`` (not move) preserves the original cloud structure so
        ``sync_up`` doesn't diverge from what gogdl expects, and so a future
        Windows session with GOG Galaxy can still find the original subfolder.
        Best-effort; never fatal.
        """
        root = Path(save_dir)
        if not root.is_dir():
            return
        # Guard 1: root already has a savegame.dat — game can read it.
        if (root / "savegame.dat").is_file():
            return
        try:
            for child in root.iterdir():
                if not child.is_dir() or not child.name.isdigit():
                    continue
                src = child / "savegame.dat"
                if src.is_file():
                    shutil.copy2(str(src), str(root / "savegame.dat"))
                    logger.info(
                        "[CloudSync] Relocated orphaned save from %s to %s",
                        src, root / "savegame.dat",
                    )
                    return  # Only relocate once.
        except Exception as e:
            logger.warning(
                "[CloudSync] Failed to relocate orphaned saves in %s: %s",
                save_dir, e,
            )

    @abstractmethod
    async def _do_sync_up(self, game_id: str, local_dir: str) -> bool:
        """Run the store's CLI push from ``local_dir``. Return success."""

    # ── shared cloud-info memoization ────────────────────────────────
    async def get_cloud_save_info(self, game_id: str) -> dict[str, Any] | None:
        """Best-effort info about the game's ACTUAL store-cloud save.

        Returns ``{"has_saves": bool, "timestamp": float, ...}`` (timestamp =
        unix epoch of the latest cloud save, 0 if unknown), or ``None`` when the
        store can't report it cheaply. Raises :class:`CloudUnreachableError` when the
        store could not be reached at all. Memoized ``_CLOUD_INFO_TTL`` seconds so
        the status path doesn't re-spawn the store tool on every page load;
        invalidated after an upload. The store query is ``_fetch_cloud_info``.
        """
        cached = self._cached_cloud_info.get(game_id)
        if cached and (time.time() - cached[0]) < _CLOUD_INFO_TTL:
            return cached[1]
        info = await self._fetch_cloud_info(game_id)
        if info is not None:
            self._cached_cloud_info[game_id] = (time.time(), info)
        return info

    async def _fetch_cloud_info(self, game_id: str) -> dict[str, Any] | None:
        """Store-specific real-cloud query (un-memoized). Default: unsupported."""
        return None
