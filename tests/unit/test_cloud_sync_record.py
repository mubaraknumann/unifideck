"""The cloud-save sync record and verdict (``services/cloud_save/sync_record.py``).

The status line used to compare an upload time with a save time, or local saves
with the plugin's own backup mirror, so it read "out of sync" after every play
session. These pin the replacement: a fingerprint recorded at each converged
sync, and a three-way verdict against it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from unifideck.services.cloud_save import sync_record as sr
from unifideck.services.cloud_save.constants import MANIFEST_FILE
from unifideck.services.cloud_save.ea_strategy import cloud_verdict
from unifideck.services.cloud_save.strategy_base import CloudSaveStrategy


def _write(path: Path, data: bytes = b"x", mtime: float = 1_700_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


# ── fingerprint ──────────────────────────────────────────────────────

def test_fingerprint_changes_with_a_save_and_ignores_our_manifest(tmp_path: Path) -> None:
    _write(tmp_path / "slot1.sav")
    first = sr.fingerprint(tmp_path)
    _write(tmp_path / MANIFEST_FILE, b"{}")
    assert sr.fingerprint(tmp_path) == first
    _write(tmp_path / "slot1.sav", b"xy")
    assert sr.fingerprint(tmp_path) != first


def test_fingerprint_of_a_missing_folder_is_none(tmp_path: Path) -> None:
    assert sr.fingerprint(tmp_path / "nope") is None


# ── verdict ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("local", "cloud_ts", "expected"),
    [
        ("L", 100.0, sr.IN_SYNC),
        ("L", 101.0, sr.IN_SYNC),         # inside the tolerance
        ("L2", 100.0, sr.LOCAL_NEWER),
        ("L", 500.0, sr.CLOUD_NEWER),
        ("L2", 500.0, sr.CONFLICT),
        ("L2", None, sr.LOCAL_NEWER),     # store cannot report the cloud
        ("L", None, sr.IN_SYNC),
    ],
)
def test_three_way_verdict(local: str, cloud_ts: float | None, expected: str) -> None:
    assert sr.verdict({"local": "L", "cloud_ts": 100.0}, local, cloud_ts) == expected


def test_no_record_means_no_verdict() -> None:
    assert sr.verdict(None, "L", 100.0) is None


def test_records_round_trip(tmp_path: Path) -> None:
    records = sr.SyncRecords(tmp_path / sr.RECORDS_FILE)
    assert records.get("epic:g") is None
    records.put("epic:g", "L", 12.0)
    records.put("gog:h", "M", None)
    assert records.get("epic:g") == {"local": "L", "cloud_ts": 12.0}
    assert records.get("gog:h") == {"local": "M", "cloud_ts": None}


# ── the base strategy records converged syncs ────────────────────────

class _Strategy(CloudSaveStrategy):
    """A store whose pull and push do what each test says."""

    store_id = "fake"

    def __init__(self, root: Path, save_dir: Path) -> None:
        super().__init__(str(root / "saves"))
        self.save_dir = save_dir
        self.cloud_ts = 1000.0
        self.pull_writes: bytes | None = None

    def _resolve_store_save_dir(self, game_id: str) -> str | None:
        return str(self.save_dir)

    async def _do_sync_down(self, game_id: str, local_dir: str, force: bool) -> bool:
        if self.pull_writes is not None:
            _write(Path(local_dir) / "slot1.sav", self.pull_writes, mtime=1_700_000_500)
        return True

    async def _do_sync_up(self, game_id: str, local_dir: str, *, force: bool = False) -> bool:
        return True

    async def _fetch_cloud_info(self, game_id: str) -> dict[str, Any] | None:
        return {"has_saves": True, "timestamp": self.cloud_ts}


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Strategy:
    from unifideck.services.cloud_save import safety

    # Backups are not under test here; keep them out of the real home.
    monkeypatch.setattr(safety, "snapshot_backup", lambda *a, **k: None)
    save_dir = tmp_path / "save"
    _write(save_dir / "slot1.sav")
    return _Strategy(tmp_path, save_dir)


async def test_push_then_status_is_in_sync(strategy: _Strategy) -> None:
    assert await strategy.sync_up("g") is True
    info = await strategy.get_cloud_save_info("g")
    assert await strategy.sync_verdict("g", info) == sr.IN_SYNC


async def test_playing_after_a_push_is_local_newer(strategy: _Strategy) -> None:
    await strategy.sync_up("g")
    _write(strategy.save_dir / "slot1.sav", b"progress", mtime=1_700_000_900)
    info = await strategy.get_cloud_save_info("g")
    assert await strategy.sync_verdict("g", info) == sr.LOCAL_NEWER


async def test_another_device_pushing_is_cloud_newer(strategy: _Strategy) -> None:
    await strategy.sync_up("g")
    strategy.cloud_ts = 9000.0
    assert await strategy.sync_verdict("g", {"timestamp": 9000.0}) == sr.CLOUD_NEWER


async def test_pull_that_downloaded_is_recorded(strategy: _Strategy) -> None:
    strategy.pull_writes = b"from the cloud"
    assert await strategy.sync_down("g") is True
    assert await strategy.sync_verdict("g", {"timestamp": 1000.0}) == sr.IN_SYNC


async def test_pull_that_skipped_a_newer_local_save_is_not_recorded(
    strategy: _Strategy,
) -> None:
    """The store skipped the download because local is newer: never "in sync"."""
    await strategy.sync_up("g")
    _write(strategy.save_dir / "slot1.sav", b"progress", mtime=1_700_000_900)
    assert await strategy.sync_down("g") is True  # pulled nothing
    assert await strategy.sync_verdict("g", {"timestamp": 1000.0}) == sr.LOCAL_NEWER


async def test_first_pull_with_nothing_downloaded_records_nothing(
    strategy: _Strategy,
) -> None:
    await strategy.sync_down("g")
    assert await strategy.sync_verdict("g", {"timestamp": 1000.0}) is None


async def test_a_stores_own_verdict_wins(strategy: _Strategy) -> None:
    assert await strategy.sync_verdict("g", {"verdict": sr.CONFLICT}) == sr.CONFLICT


# ── EA: file-by-file counts → one verdict ────────────────────────────

@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"unchanged": 2}, sr.IN_SYNC),
        ({"local_only": 2}, sr.LOCAL_NEWER),      # empty cloud, saves here
        ({"local_newer": 1, "unchanged": 1}, sr.LOCAL_NEWER),
        ({"cloud_only": 1}, sr.CLOUD_NEWER),       # a new device
        ({"cloud_newer": 1, "local_only": 1}, sr.CONFLICT),
        ({"conflicts": 1}, sr.CONFLICT),
    ],
)
def test_ea_verdict_from_counts(counts: dict[str, int], expected: str) -> None:
    assert cloud_verdict(counts) == expected


# ── automatic upload after play ──────────────────────────────────────

class _PushRecorder(_Strategy):
    """Records the ``force`` each push was given."""

    def __init__(self, root: Path, save_dir: Path) -> None:
        super().__init__(root, save_dir)
        self.pushes: list[bool] = []

    async def _do_sync_up(self, game_id: str, local_dir: str, *, force: bool = False) -> bool:
        self.pushes.append(force)
        return True


@pytest.fixture
def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _PushRecorder:
    from unifideck.services.cloud_save import safety

    monkeypatch.setattr(safety, "snapshot_backup", lambda *a, **k: None)
    save_dir = tmp_path / "save"
    _write(save_dir / "slot1.sav")
    return _PushRecorder(tmp_path, save_dir)


async def test_auto_push_after_play_uploads_without_force(recorder: _PushRecorder) -> None:
    await recorder.sync_up("g")                      # first sync: recorded
    _write(recorder.save_dir / "slot1.sav", b"progress", mtime=1_700_000_900)
    assert await recorder.sync_up("g", auto=True) is True
    assert recorder.pushes == [True, False]


async def test_auto_push_stops_when_another_device_uploaded(recorder: _PushRecorder) -> None:
    from unifideck.services.cloud_save.safety import SaveConflictError

    await recorder.sync_up("g")
    recorder.cloud_ts = 9000.0                       # someone else pushed since
    _write(recorder.save_dir / "slot1.sav", b"progress", mtime=1_700_000_900)
    with pytest.raises(SaveConflictError) as err:
        await recorder.sync_up("g", auto=True)
    assert err.value.hard is False
    assert recorder.pushes == [True]                 # the cloud was not overwritten


async def test_manual_upload_is_the_users_choice(recorder: _PushRecorder) -> None:
    await recorder.sync_up("g")
    recorder.cloud_ts = 9000.0
    assert await recorder.sync_up("g") is True       # "Upload" / "Use Local"
    assert recorder.pushes == [True, True]


async def test_first_auto_push_has_nothing_to_compare_and_uploads(recorder: _PushRecorder) -> None:
    assert await recorder.sync_up("g", auto=True) is True
    assert recorder.pushes == [False]


def test_auto_upload_is_on_by_default() -> None:
    from unittest.mock import MagicMock

    from unifideck.services.cloud_save.service import CloudSaveService

    service = CloudSaveService(MagicMock(), "/nonexistent/saves")
    assert service.auto_sync_enabled("up") is True
    assert service.auto_sync_enabled("down") is True
