"""Crash-recovery tests for PlaytimeService: heartbeat + orphan reconciliation.

A game that hangs and is force-killed (or a hard system restart) never delivers
a clean ``GAME_STOPPED``, so its ``play_sessions`` row stays open
(``ended_at IS NULL``). Two mechanisms keep that from losing playtime or
accumulating zombie rows:

* **Heartbeat** (``_checkpoint_active``): every ~60s each active session
  persists a provisional ``duration_secs`` while leaving ``ended_at`` NULL, so
  the row is neither synced nor counted until it finalizes — but a crash can be
  recovered to that lower bound.
* **Reconciliation** (``_reconcile_orphans``, run on ``start()``): closes any
  ``ended_at IS NULL`` row as ``'orphaned'``, crediting the last checkpoint so
  the time counts and becomes eligible for store sync; rows with no checkpoint
  get 0 duration (closed, not counted/synced).
"""
from __future__ import annotations

import os
import tempfile
import time
from datetime import UTC, datetime, timedelta

from unifideck.event_bus.event_bus import EventBus
from unifideck.services.playtime.db import ActivityDatabase
from unifideck.services.playtime.service import PlaytimeService


def _new_service() -> PlaytimeService:
    """A PlaytimeService with an opened temp DB (no heartbeat task running)."""
    path = os.path.join(tempfile.mkdtemp(), "pt.db")
    svc = PlaytimeService(EventBus(), path)
    svc._db = ActivityDatabase(path)
    svc._db.open()
    return svc


def _open_session(
    db: ActivityDatabase, store: str, store_game_id: str, title: str,
    started: datetime, duration_secs: int | None, updated: datetime | None,
) -> tuple[int, int]:
    """Insert a game + an OPEN (ended_at NULL) session. Returns (game_id, sess_id)."""
    game_id = db.get_or_create_game(store, store_game_id, title, 0)
    cur = db.execute(
        """INSERT INTO play_sessions
           (game_id, started_at, duration_secs, end_reason, updated_at)
           VALUES (?, ?, ?, 'unknown', ?)""",
        (
            game_id,
            started.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            duration_secs,
            updated.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if updated else None,
        ),
    )
    db._require_conn().commit()
    return game_id, int(cur.lastrowid or 0)


def test_reconcile_credits_heartbeated_orphan() -> None:
    """An orphan with a checkpoint is closed 'orphaned' and its time counts."""
    svc = _new_service()
    start = datetime.now(UTC) - timedelta(minutes=15)
    last_beat = start + timedelta(seconds=600)
    game_id, sess_id = _open_session(
        svc._db, "gog", "1454587428", "Fallout: New Vegas",
        started=start, duration_secs=600, updated=last_beat,
    )

    svc._reconcile_orphans()

    row = svc._db.query_one(
        "SELECT ended_at, duration_secs, end_reason FROM play_sessions WHERE id = ?",
        (sess_id,),
    )
    assert row["ended_at"] is not None          # closed
    assert row["end_reason"] == "orphaned"
    assert row["duration_secs"] == 600

    stats = svc._db.query_one(
        "SELECT total_secs, total_sessions FROM game_stats WHERE game_id = ?",
        (game_id,),
    )
    assert stats is not None and stats["total_secs"] == 600   # credited

    # Eligible for store sync (ended_at set, duration >= 5, not yet reported).
    unreported = svc._db.get_unreported_sessions(("gog", "epic"))
    assert any(r["id"] == sess_id for r in unreported)


def test_reconcile_discards_orphan_without_checkpoint() -> None:
    """An orphan that crashed before the first heartbeat is closed with 0 time.

    This is the existing Witcher 3 case: no checkpoint data exists, so we cannot
    credit a duration. It is closed (not left dangling) but not counted/synced.
    """
    svc = _new_service()
    game_id, sess_id = _open_session(
        svc._db, "gog", "1495134320", "The Witcher 3",
        started=datetime.now(UTC) - timedelta(hours=9),
        duration_secs=None, updated=None,
    )

    svc._reconcile_orphans()

    row = svc._db.query_one(
        "SELECT ended_at, duration_secs, end_reason FROM play_sessions WHERE id = ?",
        (sess_id,),
    )
    assert row["ended_at"] is not None
    assert row["end_reason"] == "orphaned"
    assert row["duration_secs"] == 0

    # Not counted (no game_stats row) and not eligible for sync.
    assert svc._db.query_one(
        "SELECT 1 FROM game_stats WHERE game_id = ?", (game_id,),
    ) is None
    assert svc._db.get_unreported_sessions(("gog", "epic")) == []


def test_reconcile_is_idempotent_across_restarts() -> None:
    """A second reconcile pass must not double-count (ended_at is now set)."""
    svc = _new_service()
    start = datetime.now(UTC) - timedelta(minutes=10)
    game_id, _ = _open_session(
        svc._db, "gog", "42", "Game",
        started=start, duration_secs=300, updated=start + timedelta(seconds=300),
    )

    svc._reconcile_orphans()
    svc._reconcile_orphans()   # second startup — should be a no-op

    stats = svc._db.query_one(
        "SELECT total_secs FROM game_stats WHERE game_id = ?", (game_id,),
    )
    assert stats["total_secs"] == 300   # not 600


def test_checkpoint_writes_progress_without_finalizing() -> None:
    """Heartbeat persists duration but keeps the row open (not synced/counted)."""
    svc = _new_service()
    start = datetime.now(UTC) - timedelta(seconds=120)
    game_id, sess_id = _open_session(
        svc._db, "gog", "7", "Game", started=start,
        duration_secs=None, updated=None,
    )
    # Register it as the active session the heartbeat will checkpoint.
    # ``started_monotonic`` is the duration clock; ``started_at`` is only used
    # for calendar-day attribution, so it is backdated independently here.
    svc._active["gog:7"] = {
        "game_db_id": game_id,
        "title": "Game",
        "started_at": start,
        "db_row_id": sess_id,
        "started_monotonic": time.monotonic() - 120,
    }

    svc._checkpoint_active()

    row = svc._db.query_one(
        "SELECT ended_at, duration_secs FROM play_sessions WHERE id = ?",
        (sess_id,),
    )
    assert row["ended_at"] is None                 # still open
    assert 118 <= row["duration_secs"] <= 122      # ~120s elapsed
    # Open row must NOT be sync-eligible.
    assert svc._db.get_unreported_sessions(("gog", "epic")) == []
