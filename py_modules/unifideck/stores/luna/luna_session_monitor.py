"""LunaSessionMonitor: single source of truth for "is the Luna session
usable?" — reads the Amazon session cookies from the Luna profile's SQLite
cookie DB via a temp copy, and clears them on logout via ``cookie_writer.py``.

This replaces the Microsoft ``tokens/`` package (Luna has no tokens, no
access/refresh token and no token file). "Signed in" means *non-expired
Amazon session cookies exist in the Luna browser profile* (Req 2, F2).

Two sides, two mechanisms (design "LunaSessionMonitor"):

- **Read** (the usability predicate) copies the live ``Default/Cookies``
  SQLite DB to a temp file first — the browser locks the live DB — exactly
  the temp-copy read pattern ``EdgeProfileManager.has_xbox_session`` uses
  for the xCloud session check (F5, Req 8.10). The live profile is never
  opened for writing by this path.
- **Clear/logout** reuses the 0.7.7 ``auth/edge_browser/cookie_writer.py``
  helper rather than hand-rolling SQLite writes against the profile DB
  (Req 8.7).
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from unifideck.auth.edge_browser.cookie_writer import clear_cookies

if TYPE_CHECKING:
    from unifideck.auth.edge_browser.cdp_client import EdgeCDPClient
    from unifideck.stores.luna.luna_config import LunaConfig

logger = logging.getLogger(__name__)

#: Chromium stores ``expires_utc`` as microseconds since 1601-01-01 UTC,
#: not the Unix epoch. 11644473600 is the offset in seconds between the
#: two — the same constant ``cookie_writer._to_chrome_time`` uses, applied
#: in reverse here to compare an expiry against ``time.time()``.
_EPOCH_OFFSET_S = 11644473600

#: SQL ``LIKE`` fragments for the hosts a signed-in Amazon leaves cookies
#: on. A Luna session rides amazon.* cookies (``luna.amazon.*`` is served
#: the parent-domain ``.amazon.*`` session cookies), mirroring how the
#: Edge check matches ``%xbox.com%`` for the Xbox session.
_AMAZON_HOST_PATTERNS: tuple[str, ...] = ("%amazon.%",)

#: The cookies that actually carry a signed-in Amazon session. Any one of
#: these present and unexpired means the browser can reach a signed-in
#: amazon.* without re-auth; the tracking cookies (``session-id``,
#: ``lc-main``, …) that survive a logged-out profile are deliberately not
#: in this set. Mirrors ``probe_edge_session.SIGN_IN_COOKIES`` for Xbox and
#: Amazon auth-cookie FAMILIES, matched by prefix rather than exact name.
#: Amazon suffixes its session cookies with a per-marketplace token: the US
#: domain uses ``-main`` (``at-main``, ``x-main``, ``sess-at-main``) while
#: regional domains use ``-acb<cc>`` (``.de`` → ``at-acbde``, ``x-acbde``,
#: ``sess-at-acbde``; ``.co.uk`` → ``…-acbuk``; …). Matching the full name
#: only recognised the US variant, so a signed-in ``luna.amazon.de`` session
#: read back as "not signed in" and the launch ended ``sign_in_closed``.
#: Prefix matching covers every marketplace.
_AMAZON_SESSION_COOKIE_PREFIXES: tuple[str, ...] = (
    "at-",       # at-main / at-acbde / …
    "sess-at-",  # sess-at-main / sess-at-acbde / sess-at-acb-main / …
    "x-",        # x-main / x-acbde / …
)

#: Full-name session cookies (no marketplace suffix) that also count.
_AMAZON_SESSION_COOKIE_NAMES: frozenset[str] = frozenset({
    "session-token",
})


def _is_amazon_session_cookie(name: str) -> bool:
    """Whether ``name`` is an Amazon auth/session cookie (any marketplace)."""
    if name in _AMAZON_SESSION_COOKIE_NAMES:
        return True
    return any(name.startswith(p) for p in _AMAZON_SESSION_COOKIE_PREFIXES)

#: The read must finish within 5 s; a slower read (locked/huge DB) is
#: reported as ``unknown`` and the profile is left untouched (Req 2.6).
_READ_BUDGET_S = 5.0

#: URL/title markers that mean the Luna web app bounced us to an Amazon
#: sign-in page mid-launch — the session expired while streaming (Req 2.5).
_SIGNIN_URL_MARKERS: tuple[str, ...] = ("/ap/signin", "/ap/oa", "ap/signin")


@dataclass(frozen=True)
class SessionState:
    """The outcome of reading the Luna profile's session cookies.

    ``status`` is the usability verdict; ``cookie_count`` is how many
    Amazon session cookies were found; ``nearest_expiry_epoch`` is the
    soonest expiry (Unix seconds) across those cookies, or ``None`` when
    none were found or the read could not complete.
    """

    status: Literal["usable", "expired", "unknown"]
    cookie_count: int
    nearest_expiry_epoch: int | None


class LunaSessionMonitor:
    """Reads and clears the Luna profile's Amazon session cookies.

    The single source of truth for "is the Luna session usable?". It owns
    no browser process and no CDP connection of its own; the read is a
    filesystem operation against a *copy* of the cookie DB, and the clear
    goes through ``cookie_writer.py``.
    """

    def __init__(self, config: LunaConfig) -> None:
        """Bind the monitor to a resolved :class:`LunaConfig`."""
        self._config = config

    # ── Read side (usability predicate) ──────────────────────────────

    def _cookie_db(self) -> Path:
        """Path to the Luna profile's live ``Default/Cookies`` DB.

        ``~`` is expanded: ``LunaConfig.profile_dir`` is the raw configured
        value (e.g. ``~/.local/share/unifideck/luna-profile``), and without
        expansion this pointed at a literal ``~`` dir that never exists, so a
        signed-in session read back as "not connected".
        """
        raw = self._config.profile_dir or ""
        return Path(raw).expanduser() / "Default" / "Cookies"

    def read_session(self) -> SessionState:
        """Read the Amazon session cookies and classify the session.

        Usable iff at least one Amazon session cookie is present AND its
        expiry is in the future; absent or all-expired → ``expired``
        (Req 2.4). A read that cannot complete within 5 s or hits an
        unreadable DB → ``unknown`` with the profile left untouched and a
        retry left to the caller (Req 2.6). The live DB is read only via a
        temp copy so a running browser never sees a lock (F5, Req 8.10).
        """
        cookie_db = self._cookie_db()
        if not cookie_db.exists():
            # No profile/DB yet → no session has ever been written.
            return SessionState(status="expired", cookie_count=0,
                                nearest_expiry_epoch=None)

        started = time.monotonic()
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".db", delete=False,
            ) as tmp:
                tmp_path = tmp.name
            shutil.copy2(str(cookie_db), tmp_path)
            if time.monotonic() - started > _READ_BUDGET_S:
                raise TimeoutError("cookie copy exceeded read budget")
            expiries = self._read_session_expiries(tmp_path)
        except Exception as e:
            logger.debug("[LunaSession] could not read cookie DB: %s", e)
            return SessionState(status="unknown", cookie_count=0,
                                nearest_expiry_epoch=None)
        finally:
            if tmp_path and Path(tmp_path).exists():
                Path(tmp_path).unlink()

        if time.monotonic() - started > _READ_BUDGET_S:
            logger.debug("[LunaSession] cookie read exceeded 5s budget")
            return SessionState(status="unknown", cookie_count=0,
                                nearest_expiry_epoch=None)

        return self._classify(expiries)

    def _read_session_expiries(self, db_path: str) -> list[int]:
        """Return the Unix-epoch expiry of each Amazon session cookie.

        Reads the temp *copy* read-only. A session cookie with no expiry
        (``expires_utc == 0``, dropped when the browser exits) is not a
        persisted session and is excluded.
        """
        host_clause = " OR ".join(["host_key LIKE ?"] * len(_AMAZON_HOST_PATTERNS))
        query = (
            "SELECT name, expires_utc FROM cookies "  # noqa: S608 — host_clause is a fixed count of placeholders
            f"WHERE ({host_clause})"
        )
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            rows = conn.execute(query, _AMAZON_HOST_PATTERNS).fetchall()
        finally:
            conn.close()
        expiries: list[int] = []
        for name, expires_utc in rows:
            if not _is_amazon_session_cookie(name):
                continue
            if not expires_utc:
                continue  # session-only cookie — not a persisted session
            expiries.append(self._to_unix_epoch(int(expires_utc)))
        return expiries

    @staticmethod
    def _to_unix_epoch(chrome_us: int) -> int:
        """Convert Chromium's 1601-based microseconds to Unix seconds."""
        return int(chrome_us / 1_000_000) - _EPOCH_OFFSET_S

    @staticmethod
    def _classify(expiries: list[int]) -> SessionState:
        """Apply the usability predicate to a set of cookie expiries."""
        if not expiries:
            return SessionState(status="expired", cookie_count=0,
                                nearest_expiry_epoch=None)
        now = int(time.time())
        future = [e for e in expiries if e > now]
        nearest = min(expiries)
        if future:
            return SessionState(
                status="usable",
                cookie_count=len(expiries),
                nearest_expiry_epoch=nearest,
            )
        return SessionState(
            status="expired",
            cookie_count=len(expiries),
            nearest_expiry_epoch=nearest,
        )

    def is_usable(self) -> bool:
        """True iff the session is usable (non-expired Amazon cookies).

        A convenience over :meth:`read_session`; ``unknown`` is not usable
        (the caller surfaces session-unknown + retry rather than treating
        an unreadable DB as a valid session).
        """
        return self.read_session().status == "usable"

    # ── Clear side (logout) ──────────────────────────────────────────

    def clear_cookies(self) -> bool:
        """Delete the Luna profile's Amazon cookies (logout, Req 8.7).

        Goes through ``cookie_writer.clear_cookies`` rather than opening
        the profile DB here. Returns True when the clear ran without
        error (including the no-op case of nothing to delete). Never call
        while the Luna browser is running — it owns the DB.
        """
        profile_dir = self._config.profile_dir
        if not profile_dir:
            logger.warning("[LunaSession] no profile_dir configured; "
                           "cannot clear cookies")
            return False
        clear_cookies(
            str(Path(profile_dir).expanduser()), list(_AMAZON_HOST_PATTERNS),
        )
        return True

    # ── In-launch expiry detection ───────────────────────────────────

    def looks_like_signin_page(self, cdp: EdgeCDPClient) -> bool:
        """True if the browser is currently showing an Amazon sign-in page.

        The play-activator calls this during a launch: a session can
        expire while streaming, and the Luna web app then presents an
        Amazon sign-in page (Req 2.5). Detected by the URL of the live
        page targets — a sign-in bounce lands on an ``/ap/signin`` URL.
        Best-effort and read-only; returns False if the targets cannot be
        read (the launch keeps going rather than aborting on a probe).
        """
        try:
            targets = cdp.list_targets()
        except Exception as e:
            logger.debug("[LunaSession] could not list CDP targets: %s", e)
            return False
        for target in targets:
            if target.get("type") != "page":
                continue
            url = str(target.get("url", "")).lower()
            if any(marker in url for marker in _SIGNIN_URL_MARKERS):
                logger.info("[LunaSession] sign-in page detected: %s",
                            target.get("url"))
                return True
        return False
