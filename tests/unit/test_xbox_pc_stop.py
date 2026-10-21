"""The staged stop, against real fake processes.

``xodus-cli`` is a Python child in its own session. The runner is a shell
script that records what it was asked to do and, for ``taskkill``, sends
the "game" a signal it treats as WM_CLOSE. Every bounded wait must use
``asyncio.wait``; a 3.10 ``wait_for`` timeout would escape the stop.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc.runner import NO_WAIT_FLAG, STOP_FLAG
from unifideck.launcher.xbox_pc.stop import StagedStopper, run_helper, wait_exit

#: A "game" that exits 0 on SIGUSR1 (our WM_CLOSE) and, if ``ignore_int``,
#: survives SIGINT. It writes its pid so the runner can find it.
_GAME = """
import os, signal, sys, time, pathlib
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))
signal.signal(signal.SIGUSR1, lambda *a: sys.exit(0))
if sys.argv[2] == "1":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(0.05)
"""


def _runner(tmp_path: Path, *, closes: bool) -> Path:
    """A runner that logs its argv; ``taskkill`` sends SIGUSR1 when ``closes``."""
    log = tmp_path / "runner.log"
    pidfile = tmp_path / "game.pid"
    kill = f'kill -USR1 "$(cat {pidfile})"' if closes else "true"
    script = tmp_path / "runner"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {log}\n'
        f'if [ "$1" = {NO_WAIT_FLAG} ] && [ "$2" = taskkill ]; then {kill}; fi\n'
        "exit 0\n",
    )
    script.chmod(0o755)
    return script


async def _start_game(tmp_path: Path, *, ignore_int: bool) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _GAME, str(tmp_path / "game.pid"), "1" if ignore_int else "0",
        start_new_session=True,
    )
    for _ in range(100):
        if (tmp_path / "game.pid").exists():
            break
        await asyncio.sleep(0.05)
    return proc


def _log(tmp_path: Path) -> str:
    path = tmp_path / "runner.log"
    return path.read_text() if path.exists() else ""


@pytest.mark.asyncio
async def test_wm_close_is_enough_for_a_cooperative_game(tmp_path: Path) -> None:
    proc = await _start_game(tmp_path, ignore_int=False)
    stopper = StagedStopper(proc, runner=_runner(tmp_path, closes=True), exe_name="Game.exe",
                            env=dict(os.environ), close_grace_s=5, sigint_grace_s=5)
    await stopper.staged()
    assert proc.returncode == 0, "the game closed itself after WM_CLOSE"
    assert "taskkill /IM Game.exe" in _log(tmp_path)
    assert STOP_FLAG not in _log(tmp_path)


@pytest.mark.asyncio
async def test_ignored_wm_close_falls_back_to_sigint(tmp_path: Path) -> None:
    proc = await _start_game(tmp_path, ignore_int=False)
    stopper = StagedStopper(proc, runner=_runner(tmp_path, closes=False), exe_name="Game.exe",
                            env=dict(os.environ), close_grace_s=0.3, sigint_grace_s=5)
    await stopper.staged()
    assert proc.returncode is not None and proc.returncode != 0
    assert STOP_FLAG not in _log(tmp_path)


@pytest.mark.asyncio
async def test_a_stubborn_game_is_forced_and_its_group_is_gone(tmp_path: Path) -> None:
    proc = await _start_game(tmp_path, ignore_int=True)
    stopper = StagedStopper(proc, runner=_runner(tmp_path, closes=False), exe_name="Game.exe",
                            env=dict(os.environ), close_grace_s=0.2, sigint_grace_s=0.2)
    await stopper.staged()
    assert proc.returncode == -signal.SIGKILL
    assert STOP_FLAG in _log(tmp_path), "wineserver -k ran before the kill"
    with pytest.raises(ProcessLookupError):
        os.killpg(proc.pid, 0)


@pytest.mark.asyncio
async def test_a_second_cancel_forces_the_stop(tmp_path: Path) -> None:
    """The session's pattern: cancel during ``staged`` jumps to ``force``."""
    proc = await _start_game(tmp_path, ignore_int=True)
    stopper = StagedStopper(proc, runner=_runner(tmp_path, closes=False), exe_name="Game.exe",
                            env=dict(os.environ), close_grace_s=30, sigint_grace_s=30)

    async def supervise() -> None:
        try:
            await stopper.staged()
        except asyncio.CancelledError:
            await stopper.force()
            raise

    task = asyncio.ensure_future(supervise())
    await asyncio.sleep(0.5)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=10)
    assert task in done and task.cancelled()
    assert proc.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_wait_exit_never_raises_a_timeout() -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "import time; time.sleep(30)")
    assert await wait_exit(proc, 0.1) is False
    proc.kill()
    assert await wait_exit(proc, 5) is True


@pytest.mark.asyncio
async def test_run_helper_reports_a_missing_binary(tmp_path: Path) -> None:
    assert await run_helper([str(tmp_path / "nope")], {}) is None
    assert await run_helper(["/bin/sh", "-c", "exit 4"], dict(os.environ)) == 4
    assert await run_helper(["/bin/sh", "-c", "sleep 30"], dict(os.environ), timeout_s=0.2) is None
