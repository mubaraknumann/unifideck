"""``file_lock.flock_path`` and ``try_exclusive``: cross-process file locks.

``flock`` is per open file description, so a second ``os.open`` in the same
process contends exactly like another process would. A real subprocess is
used once, for the shared-versus-exclusive case that the launcher and the
plugin actually hit.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

from unifideck.launcher.proton.infrastructure import ge_install_lock
from unifideck.launcher.proton.infrastructure.file_lock import flock_path, try_exclusive


def _held_by_other_fd(path: Path, flag: int) -> int:
    """Open ``path`` again and lock it; returns the fd (close it after)."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, flag | fcntl.LOCK_NB)
    return fd


def test_exclusive_lock_is_held_for_the_body(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    with flock_path(lock) as held:
        assert held is True
        assert try_exclusive(lock) is False
    assert try_exclusive(lock) is True


def test_creates_the_file_with_the_requested_mode(tmp_path: Path) -> None:
    lock = tmp_path / "sub" / "a.lock"
    with flock_path(lock, mode=0o600) as held:
        assert held
    assert (lock.stat().st_mode & 0o777) == 0o600


def test_two_shared_holders_coexist(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    with flock_path(lock, shared=True) as first:
        with flock_path(lock, shared=True, timeout_s=0) as second:
            assert first and second


def test_bounded_wait_gives_up_on_a_held_lock(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    fd = _held_by_other_fd(lock, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with flock_path(lock, shared=True, timeout_s=0.3) as held:
            assert held is False
        elapsed = time.monotonic() - started
        assert 0.25 <= elapsed < 2.0
    finally:
        os.close(fd)


def test_bounded_wait_succeeds_once_released(tmp_path: Path) -> None:
    """The poll loop picks the lock up after the holder lets go."""
    lock = tmp_path / "a.lock"
    holder = subprocess.Popen(
        [
            sys.executable, "-c",
            "import fcntl,os,sys,time\n"
            "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600)\n"
            "fcntl.flock(fd,fcntl.LOCK_EX)\n"
            "print('held',flush=True)\n"
            "time.sleep(0.4)\n",
            str(lock),
        ],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with flock_path(lock, shared=True, timeout_s=5) as held:
            assert held is True
    finally:
        holder.wait(timeout=5)
        holder.stdout.close()  # type: ignore[union-attr]


def test_zero_timeout_is_one_try(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    fd = _held_by_other_fd(lock, fcntl.LOCK_SH)
    try:
        with flock_path(lock, timeout_s=0) as held:
            assert held is False
        with flock_path(lock, shared=True, timeout_s=0) as held:
            assert held is True
    finally:
        os.close(fd)


def test_unopenable_lock_yields_false(tmp_path: Path) -> None:
    """A lock under a regular file cannot exist; the body still runs."""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    ran = []
    with flock_path(blocker / "a.lock") as held:
        ran.append(held)
    assert ran == [False]


def test_lock_is_released_when_the_body_raises(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    try:
        with flock_path(lock):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert try_exclusive(lock) is True


def test_try_exclusive_on_a_missing_file_is_free(tmp_path: Path) -> None:
    lock = tmp_path / "missing.lock"
    assert try_exclusive(lock) is True
    assert not lock.exists()


def test_try_exclusive_sees_a_shared_holder(tmp_path: Path) -> None:
    lock = tmp_path / "a.lock"
    fd = _held_by_other_fd(lock, fcntl.LOCK_SH)
    try:
        assert try_exclusive(lock) is False
    finally:
        os.close(fd)
    assert try_exclusive(lock) is True


def test_ge_install_lock_still_delegates(tmp_path: Path, monkeypatch) -> None:
    """``install_lock`` keeps its semantics: blocking, exclusive, 0644."""
    lock = tmp_path / "ge.lock"
    monkeypatch.setattr(ge_install_lock, "INSTALL_LOCK", lock)
    with ge_install_lock.install_lock() as held:
        assert held is True
        assert try_exclusive(lock) is False
    assert (lock.stat().st_mode & 0o777) == 0o644
