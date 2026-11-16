"""auth.edge_browser.cookie_reader — read cookies back out of the profile.

The counterpart of :mod:`cookie_writer`. A store whose web API accepts the
browser's session cookies (Luna's ``/token`` endpoint) needs those cookies
outside the browser, so this module decrypts the profile's cookie store.

``_BASE_FLAGS`` passes ``--password-store=basic``, which pins every value to
the ``v10`` scheme with the published ``peanuts`` key, so no keyring is
involved: :mod:`cookie_writer` owns the key, the IV and the ``v10`` prefix and
this module reuses them. A ``v11`` row (a keyring-encrypted value written by a
browser that was not started with that flag) cannot be decrypted here and is
skipped.

Recent Chromium prepends the SHA-256 of the host to the plaintext (cookie
database ``meta.version`` 24 and later); older profiles do not. The version is
read from the ``meta`` table, and the host hash is only stripped when it
applies.

The browser owns the live database, so it is never opened in place: the file
(plus its journal / WAL sidecars) is copied into a private temporary directory,
read there and deleted again. Cookie values are never logged.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from unifideck.auth.edge_browser.cookie_writer import (
    _BLOCK,
    _EPOCH_OFFSET_S,
    _IV,
    _KEY,
    _V10_PREFIX,
)

logger = logging.getLogger(__name__)

#: ``meta.version`` from which Chromium prefixes the plaintext with the host hash.
_HOST_HASH_VERSION = 24
_HOST_HASH_LEN = 32

_SIDECARS = ("-journal", "-wal", "-shm")

_T = TypeVar("_T")

_SELECT = (
    "SELECT host_key, name, value, encrypted_value FROM cookies "
    "WHERE host_key LIKE ? AND (has_expires = 0 OR expires_utc > ?) "
    "ORDER BY length(path) DESC, length(host_key) DESC"
)


def read_cookies(profile_dir: str, host_suffix: str) -> dict[str, str]:
    """Cookies for ``host_suffix`` (e.g. ``amazon.de``) as a name -> value map.

    Matches the host itself and every subdomain, skips expired rows and
    rows that cannot be decrypted, and prefers the most specific path/host
    when a name occurs more than once. Returns ``{}`` when the profile has
    no cookie database or it cannot be read; never raises, never logs values.
    """
    suffix = host_suffix.lstrip(".").lower()
    found = _with_private_copy(profile_dir, lambda copy: _read_copy(copy, suffix))
    return found if found is not None else {}


def cookie_hosts(profile_dir: str, name_prefixes: tuple[str, ...]) -> set[str]:
    """Hosts holding an unexpired cookie whose name starts with a prefix.

    Reads names and hosts only: nothing is decrypted, so no value can leak.
    Returns an empty set when the profile has no readable cookie database.
    """
    found = _with_private_copy(
        profile_dir, lambda copy: _hosts_in_copy(copy, name_prefixes),
    )
    return found if found is not None else set()


def _with_private_copy(profile_dir: str, read: Callable[[Path], _T]) -> _T | None:
    """Run ``read`` on a private copy of the profile's cookie database.

    ``None`` when the profile has no cookie database or it cannot be read.
    """
    db = Path(profile_dir) / "Default" / "Cookies"
    if not db.exists():
        logger.info("[cookie_reader] no cookie DB at %s", db)
        return None
    work = Path(tempfile.mkdtemp(prefix="unifideck-cookies-"))
    try:
        copy = work / "Cookies"
        shutil.copy2(db, copy)
        for sidecar in _SIDECARS:
            src = Path(str(db) + sidecar)
            if src.exists():
                shutil.copy2(src, str(copy) + sidecar)
        return read(copy)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("[cookie_reader] could not read cookies: %s", exc)
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _hosts_in_copy(copy: Path, name_prefixes: tuple[str, ...]) -> set[str]:
    """Unexpired cookie hosts in the private copy matching ``name_prefixes``."""
    now_us = int((time.time() + _EPOCH_OFFSET_S) * 1_000_000)
    conn = sqlite3.connect(str(copy), timeout=5)
    try:
        rows = conn.execute(
            "SELECT host_key, name FROM cookies "
            "WHERE has_expires = 0 OR expires_utc > ?",
            (now_us,),
        ).fetchall()
    finally:
        conn.close()
    return {
        host.lstrip(".").lower()
        for host, name in rows
        if isinstance(name, str) and name.startswith(name_prefixes)
    }


def _read_copy(copy: Path, suffix: str) -> dict[str, str]:
    """Read and decrypt the matching rows of the private copy."""
    now_us = int((time.time() + _EPOCH_OFFSET_S) * 1_000_000)
    conn = sqlite3.connect(str(copy), timeout=5)
    try:
        version = _meta_version(conn)
        rows = conn.execute(_SELECT, (f"%{suffix}", now_us)).fetchall()
    finally:
        conn.close()
    cookies: dict[str, str] = {}
    skipped = 0
    for host, name, value, encrypted in rows:
        if not _host_matches(host, suffix):
            continue
        plain = _row_value(host, value, encrypted, version)
        if plain is None:
            skipped += 1
            continue
        cookies.setdefault(name, plain)
    if skipped:
        logger.info("[cookie_reader] skipped %d undecryptable cookie(s)", skipped)
    return cookies


def _meta_version(conn: sqlite3.Connection) -> int | None:
    """The cookie database schema version, or ``None`` when unreadable."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        return int(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _host_matches(host: str, suffix: str) -> bool:
    """Whether the cookie's ``host_key`` is ``suffix`` or one of its subdomains."""
    bare = host.lstrip(".").lower()
    return bare == suffix or bare.endswith("." + suffix)


def _row_value(
    host: str, value: str, encrypted: bytes, version: int | None,
) -> str | None:
    """The cookie's plaintext, or ``None`` when it cannot be recovered."""
    if not encrypted:
        return str(value)
    if not encrypted.startswith(_V10_PREFIX):
        return None
    plain = _decrypt(encrypted[len(_V10_PREFIX):])
    if plain is None:
        return None
    plain = _strip_host_hash(host, plain, version)
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _decrypt(blob: bytes) -> bytes | None:
    """AES-128-CBC decrypt with PKCS7 unpadding; ``None`` on any failure."""
    if not blob or len(blob) % _BLOCK:
        return None
    from cryptography.hazmat.primitives.ciphers import (
        Cipher,
        algorithms,
        modes,
    )

    decryptor = Cipher(algorithms.AES(_KEY), modes.CBC(_IV)).decryptor()
    padded = decryptor.update(blob) + decryptor.finalize()
    pad = padded[-1]
    if not 1 <= pad <= _BLOCK or padded[-pad:] != bytes([pad]) * pad:
        return None
    return padded[:-pad]


def _strip_host_hash(host: str, plain: bytes, version: int | None) -> bytes:
    """Drop the SHA-256 host prefix newer Chromium versions put in front.

    With a known ``meta.version`` the prefix is stripped from version 24 on.
    Without one it is stripped only when it really is this host's hash.
    """
    if len(plain) < _HOST_HASH_LEN:
        return plain
    if version is None:
        known = hashlib.sha256(host.encode()).digest()
        return plain[_HOST_HASH_LEN:] if plain[:_HOST_HASH_LEN] == known else plain
    return plain[_HOST_HASH_LEN:] if version >= _HOST_HASH_VERSION else plain
