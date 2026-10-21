"""Tests for launcher/xbox_pc/restart_watch.py: a game that keeps restarting itself (I-044)."""
from __future__ import annotations

import itertools
import os
import time
from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc.restart_watch import (
    RestartCounter,
    eac_linux_client_refused,
    game_processes,
    watch_restarts,
    wine_path,
)

_EAC_LOG = "drive_c/users/steamuser/AppData/Roaming/EasyAntiCheat/269/loader.log"
_REFUSED = (
    "[2026.10.22-01.48.55] [Connection] Connecting to URL: "
    "https://download.eac-cdn.com/api/v1/games/269/client/wine64/download/?uuid=x\n"
    "[2026.10.22-01.48.55] [EAC Callback] Code 915. Message: '[HTTP Error Response] HTTP Response Code: 200'.\n"
)


def test_a_loop_is_the_limit_of_starts_within_the_window() -> None:
    counter = RestartCounter(limit=3, window_s=10.0)
    assert counter.observe({1: "game.exe"}, 0.0) is None
    assert counter.observe({2: "game.exe"}, 1.0) is None
    assert counter.observe({3: "game.exe"}, 2.0) == "game.exe"


def test_starts_older_than_the_window_do_not_count() -> None:
    counter = RestartCounter(limit=3, window_s=10.0)
    for pid, now in ((1, 0.0), (2, 20.0), (3, 40.0), (4, 60.0)):
        assert counter.observe({pid: "game.exe"}, now) is None


def test_a_process_seen_again_is_not_a_new_start() -> None:
    counter = RestartCounter(limit=2, window_s=10.0)
    for now in range(5):
        assert counter.observe({1: "game.exe"}, float(now)) is None


def test_many_copies_alive_at_once_are_not_a_loop() -> None:
    # An embedded browser runs many copies of one executable at once.
    counter = RestartCounter(limit=3, window_s=10.0)
    assert counter.observe(dict.fromkeys(range(1, 7), "browser.exe"), 0.0) is None


def test_game_processes_lists_only_the_install_folders_executables(tmp_path: Path) -> None:
    install = tmp_path / "Xbox" / "9PK087LNGJC5"
    proc = tmp_path / "proc"
    view = wine_path(install) + "\\.xodus-chain-abc\\Bin\\Game.exe"
    for pid, argv in (
        ("10", f"{view}\0-flag\0"),
        ("11", "Z:\\elsewhere\\Other.exe\0"),
        ("12", "/usr/bin/python3\0"),
        ("self", f"{view}\0"),
    ):
        (proc / pid).mkdir(parents=True)
        (proc / pid / "cmdline").write_bytes(argv.encode())
    (proc / "13").mkdir()
    assert game_processes(install, proc) == {10: "game.exe"}


def test_game_processes_on_a_missing_proc_is_empty(tmp_path: Path) -> None:
    assert game_processes(tmp_path, tmp_path / "missing") == {}


@pytest.mark.asyncio
async def test_watch_restarts_returns_the_looping_executable() -> None:
    pids = itertools.count(1)
    looping = await watch_restarts(
        lambda: {next(pids): "game.exe"}, RestartCounter(limit=4, window_s=60.0), poll_s=0.0,
    )
    assert looping == "game.exe"


def test_eac_refused_its_linux_client(tmp_path: Path) -> None:
    log = tmp_path / _EAC_LOG
    log.parent.mkdir(parents=True)
    log.write_text(_REFUSED)
    assert eac_linux_client_refused(tmp_path, since=time.time() - 60)


def test_an_older_eac_log_does_not_count(tmp_path: Path) -> None:
    log = tmp_path / _EAC_LOG
    log.parent.mkdir(parents=True)
    log.write_text(_REFUSED)
    old = time.time() - 3600
    os.utime(log, (old, old))
    assert not eac_linux_client_refused(tmp_path, since=time.time() - 60)


def test_an_eac_log_without_the_refusal_does_not_count(tmp_path: Path) -> None:
    log = tmp_path / _EAC_LOG
    log.parent.mkdir(parents=True)
    log.write_text("[x] Loader initialized.\n[x] Download Progress: 100%.\n")
    assert not eac_linux_client_refused(tmp_path, since=time.time() - 60)
    assert not eac_linux_client_refused(tmp_path / "no-prefix", since=0.0)
