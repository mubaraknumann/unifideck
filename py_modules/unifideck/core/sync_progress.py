"""SyncProgress — per-phase progress tracker for the frontend bar.

Ported from ``staging:main.py:1496-1650``. One instance per sync run,
owned by ``SyncService``. Each pipeline phase owns a percentage
range; within a phase, progress is computed from its sub-counter
(done / total). The frontend polls ``get_sync_progress`` → this
object's ``to_dict()`` every 500 ms.

Phase transitions are triggered by the synchronous pass-through
methods so callers (SyncService itself, ArtworkService,
MetadataService) never set internal fields directly.

Metadata sub-counters: staging's flow was sequential
(steam_metadata → unifidb_lookup → metacritic), each with its own
counter ticking. Current MetadataService fans the three sources
out in parallel via ``asyncio.gather`` (faster), so the counters
move in lockstep — but they're exposed independently so the
frontend can render one row per source. UnifiDB / Metacritic
rows were missing in for-pr-0.7; this restores them.

Per-store rows: ``stores`` maps each store a run covers to its own
``{state, phase, done, total, error}`` entry, so the Quick Access store
list can show one status line per store instead of one bar for the
whole run. States are ``queued`` (waiting for its fetch turn),
``active`` (its library fetch or a post-sync phase is working on its
games), ``waiting`` (its part of the current step is done, the rest of
the run is not), ``done``, ``error`` and ``cancelled``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

# Percentage allocation per phase. The progress bar always moves
# forward monotonically — once a phase is done, its range is
# committed and the next phase starts at the next boundary.
PHASE_RANGES: dict[str, tuple[int, int]] = {
    "idle": (0, 0),
    "fetching": (0, 10),
    "checking_installed": (10, 20),
    "syncing": (20, 40),
    "steam_metadata": (40, 50),
    "unifidb_lookup": (50, 55),
    "sgdb_lookup": (55, 60),
    "artwork": (60, 90),
    "metadata": (90, 95),
    # Background ProtonDB / Deck-Verified fetch — happens after
    # metadata enrichment via CompatibilityService. Narrow band
    # (95-98) because individual lookups are fast (~50ms) and the
    # phase shouldn't dominate the bar visually.
    "proton_meta": (95, 98),
    "complete": (100, 100),
    "error": (100, 100),
    "cancelled": (100, 100),
}


class SyncProgress:
    """Per-sync-run progress tracker."""

    def __init__(self) -> None:
        self.total_games: int = 0
        self.synced_games: int = 0
        self.current_game: dict[str, Any] = {"label": None, "values": {}}
        self.status: str = "idle"
        self.error: str | None = None
        self.progress_percent: int = 0
        # Per-phase sub-counters. The metadata phase has three
        # parallel sources, each tracked independently so the
        # frontend can render per-source progress rows.
        self.artwork_total: int = 0
        self.artwork_synced: int = 0
        self.steam_total: int = 0
        self.steam_synced: int = 0
        self.unifidb_total: int = 0
        self.unifidb_synced: int = 0
        self.metacritic_total: int = 0
        self.metacritic_synced: int = 0
        # Compatibility phase ("proton_meta"): ProtonDB tier +
        # Deck-Verified status per game. Owned by
        # :class:`CompatibilityService`.
        self.compat_total: int = 0
        self.compat_synced: int = 0
        # Per-store rows for the stores this run covers; see the module
        # docstring for the states. Replaced wholesale by ``begin_stores``.
        self.stores: dict[str, dict[str, Any]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    # ── Phase-entry helpers ────────────────────────────────────

    def start_fetching(self, store_count: int) -> None:
        self.status = "fetching"
        self.total_games = 0
        self.current_game = {
            "label": "sync.fetchingGameLists",
            "values": {"count": store_count},
        }

    def begin_stores(
        self, store_names: Iterable[str], *, fetching: bool = True,
    ) -> None:
        """Reset the per-store rows for a new run.

        Args:
            store_names: every store the run covers.
            fetching: ``True`` when the run fetches libraries, so each
                store queues for its turn; ``False`` for an artwork-only
                run, where every store goes straight to ``waiting``.
        """
        state = "queued" if fetching else "waiting"
        self.stores = {
            name: _store_row(state, "games" if fetching else "")
            for name in store_names
        }

    def finish_store_fetch(
        self, store_name: str, count: int, error: str | None,
    ) -> None:
        """Record one store's fetch result: ``waiting`` or ``error``."""
        row = self.stores.get(store_name)
        if row is None:
            return
        row.update(
            state="error" if error else "waiting",
            done=count, total=count, error=error,
        )

    def begin_store_phase(self, phase: str, games: Iterable[Any]) -> None:
        """Point every non-failed store row at ``phase``.

        ``games`` is the list the phase is about to walk; each store's
        ``total`` is its share of it. A store with no games in the list
        sits the phase out as ``waiting``.
        """
        counts: dict[str, int] = {}
        for game in games:
            store = getattr(game, "store", None)
            if isinstance(store, str):
                counts[store] = counts.get(store, 0) + 1
        for name, row in self.stores.items():
            if row["state"] in ("error", "cancelled"):
                continue
            total = counts.get(name, 0)
            row.update(
                state="active" if total else "waiting",
                phase=phase, done=0, total=total,
            )

    def _tick_store(self, store: str | None) -> None:
        """Advance one store's row by one game in its current phase."""
        row = self.stores.get(store) if store else None
        if row is None or row["state"] != "active":
            return
        row["done"] = min(row["done"] + 1, row["total"])
        if row["done"] >= row["total"]:
            row["state"] = "waiting"

    def _finish_stores(self, state: str) -> None:
        """Move every row that has not failed to a terminal ``state``."""
        for row in self.stores.values():
            if row["state"] != "error":
                row["state"] = state

    def start_store_sync(self, store_name: str, idx: int, total: int) -> None:
        row = self.stores.get(store_name)
        if row is not None:
            row.update(state="active", phase="games")
        self.status = "syncing"
        self.current_game = {
            "label": "sync.fetchingStore",
            "values": {"store": store_name, "current": idx + 1, "total": total},
        }

    def set_library_totals(self, total_games: int) -> None:
        self.total_games = total_games
        self.synced_games = total_games

    def start_artwork(
        self, total: int, label: str = "sync.checkingExistingArtwork",
    ) -> None:
        self.status = "artwork"
        self.artwork_total = total
        self.artwork_synced = 0
        self.current_game = {
            "label": label,
            "values": {"synced": 0, "total": total},
        }

    def start_metadata(
        self, total: int, label: str = "sync.fetchingEnhancedMetadata",
    ) -> None:
        """Enter the metadata enrichment phase.

        Sets the two source totals that are actually exercised
        during the blocking enrichment loop (Steam Store search +
        UnifiDB) — Metacritic moved to a fire-and-forget backfill
        in :func:`metadata_backfill.spawn` and no longer ticks in
        lockstep with the sync UI. Leaving ``metacritic_total`` at
        ``0`` makes the frontend's ``> 0`` gate hide the row so
        we don't display a counter for work that hasn't started.
        """
        self.status = "metadata"
        self.steam_total = total
        self.steam_synced = 0
        self.unifidb_total = total
        self.unifidb_synced = 0
        self.current_game = {
            "label": label,
            "values": {"synced": 0, "total": total},
        }

    def start_compat(
        self, total: int, label: str = "sync.fetchingEnhancedMetadata",
    ) -> None:
        """Enter the Compatibility (ProtonDB + Deck-Verified) phase."""
        self.status = "proton_meta"
        self.compat_total = total
        self.compat_synced = 0
        self.current_game = {
            "label": label,
            "values": {"synced": 0, "total": total},
        }

    def mark_complete(self) -> None:
        self.status = "complete"
        self.current_game = {"label": "sync.completed", "values": {}}
        self.progress_percent = 100
        self._finish_stores("done")

    def mark_error(self, error: str) -> None:
        self.status = "error"
        self.error = error
        self.progress_percent = 100

    def mark_cancelled(self) -> None:
        self.status = "cancelled"
        self.progress_percent = 100
        self._finish_stores("cancelled")

    # ── Per-game increment helpers (thread-safe) ──────────────

    async def increment_artwork(
        self, title: str, store: str | None = None,
    ) -> int:
        async with self._lock:
            self.artwork_synced += 1
            self._tick_store(store)
            self.current_game = {
                "label": "sync.downloadingArtwork",
                "values": {
                    "synced": self.artwork_synced,
                    "total": self.artwork_total,
                    "game": title,
                },
            }
            self._recalc()
            return self.artwork_synced

    async def increment_steam(
        self, title: str, store: str | None = None,
    ) -> int:
        async with self._lock:
            self.steam_synced += 1
            self._tick_store(store)
            self.current_game = {
                "label": "sync.extractingSteamMetadata",
                "values": {
                    "synced": self.steam_synced,
                    "total": self.steam_total,
                    "game": title,
                },
            }
            self._recalc()
            return self.steam_synced

    async def increment_unifidb(self, title: str) -> int:
        async with self._lock:
            self.unifidb_synced += 1
            self.current_game = {
                "label": "sync.lookingUpUnifiDB",
                "values": {
                    "synced": self.unifidb_synced,
                    "total": self.unifidb_total,
                    "game": title,
                },
            }
            self._recalc()
            return self.unifidb_synced

    async def increment_metacritic(self, title: str) -> int:
        async with self._lock:
            self.metacritic_synced += 1
            self.current_game = {
                "label": "sync.fetchingMetacriticData",
                "values": {
                    "synced": self.metacritic_synced,
                    "total": self.metacritic_total,
                    "game": title,
                },
            }
            self._recalc()
            return self.metacritic_synced

    async def increment_compat(
        self, title: str, store: str | None = None,
    ) -> int:
        async with self._lock:
            self.compat_synced += 1
            self._tick_store(store)
            self.current_game = {
                "label": "sync.fetchingCompatData",
                "values": {
                    "synced": self.compat_synced,
                    "total": self.compat_total,
                    "game": title,
                },
            }
            self._recalc()
            return self.compat_synced

    # ── Internal ──────────────────────────────────────────────

    def _recalc(self) -> None:
        """Compute progress_percent from the current phase + sub-counter.

        Metadata phase: three sources tick in lockstep, so any
        of them is a valid driver. Pick the *minimum* — most
        pessimistic, prevents the bar from racing ahead if one
        source fails fast and the others lag.
        """
        start_pct, end_pct = PHASE_RANGES.get(self.status, (0, 0))
        span = end_pct - start_pct

        if self.status == "artwork" and self.artwork_total > 0:
            sub = self.artwork_synced / self.artwork_total
            self.progress_percent = int(start_pct + span * sub)
        elif self.status == "metadata":
            # All three totals are set identically by start_metadata;
            # use whichever is non-zero (defensive).
            denom = max(
                self.steam_total,
                self.unifidb_total,
                self.metacritic_total,
            )
            if denom > 0:
                slowest = min(
                    self.steam_synced,
                    self.unifidb_synced,
                    self.metacritic_synced,
                )
                self.progress_percent = int(
                    start_pct + span * (slowest / denom),
                )
            else:
                self.progress_percent = start_pct
        elif self.status == "proton_meta" and self.compat_total > 0:
            sub = self.compat_synced / self.compat_total
            self.progress_percent = int(start_pct + span * sub)
        elif self.status == "syncing" and self.total_games > 0:
            sub = self.synced_games / self.total_games
            self.progress_percent = int(start_pct + span * sub)
        else:
            self.progress_percent = start_pct

    def to_dict(self) -> dict[str, Any]:
        self._recalc()
        return {
            "success": True,
            "total_games": self.total_games,
            "synced_games": self.synced_games,
            "current_game": self.current_game,
            "status": self.status,
            "progress_percent": self.progress_percent,
            "error": self.error,
            "artwork_total": self.artwork_total,
            "artwork_synced": self.artwork_synced,
            "steam_total": self.steam_total,
            "steam_synced": self.steam_synced,
            "unifidb_total": self.unifidb_total,
            "unifidb_synced": self.unifidb_synced,
            "metacritic_total": self.metacritic_total,
            "metacritic_synced": self.metacritic_synced,
            "compat_total": self.compat_total,
            "compat_synced": self.compat_synced,
            "stores": {name: dict(row) for name, row in self.stores.items()},
        }


def _store_row(state: str, phase: str) -> dict[str, Any]:
    """A fresh per-store progress row."""
    return {"state": state, "phase": phase, "done": 0, "total": 0, "error": None}
