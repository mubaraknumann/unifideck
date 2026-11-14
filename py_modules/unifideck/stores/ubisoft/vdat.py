"""
Read Ubisoft Connect's encrypted ``*.vdat`` cache files.

Ubisoft Connect 175 (build 13416, Oct 2026) stopped writing its library
caches in the clear. ``cache/configuration/configurations`` became
``configurations.vdat``, ``cache/ownership/<uid>`` became ``<uid>.vdat``,
and the account file ``user.dat`` became ``user.vdat``. On migration UPC
deletes the plain ``configurations`` but can leave the old ownership file
behind, frozen. The library parser then saw "no configurations" and the
sync returned zero games without an error.

Layout of a ``.vdat`` file (protobuf wire format)::

    1: envelope
       1: header
          1: 1
          2: key    {1: 1, 2: key_id (32 hex chars), 3: nonce (12), 4: tag (16)}
          3: ""
       2: ciphertext

The payload is AES-256-GCM with no associated data. The 32-byte key sits
in ``<UPC local dir>/store/<key_id>.bin``, wrapped by ``CryptProtectData``.
Under Proton that is **Wine's** implementation, which is not tied to the
machine: the 3DES key is derived from the Windows user name, a fixed
string compiled into Wine, and the blob's salt (see
``dlls/crypt32/protectdata.c``). So the cache can be read on Linux.

The decrypted bytes are the same record formats the plain files used,
except that the ownership records start at offset 0 (the plain file had
a 0x108-byte header).
"""

from __future__ import annotations

import hashlib
import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

VDAT_SUFFIX = ".vdat"
#: Windows user name inside every Proton prefix. Wine hashes it with its
#: terminating NUL (``GetUserNameA`` reports the length including it).
_WINE_USER = b"steamuser\0"
#: ``crypt32_protectdata_secret`` from Wine's protectdata.c, without NUL.
_WINE_SECRET = b"I'm hunting wabbits"
_CALG_3DES = 0x6603
_CALG_SHA1 = 0x8004
_STORE_DIR = "store"
_KEY_ID_LEN = 32
_NONCE_LEN = 12
_TAG_LEN = 16


class VdatError(Exception):
    """A ``.vdat`` file or its key could not be read or decrypted."""


# ── protobuf ────────────────────────────────────────────────────────


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        if pos >= len(buf):
            raise VdatError("truncated varint")
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise VdatError("varint too long")


def _fields(buf: bytes) -> dict[int, bytes | int]:
    """First occurrence of each top-level field in a protobuf message."""
    out: dict[int, bytes | int] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        field, wire_type = key >> 3, key & 7
        value: bytes | int
        if wire_type == 0:
            value, pos = _varint(buf, pos)
        elif wire_type == 2:
            length, pos = _varint(buf, pos)
            if pos + length > len(buf):
                raise VdatError("truncated field")
            value = buf[pos:pos + length]
            pos += length
        else:
            raise VdatError(f"unexpected wire type {wire_type}")
        out.setdefault(field, value)
    return out


def _bytes_field(msg: dict[int, bytes | int], field: int, what: str) -> bytes:
    value = msg.get(field)
    if not isinstance(value, bytes):
        raise VdatError(f"missing {what}")
    return value


# ── Wine CryptUnprotectData ─────────────────────────────────────────


def _sha1(data: bytes) -> bytes:
    """SHA1, because Wine's DPAPI format uses it; we only read it."""
    return hashlib.sha1(data, usedforsecurity=False).digest()



def _parse_wine_blob(blob: bytes) -> tuple[bytes, bytes, bytes]:
    """Return ``(salt, cipher, fingerprint)`` from a Wine DPAPI blob."""
    pos = 0

    def dword() -> int:
        nonlocal pos
        if pos + 4 > len(blob):
            raise VdatError("truncated DPAPI blob")
        (value,) = struct.unpack_from("<I", blob, pos)
        pos += 4
        return int(value)

    def sized() -> bytes:
        nonlocal pos
        length = dword()
        if pos + length > len(blob):
            raise VdatError("truncated DPAPI blob")
        data = blob[pos:pos + length]
        pos += length
        return data

    dword()
    pos += 16  # count0, info0 (magic)
    dword()
    pos += 16  # count1, info1
    dword()  # null0
    sized()  # description (UTF-16)
    cipher_alg = dword()
    dword()  # cipher key length
    sized()  # data0 (magic)
    dword()  # null1
    hash_alg = dword()
    dword()  # hash length
    salt = sized()
    cipher = sized()
    fingerprint = sized()
    if cipher_alg != _CALG_3DES or hash_alg != _CALG_SHA1:
        raise VdatError(
            f"not a Wine DPAPI blob (cipher {cipher_alg:#x}, hash {hash_alg:#x})",
        )
    return salt, cipher, fingerprint


def _derive_3des_key(digest: bytes) -> bytes:
    """``CryptDeriveKey`` for a 24-byte key from a 20-byte SHA1 digest."""
    block = digest.ljust(64, b"\0")
    inner = _sha1(bytes(0x36 ^ b for b in block))
    outer = _sha1(bytes(0x5C ^ b for b in block))
    return (inner + outer)[:24]


def wine_unprotect(blob: bytes) -> bytes:
    """Decrypt a blob written by Wine's ``CryptProtectData`` (no entropy)."""
    from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes

    salt, cipher, fingerprint = _parse_wine_blob(blob)
    if not cipher or len(cipher) % 8:
        raise VdatError("bad DPAPI ciphertext length")
    digest = _sha1(_WINE_USER + _WINE_SECRET + salt)
    decryptor = Cipher(
        TripleDES(_derive_3des_key(digest)), modes.CBC(b"\0" * 8),
    ).decryptor()
    padded = decryptor.update(cipher) + decryptor.finalize()
    pad = padded[-1]
    plain = padded[:-pad] if 1 <= pad <= 8 else b""
    if not plain or _sha1(plain) != fingerprint:
        raise VdatError("DPAPI key did not verify")
    return plain


# ── .vdat ───────────────────────────────────────────────────────────


def _store_dir_for(vdat_path: Path) -> Path | None:
    """The ``store/`` dir that holds this file's key.

    It sits in the UPC local dir: next to ``user.vdat`` and two levels
    above ``cache/<kind>/<file>.vdat``.
    """
    for parent in list(vdat_path.parents)[:3]:
        candidate = parent / _STORE_DIR
        if candidate.is_dir():
            return candidate
    return None


def decrypt_vdat(path: str | Path) -> bytes:
    """Return the plaintext of a UPC ``.vdat`` file.

    Raises :class:`VdatError` with the reason when the file, its key, or
    the decryption is bad. The caller decides how loudly to report it.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    vdat_path = Path(path)
    try:
        raw = vdat_path.read_bytes()
    except OSError as e:
        raise VdatError(f"cannot read {vdat_path}: {e}") from e
    envelope = _fields(_bytes_field(_fields(raw), 1, "envelope"))
    header = _fields(_bytes_field(envelope, 1, "header"))
    key_ref = _fields(_bytes_field(header, 2, "key reference"))
    key_id = _bytes_field(key_ref, 2, "key id").decode("ascii", "replace")
    nonce = _bytes_field(key_ref, 3, "nonce")
    tag = _bytes_field(key_ref, 4, "tag")
    ciphertext = _bytes_field(envelope, 2, "ciphertext")
    if len(key_id) != _KEY_ID_LEN or not key_id.isalnum():
        raise VdatError(f"bad key id {key_id!r}")
    if len(nonce) != _NONCE_LEN or len(tag) != _TAG_LEN:
        raise VdatError("bad nonce/tag length")
    store = _store_dir_for(vdat_path)
    if store is None:
        raise VdatError(f"no {_STORE_DIR}/ dir near {vdat_path}")
    key_file = store / f"{key_id}.bin"
    try:
        key = wine_unprotect(key_file.read_bytes())
    except OSError as e:
        raise VdatError(f"cannot read key {key_file}: {e}") from e
    if len(key) != 32:
        raise VdatError(f"unexpected key length {len(key)}")
    try:
        return AESGCM(key).decrypt(nonce, ciphertext + tag, None)
    except InvalidTag as e:
        raise VdatError(f"{vdat_path.name} failed authentication") from e


def prefer_vdat(path: Path) -> Path | None:
    """``<path>.vdat`` if it exists, else ``path`` if it exists, else None.

    A prefix that ran UPC 175 may still hold the pre-175 plain file next
    to the ``.vdat``; the plain one is frozen at the migration, so the
    encrypted one always wins.
    """
    encrypted = path.with_name(path.name + VDAT_SUFFIX)
    if encrypted.is_file():
        return encrypted
    if path.is_file():
        return path
    return None
