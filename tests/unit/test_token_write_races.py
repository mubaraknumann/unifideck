"""Regression: concurrent GOG token refreshes corrupted the token file.

The GOG update scan checks installed games six at a time. Each check found
the token stale and refreshed it, and every save went through the same
``gog_token.json.tmp`` opened with ``O_TRUNC``. The writes interleaved,
three renames failed, and the surviving file was a torn blob AES-GCM
rejected: the Deck was silently signed out of GOG.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

from unifideck.security.token_file import EncryptedTokenFile
from unifideck.stores.gog.tokens.manager import GOGTokenManager


def test_concurrent_token_writes_leave_one_whole_file(tmp_path):
    writer = EncryptedTokenFile(store="gog", secure_store=MagicMock(), bus=None, log_prefix="[test]")
    target = tmp_path / "gog_token.json"
    blobs = [bytes([i]) * (64 * 1024) for i in range(16)]
    start = threading.Barrier(len(blobs))

    def write(blob: bytes) -> None:
        start.wait()
        assert writer._write_atomic(str(target), blob)

    threads = [threading.Thread(target=write, args=(b,)) for b in blobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert target.read_bytes() in blobs
    assert [p.name for p in tmp_path.iterdir()] == ["gog_token.json"]
    assert target.stat().st_mode & 0o777 == 0o600


async def test_concurrent_stale_checks_refresh_once():
    manager = GOGTokenManager.__new__(GOGTokenManager)
    manager._refresh_lock = asyncio.Lock()
    manager._access_token = "old"
    manager._refresh_token = "refresh"
    state = {"fresh": False, "calls": 0, "in_flight": 0, "max_in_flight": 0}
    manager.get_token_age_seconds = lambda: 1.0 if state["fresh"] else 99999.0  # type: ignore[method-assign]

    async def refresh(*, access_token, refresh_token, age_seconds):
        if age_seconds < 2400:
            return True
        state["calls"] += 1
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        await asyncio.sleep(0.01)
        state["fresh"] = True
        state["in_flight"] -= 1
        return True

    manager._oauth = MagicMock(refresh_if_stale=refresh)
    results = await asyncio.gather(*(manager.refresh_if_stale() for _ in range(6)))

    assert all(results)
    assert state["calls"] == 1
    assert state["max_in_flight"] == 1


def test_temp_files_live_next_to_the_target(tmp_path):
    # Same directory keeps the final rename atomic (no cross-device move).
    writer = EncryptedTokenFile(store="gog", secure_store=MagicMock(), bus=None, log_prefix="[test]")
    target = tmp_path / "nested" / "gog_token.json"
    assert writer._write_atomic(str(target), b"x")
    assert Path(target).read_bytes() == b"x"
