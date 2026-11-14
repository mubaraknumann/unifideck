"""Ubisoft Connect 175 encrypted caches and session layout.

UPC 175.1.13416 (Oct 2026) replaced its plain library caches with encrypted
``*.vdat`` files and its account file ``user.dat`` with ``user.vdat`` plus a
``store/`` key dir. The library sync still read the plain
``configurations``, which UPC deletes on migration, so every fresh sign-in
synced zero games with no error. These tests build real encrypted files the
way Wine's ``CryptProtectData`` and UPC write them, and check that we read
them, prefer them over stale plain copies, and carry the new session files
between prefixes as one unit.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from unifideck.stores.ubisoft import parser
from unifideck.stores.ubisoft.config import UbisoftConfig
from unifideck.stores.ubisoft.library.data_loader import _DataLoader
from unifideck.stores.ubisoft.library.facade import UbisoftLibrary
from unifideck.stores.ubisoft.paths import UbisoftPrefixPaths
from unifideck.stores.ubisoft.session import UbisoftSession
from unifideck.stores.ubisoft.store import UbisoftStore
from unifideck.stores.ubisoft.vdat import (
    VdatError,
    decrypt_vdat,
    prefer_vdat,
    wine_unprotect,
)

KEY_ID = "0123456789abcdef0123456789abcdef"
UPC_LOCAL = Path("drive_c/users/steamuser/AppData/Local/Ubisoft Game Launcher")
MACHINE_GUID = "11111111-2222-3333-4444-555555555555"
MAGIC = b"Wine Crypt32 ok\0"


# ── writers that mirror Wine and UPC ────────────────────────────────


def _sha1(data: bytes) -> bytes:
    return hashlib.sha1(data, usedforsecurity=False).digest()


def _wine_protect(plain: bytes, user: bytes = b"steamuser\0") -> bytes:
    """What Wine's ``CryptProtectData`` writes (dlls/crypt32/protectdata.c)."""
    salt = os.urandom(16)
    digest = _sha1(user + b"I'm hunting wabbits" + salt).ljust(64, b"\0")
    key = (_sha1(bytes(0x36 ^ b for b in digest))
           + _sha1(bytes(0x5C ^ b for b in digest)))[:24]
    pad = 8 - len(plain) % 8
    enc = Cipher(TripleDES(key), modes.CBC(b"\0" * 8)).encryptor()
    cipher = enc.update(plain + bytes([pad]) * pad) + enc.finalize()

    def sized(b: bytes) -> bytes:
        return struct.pack("<I", len(b)) + b

    return b"".join([
        struct.pack("<I", 1), MAGIC, struct.pack("<I", 1), MAGIC,
        struct.pack("<I", 0), sized(b"\0\0"),
        struct.pack("<II", 0x6603, 168), sized(MAGIC), struct.pack("<I", 0),
        struct.pack("<II", 0x8004, 160), sized(salt),
        sized(cipher), sized(_sha1(plain)),
    ])


def _pb(field: int, payload: bytes | int) -> bytes:
    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | (0x80 if n else 0))
            if not n:
                return bytes(out)
    if isinstance(payload, int):
        return varint(field << 3) + varint(payload)
    return varint(field << 3 | 2) + varint(len(payload)) + payload


def _write_vdat(local: Path, rel: str, plain: bytes, key: bytes) -> Path:
    nonce = os.urandom(12)
    sealed = AESGCM(key).encrypt(nonce, plain, None)
    ct, tag = sealed[:-16], sealed[-16:]
    key_ref = _pb(1, 1) + _pb(2, KEY_ID.encode()) + _pb(3, nonce) + _pb(4, tag)
    header = _pb(1, 1) + _pb(2, key_ref) + _pb(3, b"")
    path = local / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pb(1, _pb(1, header) + _pb(2, ct)))
    return path


def _write_store_key(local: Path, key: bytes) -> None:
    (local / "store").mkdir(parents=True, exist_ok=True)
    (local / "store" / f"{KEY_ID}.bin").write_bytes(_wine_protect(key))


def _ownership_records(*pairs: tuple[int, int]) -> bytes:
    """Ownership records as UPC frames them (small ids, one-byte sizes)."""
    out = b""
    for a, b in pairs:
        body = bytes([0x08, a, 0x10, b, 0x22, 0x02]) + b"xx"
        out += bytes([0x0A, len(body)]) + body
    return out


# ── decryption ──────────────────────────────────────────────────────


def test_wine_dpapi_round_trip():
    key = os.urandom(32)
    assert wine_unprotect(_wine_protect(key)) == key


def test_wine_dpapi_rejects_another_user():
    with pytest.raises(VdatError):
        wine_unprotect(_wine_protect(os.urandom(32), user=b"deck\0"))


def test_decrypts_a_vdat_with_its_store_key(tmp_path: Path):
    key = os.urandom(32)
    _write_store_key(tmp_path, key)
    vdat = _write_vdat(tmp_path, "cache/configuration/configurations.vdat",
                       b"hello upc", key)
    assert decrypt_vdat(vdat) == b"hello upc"


def test_a_key_from_another_prefix_fails_loudly(tmp_path: Path):
    """UPC reuses the key id across prefixes with different keys."""
    _write_store_key(tmp_path, os.urandom(32))
    vdat = _write_vdat(tmp_path, "cache/ownership/u.vdat", b"x", os.urandom(32))
    with pytest.raises(VdatError, match="failed authentication"):
        decrypt_vdat(vdat)


def test_missing_store_dir_fails_loudly(tmp_path: Path):
    vdat = _write_vdat(tmp_path, "cache/ownership/u.vdat", b"x", os.urandom(32))
    with pytest.raises(VdatError, match="no store/ dir"):
        decrypt_vdat(vdat)


def test_garbage_is_a_vdat_error_not_a_crash(tmp_path: Path):
    bad = tmp_path / "cache/ownership/u.vdat"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"\x0a\xff\xff\xff")
    with pytest.raises(VdatError):
        decrypt_vdat(bad)


# ── parsers and file choice ─────────────────────────────────────────


def test_ownership_vdat_records_start_at_zero(tmp_path: Path):
    records = _ownership_records((42, 43), (50, 50))
    legacy = tmp_path / "legacy" / "uid"
    legacy.parent.mkdir()
    legacy.write_bytes(b"\0" * 0x108 + records)
    key = os.urandom(32)
    _write_store_key(tmp_path, key)
    vdat = _write_vdat(tmp_path, "cache/ownership/uid.vdat", records, key)
    assert parser.parse_ownership(str(legacy)) == [42, 43, 50]
    assert parser.parse_ownership(str(vdat)) == [42, 43, 50]


def test_undecryptable_cache_parses_as_unreadable_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
):
    vdat = _write_vdat(tmp_path, "cache/configuration/configurations.vdat",
                       b"x", os.urandom(32))
    assert parser.parse_configurations(str(vdat)) == []
    assert "cannot decrypt" in caplog.text


def test_prefer_vdat_over_the_frozen_plain_file(tmp_path: Path):
    plain = tmp_path / "configurations"
    plain.write_bytes(b"old")
    assert prefer_vdat(plain) == plain
    (tmp_path / "configurations.vdat").write_bytes(b"new")
    assert prefer_vdat(plain) == tmp_path / "configurations.vdat"
    plain.unlink()  # UPC 175 deletes the plain configurations on migration
    assert prefer_vdat(plain) == tmp_path / "configurations.vdat"
    (tmp_path / "configurations.vdat").unlink()
    assert prefer_vdat(plain) is None


def test_find_configurations_finds_the_vdat_only_layout(tmp_path: Path):
    """A fresh 175 sign-in has no plain file at all: the 0-games bug."""
    config = UbisoftConfig.from_config_manager(None)
    cfg = tmp_path / "pfx" / config.configurations_relative_path
    cfg = cfg.with_name(cfg.name + ".vdat")
    cfg.parent.mkdir(parents=True)
    cfg.write_bytes(b"x")
    assert UbisoftPrefixPaths(config).find_configurations(str(tmp_path)) == str(cfg)


def test_ownership_entry_prefers_vdat_and_reports_the_bare_uid(tmp_path: Path):
    (tmp_path / "c6f1e6fa-uid").write_bytes(b"old")
    (tmp_path / "c6f1e6fa-uid.vdat").write_bytes(b"new")
    path, uid = _DataLoader._first_ownership_entry(tmp_path)
    assert Path(path).name == "c6f1e6fa-uid.vdat"
    assert uid == "c6f1e6fa-uid"


# ── unreadable is not empty ─────────────────────────────────────────


def _library_with(fetch_result: object) -> UbisoftLibrary:
    lib = object.__new__(UbisoftLibrary)
    lib._config = UbisoftConfig.from_config_manager(None)
    lib._detector = SimpleNamespace(get_installed=AsyncMock(return_value={}))
    if isinstance(fetch_result, Exception):
        fetch = AsyncMock(side_effect=fetch_result)
    else:
        fetch = AsyncMock(return_value=fetch_result)
    lib._fetcher = SimpleNamespace(fetch_local_binaries=fetch)
    return lib


@pytest.mark.parametrize("fetch_result", [None, RuntimeError("parser blew up")])
def test_unreadable_cache_is_none_so_sync_keeps_shortcuts(fetch_result: object):
    """``[]`` is authoritative downstream and sweeps every Ubisoft shortcut."""
    assert asyncio.run(_library_with(fetch_result).get_library()) is None


def test_unreadable_library_toasts_once_per_streak():
    emitted: list[dict] = []

    async def _emit(_event: object, **kwargs: object) -> None:
        emitted.append(kwargs)

    store = SimpleNamespace(_emit=_emit)
    report = UbisoftStore._report_unreadable_library
    for unreadable in (True, True, False, True):
        asyncio.run(report(store, unreadable))  # type: ignore[arg-type]
    assert len(emitted) == 2
    assert emitted[0]["i18n_key"] == "toasts.ubisoftLibraryUnreadableMessage"
    assert emitted[0]["i18n_title_key"] == "toasts.header.ubisoft"


# ── session payload ─────────────────────────────────────────────────


def _make_prefix(prefix: Path) -> Path:
    local = prefix / UPC_LOCAL
    local.mkdir(parents=True)
    (prefix / "system.reg").write_text(
        f'[Software\\\\Microsoft\\\\Cryptography] 1\n"MachineGuid"="{MACHINE_GUID}"\n',
    )
    return local


def _sign_in_175(local: Path, token: str) -> None:
    """A UPC 175 signed-in session: vault + user.vdat + store/ + caches."""
    (local / "ConnectSecureStorage.dat").write_bytes(
        f"{token}\nRememberMeTicket\n".encode() + b"x" * 7000,
    )
    (local / "user.vdat").write_bytes(f"account {token}".encode())
    key = os.urandom(32)
    _write_store_key(local, key)
    _write_vdat(local, "cache/ownership/uid.vdat", _ownership_records((7, 7)), key)
    time.sleep(0.01)  # mtime ordering drives the newer-session guard


@pytest.fixture
def session(tmp_path: Path) -> UbisoftSession:
    config = dataclasses.replace(
        UbisoftConfig.from_config_manager(None),
        data_dir=str(tmp_path / "data"),
        prefixes_dir=str(tmp_path / "prefixes"),
        upc_session_file=str(tmp_path / "data/upc_session"),
    )
    return UbisoftSession(config, UbisoftPrefixPaths(config), lambda _p: MACHINE_GUID)


def test_injection_carries_user_vdat_and_store_and_drops_stale_user_dat(
    session: UbisoftSession, tmp_path: Path,
):
    auth = _make_prefix(Path(session._config.auth_prefix_dir_expanded))
    game_prefix = tmp_path / "prefixes" / "rayman"
    game = _make_prefix(game_prefix)
    (game / "user.dat").write_bytes(b"template account")  # from the clone
    _sign_in_175(auth, "token-1")

    session.inject_into_prefix(str(game_prefix))

    assert (game / "user.vdat").read_bytes() == b"account token-1"
    assert (game / "store" / f"{KEY_ID}.bin").is_file()
    assert not (game / "user.dat").exists()
    # The injected caches are readable with the injected key.
    assert parser.parse_ownership(str(game / "cache/ownership/uid.vdat")) == [7]


def test_a_newer_175_session_in_the_target_is_kept_whole(
    session: UbisoftSession, tmp_path: Path,
):
    """Caches follow the session: never pair them with another session's key."""
    auth = _make_prefix(Path(session._config.auth_prefix_dir_expanded))
    game_prefix = tmp_path / "prefixes" / "rayman"
    game = _make_prefix(game_prefix)
    _sign_in_175(auth, "token-1")
    _sign_in_175(game, "token-2")  # the game prefix signed in later
    before = (game / "cache/ownership/uid.vdat").read_bytes()

    session.inject_into_prefix(str(game_prefix))

    assert (game / "user.vdat").read_bytes() == b"account token-2"
    assert (game / "cache/ownership/uid.vdat").read_bytes() == before
    assert parser.parse_ownership(str(game / "cache/ownership/uid.vdat")) == [7]
