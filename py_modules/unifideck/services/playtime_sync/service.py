"""services/playtime_sync/service.py — report local playtime to the stores.

We track every play session locally (``services/playtime`` → ``playtime.db``).
This service pushes each finalized session up to its store (GOG / Epic) so GOG
Galaxy / the Epic launcher / the user's other devices reflect time played here,
and pulls the store's authoritative total back for display. It is the plugin's
answer to Heroic issue #1240.

Design (mirrors ``AchievementWatcher``: wired ``(bus, registry)``, plugin-only):

* **The DB is the queue.** ``play_sessions.reported_at`` is a per-session
  watermark; the set of un-stamped sessions IS the (offline-durable) push
  queue. We drain it on ``PLAYTIME_UPDATED`` (right after a session finalizes)
  and once at startup (to catch sessions recorded while offline / last run).
* **Minimal payload.** Only the per-session delta each store needs leaves the
  device (GOG ``{session_date, time}``; Epic ``{artifactId, machineId,
  startTime, endTime}``) — never the local DB / stats / streaks.
* **Always-on.** No user toggle; the only gate is "is the store available?"
  (an undocumented ``playtime_sync.enabled`` kill-switch exists for debugging).
* **Reconcile.** After pushing, pull the store's total and cache it
  (``store_playtime``) — the store total is the cross-device superset and is
  shown as-is, never summed with local.

Token handling lives in each store (``GOGSessions`` / ``EpicSessions``); this
service just calls ``store.report_play_session`` / ``store.get_play_total_secs``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe
from unifideck.services.playtime.db import ActivityDatabase

if TYPE_CHECKING:
    from unifideck.config import ConfigManager
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores import StoreRegistry

logger = logging.getLogger(__name__)

# Stores with the ``report_play_session`` / ``get_play_total_secs`` API. Matches
# the stores ``get_unreported_sessions`` filters on.
_SYNC_STORES = ("gog", "epic")

# Strong refs to fire-and-forget drains so the GC can't collect them mid-flight.
_BACKGROUND: set[asyncio.Task[Any]] = set()


def _track(task: asyncio.Task[Any]) -> None:
    """Keep a strong ref to a background task until it settles."""
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _iso_to_unix(value: str) -> int | None:
    """``play_sessions.started_at`` (ISO-8601 UTC) → epoch seconds, or None."""
    try:
        return int(
            datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp(),
        )
    except (ValueError, TypeError):
        return None


class PlaytimeSyncService:
    """Drains finalized local sessions to the stores + reconciles totals."""

    def __init__(
        self,
        bus: EventBus,
        registry: StoreRegistry | None,
        config: ConfigManager | None = None,
        db_path: str = "",
    ) -> None:
        """Store refs and auto-wire event subscriptions."""
        self._bus = bus
        self._registry = registry
        self._config = config
        self._db_path = db_path
        self._db: ActivityDatabase | None = None
        # Serialize drains so a startup catch-up and a PLAYTIME_UPDATED drain
        # can't interleave on the same rows.
        self._lock = asyncio.Lock()
        auto_wire(self, bus)

    async def start(self) -> None:
        """Open a private DB connection and kick off a catch-up drain."""
        if self._db is None:
            self._db = ActivityDatabase(self._db_path)
            self._db.open()
        _track(asyncio.create_task(self._drain()))

    async def stop(self) -> None:
        """Unsubscribe and close the DB."""
        self._bus.unsubscribe_all(self)
        if self._db is not None:
            self._db.close()
            self._db = None

    @subscribe(Events.PLAYTIME_UPDATED)
    async def _on_playtime_updated(self, **_kwargs: Any) -> None:
        """A session just finalized — drain the queue in the background."""
        _track(asyncio.create_task(self._drain()))

    async def sync_now(self) -> dict[str, int]:
        """Drain on demand (RPC/debug). Returns ``{store: pushed_count}``."""
        return await self._drain()

    # -- internals ---------------------------------------------------------

    async def _drain(self) -> dict[str, int]:
        """Push every unreported session to its store, then reconcile totals."""
        if self._config is not None and not self._config.get_bool(
            "playtime_sync.enabled", default=True,
        ):
            return {}
        pushed: dict[str, int] = {}
        async with self._lock:
            if self._db is None:
                return pushed
            rows = self._db.get_unreported_sessions(_SYNC_STORES)
            if not rows:
                return pushed
            by_store: dict[str, list[Any]] = {}
            for row in rows:
                by_store.setdefault(row["store"], []).append(row)
            for store_name, store_rows in by_store.items():
                pushed[store_name] = await self._drain_store(store_name, store_rows)
        return pushed

    async def _drain_store(self, store_name: str, rows: list[Any]) -> int:
        """Push one store's sessions; reconcile its totals. Returns push count."""
        store = self._registry.get_store(store_name) if self._registry else None
        if store is None or not hasattr(store, "report_play_session"):
            return 0
        try:
            if not await store.is_available():
                return 0
        except Exception:
            logger.debug("[playtime_sync] %s availability check failed", store_name)
            return 0

        count = 0
        reconcile: dict[int, str] = {}  # game_db_id → store_game_id
        for row in rows:
            started = _iso_to_unix(row["started_at"])
            if started is None:
                # Unparseable timestamp — stamp it so it doesn't wedge the queue.
                self._db.mark_session_reported(row["id"])  # type: ignore[union-attr]
                continue
            try:
                ok = await store.report_play_session(
                    row["store_game_id"], started, int(row["duration_secs"]),
                )
            except Exception:
                logger.debug(
                    "[playtime_sync] %s push raised for session %s",
                    store_name, row["id"], exc_info=True,
                )
                ok = False
            if ok:
                self._db.mark_session_reported(row["id"])  # type: ignore[union-attr]
                count += 1
                reconcile[int(row["game_db_id"])] = row["store_game_id"]

        await self._reconcile(store, store_name, reconcile)
        self._emit_result(store_name, count)
        return count

    async def _reconcile(
        self, store: Any, store_name: str, games: dict[int, str],
    ) -> None:
        """Pull each pushed game's store-side total and cache it for display."""
        if not games or not hasattr(store, "get_play_total_secs"):
            return
        for game_db_id, store_game_id in games.items():
            try:
                total = await store.get_play_total_secs(store_game_id)
            except Exception:
                logger.debug(
                    "[playtime_sync] %s total fetch raised for %s",
                    store_name, store_game_id, exc_info=True,
                )
                total = None
            if total is not None and self._db is not None:
                self._db.upsert_store_playtime(game_db_id, total)

    def _emit_result(self, store_name: str, pushed: int) -> None:
        """Best-effort outcome event.

        Nothing consumes this yet. It said "toast bridge consumes it" until
        2026-08, which was never true on any leg (audit §1.3) — the event is
        absent from ``src/types/events.ts`` and ``WATCHED_EVENTS``, so the
        frontend does not even poll for it. Kept deliberately; see the
        ``unwired:`` note on ``Events.PLAYTIME_SYNC_COMPLETE``.
        """
        if pushed <= 0:
            return
        _track(asyncio.create_task(
            self._bus.emit(
                Events.PLAYTIME_SYNC_COMPLETE, store=store_name, pushed=pushed,
            ),
        ))
