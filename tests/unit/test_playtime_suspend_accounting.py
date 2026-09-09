"""Playtime duration must exclude suspend, and must not depend on wall clock.

Audit §1.3. ``PlaytimeService`` used to bill a session as wall-clock elapsed
minus a sleep total accumulated by ``SUSPEND``/``RESUME`` bus handlers. Nothing
in the tree has ever emitted those two events, so the sleep total was
permanently 0: putting the Deck down mid-game billed the whole suspend as
playtime, and ``PlaytimeSyncService`` pushed that number to GOG/Epic, where it
cannot be retracted.

The fix measures awake time off ``time.monotonic()`` — CLOCK_MONOTONIC does not
advance while the machine is suspended — so no signal is needed and a hard
suspend that fires nothing is covered too.

These tests pin the property, not the implementation detail: a suspend gap is
simulated by advancing the *wall* clock without advancing the monotonic clock,
which is exactly what a real suspend does to a running process. A regression to
any wall-clock-derived duration fails ``test_suspend_gap_is_not_billed``.
"""
from __future__ import annotations

import os
import tempfile
import time
from datetime import UTC, datetime, timedelta

from unifideck.core.types import Events
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


def _session(*, wall_ago_secs: float, awake_secs: float) -> dict[str, object]:
    """An active-session dict whose wall span and awake span disagree.

    ``wall_ago_secs > awake_secs`` is the shape a suspend leaves behind: the
    process was alive for the whole wall span but only running for part of it.
    """
    return {
        "game_db_id": 1,
        "title": "Game",
        "started_at": datetime.now(UTC) - timedelta(seconds=wall_ago_secs),
        "db_row_id": 1,
        "started_monotonic": time.monotonic() - awake_secs,
    }


def test_suspend_gap_is_not_billed() -> None:
    """3 min played across a 10 min suspend bills ~3 min, not ~13."""
    session = _session(wall_ago_secs=780, awake_secs=180)

    duration = PlaytimeService._provisional_duration(session)

    assert 178 <= duration <= 185, (
        f"expected ~180s of awake time, got {duration}s — a duration near 780 "
        "means the calculation went back to wall clock"
    )


def test_duration_ignores_started_at_entirely() -> None:
    """``started_at`` is for calendar-day attribution only, never for duration.

    Two sessions with the same awake span but wildly different wall spans must
    bill the same. This is what stops a future edit from quietly reintroducing
    ``started_at`` into the duration path.
    """
    short_wall = PlaytimeService._provisional_duration(
        _session(wall_ago_secs=60, awake_secs=60),
    )
    long_wall = PlaytimeService._provisional_duration(
        _session(wall_ago_secs=36_000, awake_secs=60),
    )

    assert short_wall == long_wall


def test_duration_never_negative() -> None:
    """A clock reading that lands before the anchor floors at 0, not below."""
    session = _session(wall_ago_secs=10, awake_secs=-5)

    assert PlaytimeService._provisional_duration(session) == 0


def test_checkpoint_bills_awake_time() -> None:
    """The heartbeat writes awake seconds, so a crash recovers the right bound.

    ``_reconcile_orphans`` credits whatever the last checkpoint wrote, so a
    wall-clock checkpoint would launder the suspend gap into the DB even though
    ``_end_session`` is correct.
    """
    svc = _new_service()
    game_id = svc._db.get_or_create_game("gog", "7", "Game", 0)
    cur = svc._db.execute(
        """INSERT INTO play_sessions (game_id, started_at, end_reason)
           VALUES (?, ?, 'unknown')""",
        (game_id, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")),
    )
    svc._db._require_conn().commit()
    sess_id = int(cur.lastrowid or 0)

    session = _session(wall_ago_secs=780, awake_secs=180)
    session["game_db_id"] = game_id
    session["db_row_id"] = sess_id
    svc._active["gog:7"] = session  # type: ignore[assignment]

    svc._checkpoint_active()

    row = svc._db.query_one(
        "SELECT ended_at, duration_secs FROM play_sessions WHERE id = ?",
        (sess_id,),
    )
    assert row["ended_at"] is None
    assert 178 <= row["duration_secs"] <= 185


def test_suspend_and_resume_events_are_gone() -> None:
    """The retired members must not come back without a real emitter.

    Re-declaring them is how the original bug shipped: a subscriber was written
    against events that were never emitted, and the asymmetry with the rest of
    the enum made the wiring look complete.
    """
    names = {e.name for e in Events}
    assert "SUSPEND" not in names
    assert "RESUME" not in names
