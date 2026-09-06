"""The one encrypted-token-file primitive GOG and Microsoft now share.

Audit §1.4 c / register item 9. Both stores carried their own copy of read /
decrypt / legacy-detect / atomic-0600-write / permission-audit. This pins the
extracted version's contract, including the two defects that were found in
the copies and must not come back:

* Microsoft's ``_write_atomic_0600`` closed the fd twice on a failed *write*,
  so ``EBADF`` replaced the real error and a disk-full token save was
  reported as "Bad file descriptor".
* The plaintext-fallback refusal: if encryption is unavailable we write
  nothing. Losing the session beats leaking a refresh token.
"""
from __future__ import annotations

import base64
import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from unifideck.security.secure_token_store import SecureTokenStoreError
from unifideck.security.token_file import EncryptedTokenFile


class _Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event: object, **payload: object) -> None:
        self.events.append((getattr(event, "name", str(event)), dict(payload)))


class _FakeStore:
    """Stand-in for SecureTokenStore: magic prefix + base64 of the JSON.

    Base64 rather than plain JSON so that "the bytes on disk do not contain
    the secret" is a claim the fake can actually support — with a
    passthrough encoding that assertion would be vacuous.
    """

    MAGIC = b"UFD1FAKE:"

    def __init__(self, *, fail_encrypt: bool = False,
                 fail_decrypt: bool = False) -> None:
        self.fail_encrypt = fail_encrypt
        self.fail_decrypt = fail_decrypt

    def is_encrypted(self, blob: bytes) -> bool:
        return blob.startswith(self.MAGIC)

    def encrypt_payload(self, payload: dict[str, Any]) -> bytes:
        if self.fail_encrypt:
            raise SecureTokenStoreError("no key")
        return self.MAGIC + base64.b64encode(json.dumps(payload).encode())

    def decrypt_payload(self, blob: bytes) -> dict[str, Any]:
        if self.fail_decrypt:
            raise SecureTokenStoreError("bad key")
        return json.loads(base64.b64decode(blob[len(self.MAGIC):]).decode())


def _mk(store: _FakeStore | None = None,
        bus: _Bus | None = None) -> EncryptedTokenFile:
    return EncryptedTokenFile(
        store="gog",
        secure_store=store or _FakeStore(),  # type: ignore[arg-type]
        bus=bus,
        log_prefix="[Test]",
    )


# ── round trip ───────────────────────────────────────────────────

async def test_write_then_read_round_trips(tmp_path: Path) -> None:
    f = _mk()
    path = str(tmp_path / "tok.json")
    payload = {"access_token": "a", "refresh_token": "r", "user_id": "7"}
    assert await f.write(path, payload) is True
    assert await f.read(path) == payload


async def test_written_file_is_encrypted_not_plaintext(tmp_path: Path) -> None:
    f = _mk()
    path = str(tmp_path / "tok.json")
    await f.write(path, {"refresh_token": "super-secret"})
    raw = Path(path).read_bytes()
    assert b"super-secret" not in raw
    assert raw.startswith(_FakeStore.MAGIC)


async def test_written_file_lands_at_0600(tmp_path: Path) -> None:
    f = _mk()
    path = str(tmp_path / "tok.json")
    await f.write(path, {"refresh_token": "r"})
    assert Path(path).stat().st_mode & 0o777 == 0o600


async def test_write_creates_missing_parent_directories(tmp_path: Path) -> None:
    f = _mk()
    path = str(tmp_path / "deep" / "nested" / "tok.json")
    assert await f.write(path, {"refresh_token": "r"}) is True
    assert Path(path).is_file()


async def test_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    f = _mk()
    path = str(tmp_path / "tok.json")
    await f.write(path, {"refresh_token": "r"})
    assert not (tmp_path / "tok.json.tmp").exists()


# ── the plaintext-fallback refusal ───────────────────────────────

async def test_encryption_failure_writes_nothing(tmp_path: Path) -> None:
    f = _mk(_FakeStore(fail_encrypt=True))
    path = str(tmp_path / "tok.json")
    assert await f.write(path, {"refresh_token": "r"}) is False
    assert not Path(path).exists()


async def test_encryption_failure_does_not_clobber_an_existing_file(
    tmp_path: Path,
) -> None:
    """A failed re-save must leave the previous good token in place."""
    path = str(tmp_path / "tok.json")
    assert await _mk().write(path, {"refresh_token": "original"}) is True
    before = Path(path).read_bytes()

    broken = _mk(_FakeStore(fail_encrypt=True))
    assert await broken.write(path, {"refresh_token": "new"}) is False
    assert Path(path).read_bytes() == before


# ── legacy plaintext migration ───────────────────────────────────

async def test_reads_a_legacy_plaintext_file(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    path.write_text(json.dumps({"refresh_token": "legacy"}))
    assert await _mk().read(str(path)) == {"refresh_token": "legacy"}


async def test_legacy_plaintext_read_emits_an_audit_event(
    tmp_path: Path,
) -> None:
    import asyncio

    path = tmp_path / "tok.json"
    path.write_text(json.dumps({"refresh_token": "legacy"}))
    bus = _Bus()
    await _mk(bus=bus).read(str(path))
    await asyncio.sleep(0)
    assert len(bus.events) == 1
    assert bus.events[0][1]["store"] == "gog"


async def test_legacy_file_is_re_encrypted_on_the_next_write(
    tmp_path: Path,
) -> None:
    """The migration contract: read plaintext, write back ciphertext."""
    f = _mk()
    path = str(tmp_path / "tok.json")
    Path(path).write_text(json.dumps({"refresh_token": "legacy"}))

    data = await f.read(path)
    assert data is not None
    assert await f.write(path, data) is True
    assert Path(path).read_bytes().startswith(_FakeStore.MAGIC)
    assert await f.read(path) == {"refresh_token": "legacy"}


# ── failure modes all collapse to None ───────────────────────────

async def test_missing_file_reads_as_none(tmp_path: Path) -> None:
    assert await _mk().read(str(tmp_path / "nope.json")) is None


async def test_undecryptable_blob_reads_as_none(tmp_path: Path) -> None:
    path = str(tmp_path / "tok.json")
    await _mk().write(path, {"refresh_token": "r"})
    f = _mk(_FakeStore(fail_decrypt=True))
    assert await f.read(path) is None


async def test_unparseable_plaintext_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    path.write_text("{not json at all")
    assert await _mk().read(str(path)) is None


async def test_non_utf8_plaintext_reads_as_none(tmp_path: Path) -> None:
    path = tmp_path / "tok.json"
    path.write_bytes(b"\xff\xfe\x00garbage")
    assert await _mk().read(str(path)) is None


# ── the EBADF regression ─────────────────────────────────────────

async def test_failed_write_reports_the_real_errno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Microsoft's copy called ``os.close(fd)`` from its except arm after
    ``os.fdopen`` had already taken ownership, so a write failure surfaced
    as EBADF and the true cause (ENOSPC) was lost. The write must fail
    cleanly and log the original error instead."""
    real_fdopen = os.fdopen
    seen: list[BaseException] = []

    class _Boom:
        def __init__(self, f: Any) -> None:
            self._f = f

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> bool:
            self._f.close()
            return False

        def write(self, _data: bytes) -> int:
            raise OSError(errno.ENOSPC, "No space left on device")

    def fake_fdopen(fd: int, mode: str) -> Any:
        return _Boom(real_fdopen(fd, mode))

    monkeypatch.setattr(os, "fdopen", fake_fdopen)

    import logging
    logger = logging.getLogger("unifideck.security.token_file")

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())  # type: ignore[arg-type]

    handler = _Capture()
    logger.addHandler(handler)
    try:
        ok = await _mk().write(str(tmp_path / "tok.json"), {"a": "b"})
    finally:
        logger.removeHandler(handler)

    assert ok is False
    joined = " ".join(str(s) for s in seen)
    assert "No space left on device" in joined
    assert "Bad file descriptor" not in joined


# ── remove ───────────────────────────────────────────────────────

async def test_remove_deletes_every_named_path(tmp_path: Path) -> None:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text("{}")
    b.write_text("{}")
    await _mk().remove(str(a), str(b))
    assert not a.exists()
    assert not b.exists()


async def test_remove_tolerates_absent_paths(tmp_path: Path) -> None:
    """Sign-out runs this against paths that may never have existed."""
    await _mk().remove(str(tmp_path / "never.json"))
