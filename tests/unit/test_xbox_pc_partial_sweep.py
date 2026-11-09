"""Abandoned Xbox PC downloads are discarded after 24 hours, and nothing else is."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.xbox_pc.install_records import (
    read_install_record,
    record_path,
    write_install_record,
)
from unifideck.stores.microsoft.xodus.guards import STREAMING_LOCK
from unifideck.stores.microsoft.xodus.partial_sweep import (
    PARTIAL_MAX_AGE_S,
    PartialSweepService,
    discard_stale_partials,
)

PID = "9PK087LNGJC5"
NOW = 2_000_000_000.0
NOTHING_QUEUED: frozenset[str] = frozenset()


@pytest.fixture()
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(
        "unifideck.utils.paths.get_games_map_path",
        lambda config=None: str(tmp_path / "games.map"),
    )
    folder = tmp_path / "home" / "deck" / "Games"
    folder.mkdir(parents=True)
    return folder


def _install(base: Path, *, state: str = "partial", idle_s: float = PARTIAL_MAX_AGE_S + 1) -> Path:
    """One game on disk with a record last touched ``idle_s`` before NOW."""
    root = base / "Xbox" / PID
    (root / "game").mkdir(parents=True)
    (root / "game" / "data.bin").write_text("x")
    record: dict[str, Any] = {
        "schema": 1, "productId": PID, "state": state, "market": "IN",
        "installRoot": str(root), "gameDir": str(root / "game"),
    }
    write_install_record(record)
    os.utime(record_path(PID), (NOW - idle_s, NOW - idle_s))
    return root


def test_discards_a_partial_idle_for_24_hours(base: Path) -> None:
    root = _install(base)
    assert discard_stale_partials(NOTHING_QUEUED, now=NOW) == [PID]
    assert not root.exists()
    assert read_install_record(PID) is None


def test_keeps_a_partial_younger_than_24_hours(base: Path) -> None:
    root = _install(base, idle_s=PARTIAL_MAX_AGE_S - 60)
    assert discard_stale_partials(NOTHING_QUEUED, now=NOW) == []
    assert (root / "game" / "data.bin").exists()
    assert read_install_record(PID) is not None


@pytest.mark.parametrize("state", ["installed", "updating"])
def test_never_touches_a_game_that_is_not_partial(base: Path, state: str) -> None:
    root = _install(base, state=state, idle_s=PARTIAL_MAX_AGE_S * 30)
    assert discard_stale_partials(NOTHING_QUEUED, now=NOW) == []
    assert (root / "game" / "data.bin").exists()
    assert read_install_record(PID) is not None


def test_keeps_a_game_in_the_download_queue(base: Path) -> None:
    root = _install(base)
    assert discard_stale_partials(frozenset({PID}), now=NOW) == []
    assert root.exists()


def test_keeps_a_download_that_is_still_streaming(base: Path) -> None:
    root = _install(base)
    lock = root / "game" / STREAMING_LOCK
    lock.write_text("")
    with lock.open() as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert discard_stale_partials(NOTHING_QUEUED, now=NOW) == []
    assert root.exists()


def test_keeps_the_record_when_the_drive_is_not_connected(base: Path) -> None:
    root = Path("/nonexistent-unifideck-card/Games/Xbox") / PID
    write_install_record({
        "schema": 1, "productId": PID, "state": "partial", "market": "IN",
        "installRoot": str(root), "gameDir": str(root / "game"),
    })
    os.utime(record_path(PID), (NOW - PARTIAL_MAX_AGE_S * 2, NOW - PARTIAL_MAX_AGE_S * 2))
    assert discard_stale_partials(NOTHING_QUEUED, now=NOW) == []
    assert read_install_record(PID) is not None


def test_forgets_a_record_whose_folder_is_provably_gone(base: Path) -> None:
    root = _install(base)
    (root / "game" / "data.bin").unlink()
    (root / "game").rmdir()
    root.rmdir()
    assert discard_stale_partials(NOTHING_QUEUED, now=NOW) == []
    assert read_install_record(PID) is None


def test_keeps_a_folder_outside_our_layout(base: Path) -> None:
    root = base / "Elsewhere"
    (root / "game").mkdir(parents=True)
    write_install_record({
        "schema": 1, "productId": PID, "state": "partial", "market": "IN",
        "installRoot": str(root), "gameDir": str(root / "game"),
    })
    os.utime(record_path(PID), (NOW - PARTIAL_MAX_AGE_S * 2, NOW - PARTIAL_MAX_AGE_S * 2))
    assert discard_stale_partials(NOTHING_QUEUED, now=NOW) == []
    assert root.exists()
    assert read_install_record(PID) is not None


async def test_service_asks_the_queue_on_every_pass(base: Path) -> None:
    root = _install(base, idle_s=PARTIAL_MAX_AGE_S * 2)
    os.utime(record_path(PID), (1.0, 1.0))
    service = PartialSweepService(lambda: frozenset({PID}))
    assert await service.sweep() == []
    assert root.exists()
    service = PartialSweepService(lambda: NOTHING_QUEUED)
    assert await service.sweep() == [PID]
    assert not root.exists()


async def test_stop_without_start_does_nothing() -> None:
    await PartialSweepService(lambda: NOTHING_QUEUED).stop()
