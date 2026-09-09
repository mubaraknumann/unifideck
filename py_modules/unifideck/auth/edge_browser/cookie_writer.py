"""auth.edge_browser.cookie_writer — plant cookies in the Edge profile.

Some stores authenticate in a way that leaves the shared Edge profile
WITHOUT a usable web session, even though the plugin holds valid
credentials. Amazon is the case that forced this module: nile signs in
through Amazon's *device registration* flow, which authorises the
device and hands back tokens, but leaves the browser with only
tracking cookies (``session-id``, ``lc-main``, …) and none of the
auth cookies a signed-in amazon.com needs. Opening the shop in that
profile therefore showed a logged-out page.

Amazon's own apps solve this by exchanging a refresh token for website
cookies; this module is the other half — writing them where Edge will
read them.

The encryption scheme
---------------------
Chromium stores cookie values in ``encrypted_value``, not ``value``.
On Linux the scheme depends on which password store is in use, and we
do not have to guess: ``_BASE_FLAGS`` passes ``--password-store=basic``,
which pins it to the ``v10`` variant with a well-known key —
PBKDF2-HMAC-SHA1 of the literal password ``peanuts``, salt
``saltysalt``, one iteration, 16 bytes — AES-128-CBC with an IV of 16
spaces.

Recent Chromium also prepends a 32-byte SHA-256 of the host to the
plaintext before encrypting, so a cookie stolen from one domain cannot
be replayed on another. Verified against this profile's own cookies
rather than assumed: decrypting ``i18n-prefs`` yields the host hash
followed by ``INR``. :func:`_encrypt` reproduces that layout.

Never write while Edge is running — it owns the DB and rewrites it on
exit, so a concurrent write is both a lock risk and pointless.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Chromium's "basic" (no-keyring) password store. Not a secret: it is a
# published constant, and it is why --password-store=basic matters.
_V10_PREFIX = b"v10"
_KEY = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
_IV = b" " * 16
_BLOCK = 16

# Chromium timestamps are microseconds since 1601-01-01, not the Unix
# epoch. 11644473600 is the offset in seconds between the two.
_EPOCH_OFFSET_S = 11644473600


def _to_chrome_time(unix_seconds: float) -> int:
    """Convert a Unix timestamp to Chromium's 1601-based microseconds."""
    return int((unix_seconds + _EPOCH_OFFSET_S) * 1_000_000)


def _encrypt(host: str, value: str) -> bytes:
    """Encrypt ``value`` the way Chromium's basic password store does."""
    from cryptography.hazmat.primitives.ciphers import (
        Cipher,
        algorithms,
        modes,
    )

    plain = hashlib.sha256(host.encode()).digest() + value.encode()
    pad = _BLOCK - (len(plain) % _BLOCK)
    plain += bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).encryptor()
    return _V10_PREFIX + encryptor.update(plain) + encryptor.finalize()


def _row(cookie: dict[str, Any], now_us: int, expires_us: int) -> tuple[Any, ...]:
    """One INSERT row, in the cookies table's column order."""
    host = str(cookie["host"])
    return (
        now_us,                        # creation_utc
        host,                          # host_key
        "",                            # top_frame_site_key
        str(cookie["name"]),           # name
        "",                            # value — empty; the real one is
                                       #   in encrypted_value
        _encrypt(host, str(cookie["value"])),
        str(cookie.get("path") or "/"),
        expires_us,                    # expires_utc
        1 if cookie.get("secure", True) else 0,
        1 if cookie.get("httponly", False) else 0,
        now_us,                        # last_access_utc
        1,                             # has_expires
        1,                             # is_persistent — must survive the
                                       #   browser exit, or this was pointless
        1,                             # priority: medium
        0,                             # samesite: unspecified
        2,                             # source_scheme: https
        443,                           # source_port
        now_us,                        # last_update_utc
        0,                             # source_type
        0,                             # has_cross_site_ancestor
    )


# Clear every prior copy of this (host, name) before inserting.
#
# ``INSERT OR REPLACE`` alone is not enough: the unique index is
# ``(host_key, top_frame_site_key, has_cross_site_ancestor, name, path,
# source_scheme, source_port)``, so a row differing only in
# ``has_cross_site_ancestor`` does not collide — and a fresh insert
# leaves the stale one in place. Observed: writing ``ubid-main`` left
# TWO rows, and Chromium would then send a stale value alongside the
# new one. Deleting by the pair the site actually cares about avoids
# depending on the index's exact column list, which has changed across
# Chromium versions before.
_DELETE = "DELETE FROM cookies WHERE host_key = ? AND name = ?"

_INSERT = """
INSERT OR REPLACE INTO cookies (
    creation_utc, host_key, top_frame_site_key, name, value,
    encrypted_value, path, expires_utc, is_secure, is_httponly,
    last_access_utc, has_expires, is_persistent, priority, samesite,
    source_scheme, source_port, last_update_utc, source_type,
    has_cross_site_ancestor
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def write_cookies(
    profile_dir: str,
    cookies: list[dict[str, Any]],
    *,
    ttl_days: int = 365,
) -> int:
    """Write ``cookies`` into the profile's store. Returns how many landed.

    Each entry needs ``host``, ``name`` and ``value``; ``path``,
    ``secure`` and ``httponly`` are optional. Existing cookies with the
    same (host, name, path) are replaced.

    Best-effort: any failure is logged and reported as ``0`` rather than
    raised. A shop that opens signed out is a far better outcome than a
    cart button that errors.
    """
    db = Path(profile_dir) / "Default" / "Cookies"
    if not db.exists():
        logger.info("[cookie_writer] no cookie DB at %s", db)
        return 0
    if not cookies:
        return 0
    now = time.time()
    now_us = _to_chrome_time(now)
    expires_us = _to_chrome_time(now + ttl_days * 86400)
    try:
        conn = sqlite3.connect(str(db), timeout=5)
        try:
            conn.executemany(
                _DELETE,
                [(str(c["host"]), str(c["name"])) for c in cookies],
            )
            rows = [_row(c, now_us, expires_us) for c in cookies]
            conn.executemany(_INSERT, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[cookie_writer] could not write cookies: %s", e)
        return 0
    logger.info(
        "[cookie_writer] wrote %d cookie(s): %s",
        len(cookies), ", ".join(sorted(str(c["name"]) for c in cookies)),
    )
    return len(cookies)
