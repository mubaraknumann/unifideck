"""Property test for the Luna session-usability predicate (task 2.2).

Feature: amazon-luna-streaming, Property 7: Session-usability predicate.

Property 7: For any set of cookies in the Luna profile with varying presence
and expiry timestamps, ``LunaSessionMonitor`` reports the session usable iff at
least one Amazon session cookie is present with an expiry in the future;
otherwise expired.

Validates: Requirements 2.2, 2.4

The test builds a real Chromium-shaped ``Default/Cookies`` SQLite DB from a
Hypothesis-generated cookie set and exercises the *real* temp-copy read path
(``LunaSessionMonitor.read_session``) — no mock of the monitor itself, only an
in-memory/on-disk cookie store standing in for a real Amazon/browser profile.
The verdict is checked against an independent oracle derived straight from the
predicate wording, so the test never re-implements the monitor's SQL.

≥100 iterations (Hypothesis ``max_examples=200``).
"""
from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from unifideck.stores.luna.luna_config import LunaConfig
from unifideck.stores.luna.luna_session_monitor import (
    _EPOCH_OFFSET_S,
    _is_amazon_session_cookie,
    LunaSessionMonitor,
)

# A generated cookie: (name, host_key, kind) where ``kind`` selects the
# expiry regime so expiries never land near "now" (avoids a wall-clock race
# at the predicate boundary — the predicate itself is tested, not the clock).
_KIND_FUTURE = "future"   # unexpired persisted cookie
_KIND_PAST = "past"       # expired persisted cookie
_KIND_SESSION = "session"  # expires_utc == 0 → session-only, not persisted

# One clear year on either side of now, expressed in Chrome microseconds
# (microseconds since 1601-01-01 UTC), the unit the real cookie DB stores.
_YEAR_S = 365 * 24 * 60 * 60


def _chrome_us(unix_epoch_s: int) -> int:
    """Unix seconds → Chromium 1601-based microseconds (DB storage unit)."""
    return (unix_epoch_s + _EPOCH_OFFSET_S) * 1_000_000


# Amazon session-cookie names (the ones that count) across marketplaces —
# the US ``-main`` variants AND the regional ``-acb<cc>`` variants a signed-in
# ``luna.amazon.de`` actually writes (at-acbde / x-acbde / sess-at-acbde), the
# real case that regressed. Plus decoys that must be ignored: non-session
# Amazon cookies, and a session cookie on a non-Amazon host.
_SESSION_NAMES = [
    "at-main", "sess-at-main", "x-main", "sess-at-acb-main", "session-token",
    "at-acbde", "sess-at-acbde", "x-acbde",
    "at-acbuk", "x-acbuk",
]
_DECOY_NAMES = ["session-id", "lc-main", "ubid-main", "ubid-acbde"]

_AMAZON_HOSTS = [".amazon.de", ".amazon.com", "luna.amazon.de", ".amazon.co.uk"]
_NON_AMAZON_HOSTS = [".xbox.com", "example.com", ".google.com"]

_names = st.sampled_from(_SESSION_NAMES + _DECOY_NAMES)
_hosts = st.sampled_from(_AMAZON_HOSTS + _NON_AMAZON_HOSTS)
_kinds = st.sampled_from([_KIND_FUTURE, _KIND_PAST, _KIND_SESSION])

_cookie = st.tuples(_names, _hosts, _kinds)
_cookie_sets = st.lists(_cookie, min_size=0, max_size=12)


def _host_matches_amazon(host: str) -> bool:
    """Mirror the SQL ``host_key LIKE '%amazon.%'`` match in Python."""
    return "amazon." in host


def _expected_usable(cookies: list[tuple[str, str, str]]) -> bool:
    """Independent oracle for the predicate (Req 2.2 / 2.4).

    Usable iff at least one Amazon *session* cookie is present on an
    amazon.* host with a persisted (non-zero) expiry that is in the future.
    """
    for name, host, kind in cookies:
        if not _is_amazon_session_cookie(name):
            continue
        if not _host_matches_amazon(host):
            continue
        if kind == _KIND_SESSION:
            continue  # expires_utc == 0 — not a persisted session
        if kind == _KIND_FUTURE:
            return True
    return False


def _write_cookie_db(db_path: Path, cookies: list[tuple[str, str, str]],
                     now_s: int) -> None:
    """Create a Chromium-shaped ``Cookies`` DB holding ``cookies``."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE cookies ("
            "name TEXT, host_key TEXT, expires_utc INTEGER)"
        )
        for name, host, kind in cookies:
            if kind == _KIND_SESSION:
                expires_utc = 0
            elif kind == _KIND_FUTURE:
                expires_utc = _chrome_us(now_s + _YEAR_S)
            else:  # _KIND_PAST
                expires_utc = _chrome_us(now_s - _YEAR_S)
            conn.execute(
                "INSERT INTO cookies (name, host_key, expires_utc) "
                "VALUES (?, ?, ?)",
                (name, host, expires_utc),
            )
        conn.commit()
    finally:
        conn.close()


@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(cookies=_cookie_sets)
def test_session_usability_predicate(tmp_path_factory, cookies):
    """Monitor reports usable iff an unexpired Amazon session cookie exists."""
    import time

    now_s = int(time.time())
    profile = tmp_path_factory.mktemp("luna-profile")
    _write_cookie_db(profile / "Default" / "Cookies", cookies, now_s)

    config = replace(LunaConfig(), profile_dir=str(profile))
    monitor = LunaSessionMonitor(config)

    state = monitor.read_session()
    expected_usable = _expected_usable(cookies)

    if expected_usable:
        assert state.status == "usable", (cookies, state)
        assert monitor.is_usable() is True
    else:
        # No unexpired Amazon session cookie → expired (never "unknown":
        # the DB is readable and well under the 5s budget here).
        assert state.status == "expired", (cookies, state)
        assert monitor.is_usable() is False
