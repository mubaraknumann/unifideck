"""rpc/mixins/cleanup_finalize.py — post-wipe reset of *live* state.

The disk half of "Delete all Unifideck data" was already correct: shortcuts,
artwork, credentials, prefixes and install dirs all go. What it never did was
tell the running process, so the Downloads tab kept listing games whose files
had just been deleted — reading ``SyncService``'s in-memory ``_all_games``,
which the wipe left untouched — with sizes served from
``installed_disk_info``'s 300-second memo.

This mixin is the missing final step: snapshot what the process still believes
is installed, drop the in-memory library and the size memo, prune the CLI
install records that only become prunable once the marker sweep has run, then
announce each cleared game so the frontend refreshes itself.

Split out of ``sync_cleanup.py`` (515/550 LOC) rather than added to it — the
same volumetry split both that module and ``cleanup_sweeps.py`` document.
Where ``cleanup_sweeps`` holds state-free blocking filesystem passes, this
holds the async orchestration that needs the mixin's collaborators.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from unifideck.core import cleanup_sweeps
from unifideck.core.types.events import Events
from unifideck.services.installed_disk_info import clear_memo

logger = logging.getLogger(__name__)


class _CleanupFinalizeMixin:
    """Post-wipe invalidation of in-process state. Never raises."""

    # Provided by the concrete Plugin at runtime.
    bus: Any
    sync_service: Any
    cache: Any

    async def _finalize_wipe(self, delete_files: bool) -> int:
        """Reset live state after the on-disk wipe. Returns records pruned.

        Order is load-bearing:

        1. **snapshot** — must precede the reset, or there is nothing left
           to announce.
        2. **reset + memo clear** — must precede the announce, so
           ``SyncService._on_shortcut_install_state_changed`` finds no
           matching record and returns *before* ``_save_library_cache()``.
           Announcing first would re-persist the library the wipe deleted.
        3. **prune** — must follow ``marker_sweep.sweep_all`` (the caller
           runs it immediately before this), which reads the very records
           being pruned.

        Every step is individually best-effort. ``perform_full_cleanup`` is
        ``auto_wrap``-ed, so a raise here would turn a fully successful wipe
        into an ``internal_error`` envelope and a "Delete failed" toast for
        the user — after their data was already gone.
        """
        cleared = self._snapshot_installed()
        self._reset_library_state()
        pruned = await self._prune_records(delete_files)
        await self._announce_cleared(cleared)
        return pruned

    def _snapshot_installed(self) -> list[tuple[str, str, int]]:
        """``(store, store_game_id, app_id)`` for each still-"installed" game.

        Read from the same in-memory library the Downloads tab filters on,
        so the snapshot is exactly the set of rows about to become lies.
        """
        sync_service = getattr(self, "sync_service", None)
        if sync_service is None:
            return []
        try:
            games = sync_service.get_all_games()
        except Exception:
            logger.exception("[cleanup] get_all_games failed")
            return []
        snapshot: list[tuple[str, str, int]] = []
        for game in games or []:
            if not getattr(game, "installed", False):
                continue
            store = getattr(game, "store", "") or ""
            game_id = getattr(game, "store_game_id", "") or ""
            if not store or not game_id:
                continue
            app_id = getattr(game, "app_id", 0)
            snapshot.append(
                (store, game_id, app_id if isinstance(app_id, int) else 0),
            )
        return snapshot

    def _reset_library_state(self) -> None:
        """Drop the in-memory library and the installed-size memo."""
        sync_service = getattr(self, "sync_service", None)
        if sync_service is not None:
            try:
                sync_service.reset_library_state()
            except Exception:
                logger.exception("[cleanup] reset_library_state failed")
        # ``clear_memo`` exists for exactly this moment. Belt-and-suspenders
        # once the library is empty, but it stops a stale (size, location)
        # pair being served to a re-synced game inside the memo's TTL.
        try:
            clear_memo()
        except Exception:
            logger.exception("[cleanup] clear_memo failed")

    async def _prune_records(self, delete_files: bool) -> int:
        """Prune dangling CLI install records + the cache ``.bak`` snapshots."""
        pruned = 0
        try:
            pruned = await asyncio.to_thread(
                cleanup_sweeps.sweep_stale_install_records, delete_files,
            )
        except Exception:
            logger.exception("[cleanup] stale-record prune failed")
        base_path = getattr(getattr(self, "cache", None), "base_path", None)
        if base_path is not None:
            try:
                pruned += await asyncio.to_thread(
                    cleanup_sweeps.sweep_cache_backups, str(base_path),
                )
            except Exception:
                logger.exception("[cleanup] cache-backup sweep failed")
        logger.info("[cleanup] pruned %d residual record(s)", pruned)
        return pruned

    async def _announce_cleared(
        self, cleared: list[tuple[str, str, int]],
    ) -> None:
        """Emit ``SHORTCUT_INSTALL_STATE_CHANGED`` for each cleared game.

        Reuses the event ``ShortcutService.mark_uninstalled`` already emits,
        with the same six kwargs, so no new event has to be threaded through
        the enum, the schema gate, the frontend types and the poll allowlist.
        Two surfaces react for free: ``DownloadsTab`` refetches the installed
        list, and ``library-filters`` flips the per-app installed flag and
        invalidates that app's cached size.

        ``GAME_UNINSTALLED`` would be the more literal choice but is useless
        here: its subscriber looks the shortcut up in ``shortcuts.vdf``, which
        this flow has just emptied, so it returns before emitting anything.

        Expect one benign ``SyncService`` warning per game ("no matching
        record in _all_games") — that is the reset working as intended.
        """
        bus = getattr(self, "bus", None)
        if bus is None or not cleared:
            return
        for store, game_id, app_id in cleared:
            try:
                await bus.emit(
                    Events.SHORTCUT_INSTALL_STATE_CHANGED,
                    store=store,
                    store_game_id=game_id,
                    app_id=app_id,
                    installed=False,
                    exe_path="",
                    install_path="",
                )
            except Exception:
                logger.exception(
                    "[cleanup] announcing %s:%s cleared failed", store, game_id,
                )
        logger.info("[cleanup] announced %d cleared game(s)", len(cleared))
