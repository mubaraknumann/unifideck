"""services/playtime/service.py — Per-game session tracker.

SQLite-backed playtime tracker. Refactor of legacy playtime/
package. Emits ``PLAYTIME_UPDATED`` after each session.
Persistence lives in ``db.py`` (``PlaytimeDB``); this module
owns event wiring + session lifecycle.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events
from unifideck.event_bus.event_bus_devex import auto_wire, subscribe

from .db import ActivityDatabase
from .stats_mixin import PlaytimeStatsMixin

if TYPE_CHECKING:
    from unifideck.event_bus.event_bus import EventBus

logger = logging.getLogger(__name__)

# Sessions shorter than this are ignored as accidental launches.
_MIN_SESSION_SECONDS = 5

# How often active sessions persist a provisional duration to disk. A crash /
# force-kill (no clean stop) can then be recovered to within this interval on
# the next startup (see ``_reconcile_orphans``). Cheap: one UPDATE per running
# game per tick.
_HEARTBEAT_SECONDS = 60


class PlaytimeService(PlaytimeStatsMixin):
    """SQLite-backed playtime tracker wired to the EventBus.

    Daily-stats recording + streak math live in :class:`PlaytimeStatsMixin`
    (``stats_mixin.py``); this module owns event wiring + session lifecycle.
    """

    def __init__(self, bus: EventBus, db_path: str) -> None:
        """Store refs, init empty ``_active`` map, and auto_wire."""
        self._bus = bus
        self._db_path = db_path
        self._db: ActivityDatabase | None = None
        self._active: dict[str, dict[str, Any]] = {}
        self._heartbeat_task: asyncio.Task[Any] | None = None

        # ``auto_wire(self, bus)`` walks ``self``'s methods
        # and registers every ``@subscribe(Events.X)``-marked
        # handler with the bus. Earlier this site called
        # ``self._bus.auto_wire(self)`` as if it were a bus
        # method, but ``auto_wire`` is module-level — the
        # call raised ``AttributeError`` and every
        # subscription was lost (caught and silenced upstream).
        auto_wire(self, self._bus)

    async def start(self) -> None:
        """Open the DB, recover any crash-orphaned sessions, start heartbeat."""
        if self._db is None:
            self._db = ActivityDatabase(self._db_path)
            self._db.open()
            # Close sessions left open by a previous crash/force-kill BEFORE
            # any new sessions start, so the recovery can't race a relaunch.
            self._reconcile_orphans()
        if self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """Cancel heartbeat, flush in-flight sessions, close the DB."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._db is not None:
            # End all active sessions
            keys = list(self._active.keys())
            for key in keys:
                store, game_id = key.split(":", 1)
                await self._end_session(store, game_id, end_reason="plugin_unload")

            self._db.close()
            self._db = None

    @staticmethod
    def _provisional_duration(session: dict[str, Any]) -> int:
        """Awake seconds so far for ``session``, no mutation.

        Single source of truth for both the heartbeat checkpoint and the final
        ``_end_session`` calc.

        Measured off ``time.monotonic()``, which maps to ``CLOCK_MONOTONIC`` and
        does NOT advance while the machine is suspended — so sleep is excluded
        for free, on every suspend path, including a hard one that fires no
        signal at all. This used to be ``wall_clock - total_sleep_secs``, where
        the sleep total was accumulated by ``SUSPEND``/``RESUME`` bus handlers
        that nothing ever emitted: sleep was permanently 0, so putting the Deck
        down mid-game billed the whole night as playtime and
        ``PlaytimeSyncService`` pushed that number to GOG/Epic.

        ``started_at`` stays wall-clock and is NOT interchangeable with this:
        ``_update_daily_stats`` needs a real date to attribute the session to a
        calendar day.
        """
        return max(0, int(time.monotonic() - session["started_monotonic"]))

    async def _heartbeat_loop(self) -> None:
        """Persist a provisional duration for active sessions every tick."""
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_SECONDS)
                try:
                    self._checkpoint_active()
                except Exception:
                    logger.debug(
                        "[PlaytimeService] heartbeat checkpoint failed",
                        exc_info=True,
                    )
        except asyncio.CancelledError:
            pass

    def _checkpoint_active(self) -> None:
        """Write each active session's running duration to disk (crash safety).

        Deliberately leaves ``ended_at`` NULL and ``end_reason`` 'unknown' so
        the row is NOT yet eligible for store sync (``get_unreported_sessions``
        requires ``ended_at``) nor counted in stats (``_refresh_game_stats``
        requires ``ended_at``) — it only records progress. If the process dies
        before a clean stop, ``_reconcile_orphans`` recovers this lower bound.
        """
        if self._db is None or not self._active:
            return
        for session in self._active.values():
            row_id = session.get("db_row_id")
            if not row_id:
                continue
            duration = self._provisional_duration(session)
            self._db.execute(
                """UPDATE play_sessions
                   SET duration_secs = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE id = ? AND ended_at IS NULL""",
                (duration, row_id),
            )
        self._db._require_conn().commit()

    def _reconcile_orphans(self) -> None:
        """Finalize sessions left open by a crash / force-kill, on startup.

        A clean shutdown flushes active sessions via ``stop()`` → ``_end_session``
        (which sets ``ended_at``), so any row still ``ended_at IS NULL`` here is
        an orphan from a hard restart. We credit the last heartbeat checkpoint
        (``duration_secs`` — a lower bound), label it ``'orphaned'``, and
        recompute that game's stats so the time counts and becomes eligible for
        store sync (``reported_at`` stays NULL → the sync drain picks it up).
        Orphans with no checkpoint (crashed before the first heartbeat) get 0
        duration: closed, but neither counted nor synced.
        """
        if self._db is None:
            return
        rows = self._db.query(
            """SELECT id, game_id, started_at, duration_secs, updated_at
               FROM play_sessions WHERE ended_at IS NULL""",
        )
        if not rows:
            return
        credited: set[int] = set()
        for row in rows:
            game_db_id = self._finalize_orphan_row(row)
            if game_db_id is not None:
                credited.add(game_db_id)
        for game_db_id in credited:
            self._refresh_game_stats(game_db_id)
        self._db._require_conn().commit()
        logger.info(
            "[PlaytimeService] Reconciled %d orphaned session(s); credited %d game(s)",
            len(rows), len(credited),
        )

    def _finalize_orphan_row(self, row: Any) -> int | None:
        """Close one crash-orphaned session row, crediting daily stats.

        Approximates the end at the last heartbeat (``updated_at``), falling
        back to ``started + duration`` then ``started`` so the row is always
        closed even with no checkpoint. Returns the game's db id when the
        duration was credited (the caller then refreshes its stats), else None.
        """
        if self._db is None:
            return None
        duration = int(row["duration_secs"] or 0)
        started = self._parse_iso(row["started_at"])
        ended = self._parse_iso(row["updated_at"])
        if ended is None and started is not None:
            ended = started + timedelta(seconds=duration)
        ended_dt = ended or started or datetime.now(UTC)
        ended_iso = ended_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self._db.execute(
            """UPDATE play_sessions
               SET ended_at = ?, duration_secs = ?, end_reason = 'orphaned',
                   updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE id = ?""",
            (ended_iso, duration, row["id"]),
        )
        if duration >= _MIN_SESSION_SECONDS and started is not None:
            self._update_daily_stats(row["game_id"], started, ended_dt, duration)
            return int(row["game_id"])
        return None

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        """Parse an ISO-8601 (``...Z``) timestamp to aware UTC, or None."""
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    @subscribe(Events.GAME_LAUNCHED)
    async def _on_game_launched(self, **kwargs: Any) -> None:
        """Record session start in ``_active`` under ``store:game_id``."""
        if self._db is None:
            return

        store = kwargs.get("store")
        game_id = kwargs.get("game_id")
        title = kwargs.get("title", "")
        app_id = kwargs.get("app_id", 0)

        if not store or not game_id:
            return

        key = f"{store}:{game_id}"

        if key in self._active:
            logger.warning("[PlaytimeService] Session already active for %s", key)
            return

        now = datetime.now(UTC)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        # Get or create game ID
        game_db_id = self._db.get_or_create_game(store, game_id, title, app_id)

        # Insert session with ended_at=NULL (active)
        cursor = self._db.execute(
            """INSERT INTO play_sessions
               (game_id, started_at, end_reason)
               VALUES (?, ?, 'unknown')""",
            (game_db_id, now_iso),
        )
        self._db._require_conn().commit()
        row_id = cursor.lastrowid

        self._active[key] = {
            "game_db_id": game_db_id,
            "title": title,
            # Wall clock, for calendar-day attribution in _update_daily_stats.
            "started_at": now,
            "db_row_id": row_id,
            # Suspend-excluding clock, for duration. See _provisional_duration.
            "started_monotonic": time.monotonic(),
        }

        logger.info("[PlaytimeService] Session started: %s (%s)", title, key)

    @subscribe(Events.GAME_STOPPED)
    async def _on_game_stopped(self, **kwargs: Any) -> None:
        """Delegate to ``_end_session(store, game_id)``."""
        store = kwargs.get("store")
        game_id = kwargs.get("game_id")

        if store and game_id:
            await self._end_session(store, game_id, end_reason="normal")

    # There are deliberately no SUSPEND/RESUME handlers. They existed here to
    # accumulate a per-session sleep total, subscribed to two events that no
    # code in the tree has ever emitted — there is no logind listener and no
    # SteamClient suspend hook. ``_provisional_duration`` now measures awake
    # time directly off CLOCK_MONOTONIC, which needs no signal and also covers
    # a hard suspend that would fire none. Both enum members were retired.

    async def get_playtime(self, store: str, game_id: str) -> dict[str, Any]:
        """Return cumulative playtime for a single game."""
        if self._db is None:
            return {}

        key = f"{store}:{game_id}"
        is_active = key in self._active

        row = self._db.query_one(
            """SELECT gs.total_secs, gs.total_sessions, gs.last_played_at,
                      gs.current_streak_days, gs.longest_streak_days,
                      sp.store_total_secs
               FROM games g
               JOIN game_stats gs ON g.id = gs.game_id
               LEFT JOIN store_playtime sp ON sp.game_id = g.id
               WHERE g.store = ? AND g.store_game_id = ?""",
            (store, game_id)
        )

        if row:
            return {
                "total_seconds": row["total_secs"],
                # Store's authoritative cross-device total (None until first
                # synced). The frontend prefers this over ``total_seconds``.
                "store_total_secs": row["store_total_secs"],
                "session_count": row["total_sessions"],
                "last_played": row["last_played_at"],
                "current_streak": row["current_streak_days"],
                "longest_streak": row["longest_streak_days"],
                "is_active": is_active,
            }

        return {
            "total_seconds": 0,
            "store_total_secs": None,
            "session_count": 0,
            "last_played": None,
            "current_streak": 0,
            "longest_streak": 0,
            "is_active": is_active,
        }

    async def get_all_playtimes(self) -> list[dict[str, Any]]:
        """Return cumulative playtime for every tracked game.

        Joins ``games`` x ``game_stats`` so each row carries both
        the canonical identity (``store`` + ``store_game_id``) and
        the aggregated stats the frontend needs to render the
        "Most played" list. Returns ``[]`` when the database is
        unavailable so callers don't have to guard the result.

        This method is the bulk equivalent of :meth:`get_playtime`
        — added because the RPC handlers (both the new
        ``rpc/handlers/launch.py`` and the legacy
        ``rpc/mixins/playtime.py``) referenced a ``get_all`` /
        ``get_all_playtimes`` method that didn't exist on the
        service. The RPC therefore always raised
        ``AttributeError`` and the frontend's "playtime stats"
        page could never load.
        """
        if self._db is None:
            return []

        rows = self._db.query(
            """SELECT g.store, g.store_game_id AS game_id, g.title,
                      gs.total_secs, gs.total_sessions, gs.last_played_at,
                      gs.current_streak_days, gs.longest_streak_days,
                      sp.store_total_secs
               FROM games g
               JOIN game_stats gs ON g.id = gs.game_id
               LEFT JOIN store_playtime sp ON sp.game_id = g.id
               ORDER BY gs.total_secs DESC""",
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            key = f"{row['store']}:{row['game_id']}"
            result.append({
                "store": row["store"],
                "game_id": row["game_id"],
                "title": row["title"],
                "total_seconds": row["total_secs"],
                "store_total_secs": row["store_total_secs"],
                "session_count": row["total_sessions"],
                "last_played": row["last_played_at"],
                "current_streak": row["current_streak_days"],
                "longest_streak": row["longest_streak_days"],
                "is_active": key in self._active,
            })
        return result

    async def _end_session(self, store: str, game_id: str, end_reason: str = "normal") -> None:
        """Record completed session + update totals."""
        if self._db is None:
            return

        key = f"{store}:{game_id}"
        session = self._active.pop(key, None)
        if not session:
            return

        now = datetime.now(UTC)
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        duration_secs = self._provisional_duration(session)

        if duration_secs < _MIN_SESSION_SECONDS:
            logger.debug("[PlaytimeService] Discarding short session (%ds) for %s", duration_secs, session["title"])
            if session["db_row_id"]:
                self._db.execute("DELETE FROM play_sessions WHERE id = ?", (session["db_row_id"],))
                self._db._require_conn().commit()
            return

        if session["db_row_id"]:
            # Update session
            self._db.execute(
                """UPDATE play_sessions
                   SET ended_at = ?, duration_secs = ?, end_reason = ?,
                       updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
                   WHERE id = ?""",
                (now_iso, duration_secs, end_reason, session["db_row_id"]),
            )

            # Update daily stats
            self._update_daily_stats(session["game_db_id"], session["started_at"], now, duration_secs)

            # Refresh materialized totals and streaks
            self._refresh_game_stats(session["game_db_id"])

            self._db._require_conn().commit()

        logger.info("[PlaytimeService] Session ended: %s (%ds)", session["title"], duration_secs)

        if self._bus:
            await self._bus.emit(
                Events.PLAYTIME_UPDATED,
                store=store,
                game_id=game_id,
                duration_secs=duration_secs,
            )
