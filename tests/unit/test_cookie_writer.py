"""``auth.edge_browser.cookie_writer`` — planting cookies Edge will read.

Amazon's sign-in is a device-registration flow: it authorises the
device but leaves the browser without the auth cookies a signed-in
amazon.com needs, so the shop opened logged out. The fix exchanges
nile's refresh token for website cookies; this module writes them.

Two things here are easy to get subtly wrong and impossible to notice
without a round trip:

* **The encryption layout.** Chromium's basic password store is
  ``v10`` + AES-128-CBC, and recent versions prepend a SHA-256 of the
  host so a cookie cannot be replayed on another domain. Writing the
  value without that prefix produces a row that decrypts to garbage —
  and a browser that silently ignores it.
* **Replacing, not accumulating.** The unique index includes
  ``has_cross_site_ancestor``, so ``INSERT OR REPLACE`` alone leaves a
  pre-existing row in place and Chromium then has two values for one
  cookie. Observed with ``ubid-main`` against the real profile.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from unifideck.auth.edge_browser import cookie_writer as cw

# Chromium's schema, trimmed to the columns the writer touches plus the
# real unique index — that index is the reason the delete-first exists.
_SCHEMA = """
CREATE TABLE cookies(
    creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL,
    top_frame_site_key TEXT NOT NULL, name TEXT NOT NULL,
    value TEXT NOT NULL, encrypted_value BLOB NOT NULL,
    path TEXT NOT NULL, expires_utc INTEGER NOT NULL,
    is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL,
    last_access_utc INTEGER NOT NULL, has_expires INTEGER NOT NULL,
    is_persistent INTEGER NOT NULL, priority INTEGER NOT NULL,
    samesite INTEGER NOT NULL, source_scheme INTEGER NOT NULL,
    source_port INTEGER NOT NULL, last_update_utc INTEGER NOT NULL,
    source_type INTEGER NOT NULL, has_cross_site_ancestor INTEGER NOT NULL);
CREATE UNIQUE INDEX cookies_unique_index ON cookies(
    host_key, top_frame_site_key, has_cross_site_ancestor, name, path,
    source_scheme, source_port);
"""

_COOKIE = {
    "host": ".amazon.com", "name": "at-main", "value": "Atza|secret",
    "path": "/", "secure": True, "httponly": True,
}


@pytest.fixture
def profile(tmp_path):
    """A profile directory holding an empty Chromium cookie DB."""
    default = tmp_path / "Default"
    default.mkdir()
    conn = sqlite3.connect(str(default / "Cookies"))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return tmp_path


def _rows(profile, name: str) -> list[tuple]:
    conn = sqlite3.connect(str(profile / "Default" / "Cookies"))
    try:
        return conn.execute(
            "select host_key, encrypted_value, is_persistent, has_expires "
            "from cookies where name = ?", (name,),
        ).fetchall()
    finally:
        conn.close()


def _decrypt(blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import (
        Cipher,
        algorithms,
        modes,
    )

    decryptor = Cipher(
        algorithms.AES(cw._KEY), modes.CBC(cw._IV),
    ).decryptor()
    plain = decryptor.update(blob[3:]) + decryptor.finalize()
    return plain[:-plain[-1]]


def test_a_written_cookie_decrypts_back_to_its_value(profile) -> None:
    """The whole point: Edge must be able to read what we wrote."""
    assert cw.write_cookies(str(profile), [_COOKIE]) == 1

    (host, blob, _, _), = _rows(profile, "at-main")
    assert blob.startswith(b"v10")
    plain = _decrypt(blob)
    assert plain.startswith(hashlib.sha256(host.encode()).digest()), (
        "missing the host prefix — Chromium would reject the value"
    )
    assert plain[32:] == b"Atza|secret"


def test_the_plaintext_value_column_stays_empty(profile) -> None:
    """A value in both columns is a credential leaked in cleartext."""
    cw.write_cookies(str(profile), [_COOKIE])

    conn = sqlite3.connect(str(profile / "Default" / "Cookies"))
    try:
        (value,), = conn.execute(
            "select value from cookies where name = 'at-main'",
        ).fetchall()
    finally:
        conn.close()
    assert value == ""


def test_cookies_are_persistent_so_they_survive_the_browser(profile) -> None:
    """A session cookie would die on exit, making the write pointless."""
    cw.write_cookies(str(profile), [_COOKIE])

    (_, _, persistent, has_expires), = _rows(profile, "at-main")
    assert persistent == 1
    assert has_expires == 1


def test_writing_twice_replaces_rather_than_accumulates(profile) -> None:
    """The bug the delete-first exists for.

    ``INSERT OR REPLACE`` alone left a second ``ubid-main`` row, because
    the unique index includes ``has_cross_site_ancestor``. Chromium then
    sends a stale value alongside the fresh one.
    """
    cw.write_cookies(str(profile), [_COOKIE])
    cw.write_cookies(str(profile), [{**_COOKIE, "value": "Atza|newer"}])

    rows = _rows(profile, "at-main")
    assert len(rows) == 1
    assert _decrypt(rows[0][1])[32:] == b"Atza|newer"


def test_a_row_differing_only_by_ancestor_flag_is_still_replaced(
    profile,
) -> None:
    """Exactly the shape that slipped past INSERT OR REPLACE."""
    conn = sqlite3.connect(str(profile / "Default" / "Cookies"))
    conn.execute(
        "insert into cookies values (0,'.amazon.com','','at-main','',"
        "x'0000','/',0,1,1,0,1,1,1,0,2,443,0,0,1)",   # ancestor flag = 1
    )
    conn.commit()
    conn.close()

    cw.write_cookies(str(profile), [_COOKIE])

    assert len(_rows(profile, "at-main")) == 1


def test_a_missing_profile_is_not_an_error(tmp_path) -> None:
    """A shop that opens signed out beats a cart button that throws."""
    assert cw.write_cookies(str(tmp_path / "nope"), [_COOKIE]) == 0


def test_an_empty_cookie_list_is_a_no_op(profile) -> None:
    assert cw.write_cookies(str(profile), []) == 0
