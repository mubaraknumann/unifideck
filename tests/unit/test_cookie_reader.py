"""``auth.edge_browser.cookie_reader`` — reading cookies back out of a profile.

The reader is the inverse of :mod:`cookie_writer`, so the main check is a
round trip through the writer. The other cases cover what a real profile
holds besides that: the older layout without the host-hash prefix
(``meta.version`` < 24), keyring-encrypted ``v11`` rows (skipped), plain
``value`` rows, expired rows, other hosts, and a missing database.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import pytest

from unifideck.auth.edge_browser import cookie_reader as cr
from unifideck.auth.edge_browser import cookie_writer as cw

_SCHEMA = """
CREATE TABLE meta(key LONGVARCHAR NOT NULL UNIQUE PRIMARY KEY, value LONGVARCHAR);
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
"""


def _profile(tmp_path: Path, version: int) -> Path:
    default = tmp_path / "Default"
    default.mkdir()
    conn = sqlite3.connect(str(default / "Cookies"))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
    conn.commit()
    conn.close()
    return tmp_path


def _insert(profile: Path, host: str, name: str, value: str, blob: bytes,
            *, expires_offset_s: float = 86400.0) -> None:
    expires = cw._to_chrome_time(time.time() + expires_offset_s)
    conn = sqlite3.connect(str(profile / "Default" / "Cookies"))
    conn.execute(
        "INSERT INTO cookies VALUES (0,?,'',?,?,?,'/',?,1,1,0,1,1,1,0,2,443,0,0,0)",
        (host, name, value, blob, expires),
    )
    conn.commit()
    conn.close()


def _encrypt_without_host_hash(value: str | bytes) -> bytes:
    """Encrypt ``value`` as v10 with no host-hash prefix (older layout)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    plain = value.encode() if isinstance(value, str) else value
    pad = 16 - len(plain) % 16
    plain += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(cw._KEY), modes.CBC(cw._IV)).encryptor()
    return cw._V10_PREFIX + enc.update(plain) + enc.finalize()


def test_round_trip_with_cookie_writer(tmp_path: Path) -> None:
    profile = _profile(tmp_path, 24)
    cw.write_cookies(str(profile), [
        {"host": ".amazon.de", "name": "at-main", "value": "Atza|dummy-value"},
        {"host": "luna.amazon.de", "name": "lc-main", "value": "de_DE"},
        {"host": ".amazon.com", "name": "at-main", "value": "other-region"},
    ])

    cookies = cr.read_cookies(str(profile), "amazon.de")

    assert cookies == {"at-main": "Atza|dummy-value", "lc-main": "de_DE"}


def test_old_layout_without_host_hash(tmp_path: Path) -> None:
    profile = _profile(tmp_path, 23)
    _insert(profile, ".amazon.de", "session-token", "", _encrypt_without_host_hash("abc"))

    assert cr.read_cookies(str(profile), "amazon.de") == {"session-token": "abc"}


def test_missing_meta_version_strips_only_a_real_host_hash(tmp_path: Path) -> None:
    profile = _profile(tmp_path, 24)
    conn = sqlite3.connect(str(profile / "Default" / "Cookies"))
    conn.execute("DELETE FROM meta")
    conn.commit()
    conn.close()
    long_value = "x" * 40  # longer than the 32-byte hash, so the check matters
    _insert(profile, ".amazon.de", "plain-layout", "", _encrypt_without_host_hash(long_value))
    hashed = hashlib.sha256(b".amazon.de").digest() + b"v"
    _insert(profile, ".amazon.de", "hashed-layout", "", _encrypt_without_host_hash(hashed))

    cookies = cr.read_cookies(str(profile), "amazon.de")

    assert cookies == {"plain-layout": long_value, "hashed-layout": "v"}


def test_v11_and_garbage_rows_are_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    profile = _profile(tmp_path, 24)
    _insert(profile, ".amazon.de", "keyring", "", b"v11" + b"\x00" * 32)
    _insert(profile, ".amazon.de", "short", "", b"v10" + b"\x01\x02")
    _insert(profile, ".amazon.de", "badpad", "", _encrypt_without_host_hash("")[:3] + b"\x00" * 16)
    cw.write_cookies(str(profile), [{"host": ".amazon.de", "name": "good", "value": "ok"}])

    with caplog.at_level("INFO"):
        cookies = cr.read_cookies(str(profile), "amazon.de")

    assert cookies == {"good": "ok"}
    assert "skipped 3 undecryptable" in caplog.text
    # Values and names of skipped rows never reach the log.
    assert "keyring" not in caplog.text


def test_plain_value_and_expired_rows(tmp_path: Path) -> None:
    profile = _profile(tmp_path, 24)
    _insert(profile, ".amazon.de", "plain", "visible", b"")
    _insert(profile, ".amazon.de", "old", "gone", b"", expires_offset_s=-60)

    assert cr.read_cookies(str(profile), "amazon.de") == {"plain": "visible"}


def test_lookalike_hosts_do_not_match(tmp_path: Path) -> None:
    profile = _profile(tmp_path, 24)
    _insert(profile, ".notamazon.de", "a", "1", b"")
    _insert(profile, "amazon.de.evil.example", "b", "2", b"")
    _insert(profile, "amazon.de", "c", "3", b"")

    assert cr.read_cookies(str(profile), ".amazon.de") == {"c": "3"}


def test_missing_database_is_empty(tmp_path: Path) -> None:
    assert cr.read_cookies(str(tmp_path), "amazon.de") == {}


def test_unreadable_database_is_empty(tmp_path: Path) -> None:
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Cookies").write_bytes(b"not a database")
    assert cr.read_cookies(str(tmp_path), "amazon.de") == {}


def test_temp_copy_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "p").mkdir()
    profile = _profile(tmp_path / "p", 24)
    (profile / "Default" / "Cookies-journal").write_bytes(b"")
    cw.write_cookies(str(profile), [{"host": ".amazon.de", "name": "n", "value": "v"}])
    made: list[str] = []
    real_mkdtemp = cr.tempfile.mkdtemp

    def _mkdtemp(**kwargs: str) -> str:
        path = real_mkdtemp(dir=str(tmp_path), **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(cr.tempfile, "mkdtemp", _mkdtemp)

    assert cr.read_cookies(str(profile), "amazon.de") == {"n": "v"}
    assert len(made) == 1
    assert not Path(made[0]).exists()
