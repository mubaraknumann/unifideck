"""Cancelling or stalling an install must kill the download tool's tree.

Audit §3.2 bullet 4 listed cancellation as "only Epic uses shared
``terminate_process_tree``". Measured against the real store code, the
consequence was larger than that reads:

* **GOG** left the whole ``gogdl`` tree alive. gogdl drives its downloads
  through ``multiprocessing`` workers, so its own ``proc.terminate()`` —
  parent only, then ``await asyncio.sleep(1)`` — killed nothing and was
  itself skipped during cancellation, because that ``await`` re-raises
  immediately in a task already being cancelled.
* **Amazon** left the tree alive *and* blocked the cancel: it caught
  ``CancelledError``, then ran ``wait_with_timeout(proc, 3600)`` before
  re-raising. ``DownloadService.cancel`` awaits the task and the RPC awaits
  ``cancel``, so pressing Cancel hung the UI for up to an hour. Epic had the
  same two steps in the opposite order all along.

Epic's guard for this lives in ``test_epic_phantom_install.py``; these are
its GOG and Amazon counterparts, plus the stall half, which is new to all
three.

The Amazon test asserts the cancel **returns promptly**. Without that it
would pass against the original blocking code, because the tree does
eventually get killed — an hour later.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unifideck.stores.shared.cli_install_helpers import (
    InstallStalledError,
    drain_install_output,
)


class _FakeProc:
    """A subprocess whose stdout does whatever the test needs."""

    def __init__(self, stdout: Any) -> None:
        self.stdout = stdout
        self.returncode: int | None = None
        self.pid = 4242

    async def wait(self) -> int:
        """Never returns on its own — the tests drive termination."""
        await asyncio.sleep(3600)
        return 0


class _CancellingStdout:
    """stdout that raises CancelledError, as a real cancel does mid-read."""

    async def readline(self) -> bytes:
        raise asyncio.CancelledError


class _SilentStdout:
    """stdout that never produces a line — a CLI wedged with stdout open."""

    async def readline(self) -> bytes:
        await asyncio.sleep(3600)
        return b""


class _HeartbeatStdout:
    """stdout that keeps emitting, slower than one read but never stalling."""

    def __init__(self, beats: int, gap: float) -> None:
        self._left = beats
        self._gap = gap

    async def readline(self) -> bytes:
        await asyncio.sleep(self._gap)
        if self._left <= 0:
            return b""
        self._left -= 1
        return b"Progress: 10.0 100/1000\n"


async def _noop_handler(
    _line: str, _game_id: str, _cb: Any,
) -> None:
    """Line handler that does nothing."""
    return


# --------------------------------------------------------------------------
# The shared drain: stall detection
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_silent_cli_raises_instead_of_hanging_forever() -> None:
    """Without ``stall_s`` this awaits ``readline()`` with no bound at all.

    That is why Epic's 7200s and Amazon's 3600s install timeouts could not
    fire: ``wait_with_timeout`` runs only *after* EOF.
    """
    proc = _FakeProc(_SilentStdout())
    with pytest.raises(InstallStalledError) as excinfo:
        await drain_install_output(proc, "g", None, _noop_handler, stall_s=0.05)
    assert "no output" in str(excinfo.value)
    assert excinfo.value.in_finalize is False


@pytest.mark.asyncio
async def test_a_live_heartbeat_is_never_killed() -> None:
    """The negative case, which is the one that matters.

    Every one of these CLIs prints roughly once a second while bytes move.
    A watchdog that fires on a slow-but-live download is worse than none.
    """
    proc = _FakeProc(_HeartbeatStdout(beats=6, gap=0.02))
    await drain_install_output(proc, "g", None, _noop_handler, stall_s=0.2)


@pytest.mark.asyncio
async def test_no_stall_window_means_no_watchdog() -> None:
    """Callers that pass nothing keep the old unbounded behaviour."""
    proc = _FakeProc(_HeartbeatStdout(beats=2, gap=0.01))
    await drain_install_output(proc, "g", None, _noop_handler)


@pytest.mark.asyncio
async def test_the_finalize_window_widens_once_the_predicate_flips() -> None:
    """GOG's two-phase shape: a short download window, a long tail window.

    A flat short window killed installs mid-extraction; a flat long one
    cannot tell a stall from a slow CDN.
    """
    in_tail = {"v": False}
    proc = _FakeProc(_SilentStdout())

    # Still downloading: the short window applies and fires.
    with pytest.raises(InstallStalledError) as excinfo:
        await drain_install_output(
            proc, "g", None, _noop_handler,
            stall_s=0.05, finalize_s=5.0, in_finalize=lambda: in_tail["v"],
        )
    assert excinfo.value.in_finalize is False

    # In the tail: the wide window applies, so the same silence is tolerated.
    in_tail["v"] = True
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            drain_install_output(
                proc, "g", None, _noop_handler,
                stall_s=0.05, finalize_s=5.0, in_finalize=lambda: in_tail["v"],
            ),
            timeout=0.3,
        )


@pytest.mark.asyncio
async def test_the_stall_error_names_the_phase_it_fired_in() -> None:
    """A support bundle should say whether it died downloading or finalizing."""
    proc = _FakeProc(_SilentStdout())
    with pytest.raises(InstallStalledError) as excinfo:
        await drain_install_output(
            proc, "g", None, _noop_handler,
            stall_s=0.05, finalize_s=0.05, in_finalize=lambda: True,
        )
    assert excinfo.value.in_finalize is True
    assert "finalizing" in str(excinfo.value)


# --------------------------------------------------------------------------
# GOG — cancel must kill gogdl's whole tree
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancelling_a_gog_install_kills_the_gogdl_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gogdl's multiprocessing workers outlive a parent-only kill."""
    import unifideck.stores.gog.install.progress as progress_mod

    killed: list[str] = []

    async def fake_terminate(p: Any, _prefix: str, **_kw: Any) -> None:
        killed.append("tree")
        p.returncode = -15

    monkeypatch.setattr(progress_mod, "terminate_process_tree", fake_terminate)

    monitor = progress_mod._GogdlProgressMonitor.__new__(
        progress_mod._GogdlProgressMonitor,
    )
    proc = _FakeProc(_CancellingStdout())

    with pytest.raises(asyncio.CancelledError):
        await monitor._run_and_watch(proc, "g", None)

    assert killed == ["tree"], "cancel must terminate gogdl's process tree"


@pytest.mark.asyncio
async def test_a_stalled_gog_install_kills_the_tree_and_reports_the_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stall is a failure result, not an exception, and names the cause."""
    import unifideck.stores.gog.install.progress as progress_mod

    killed: list[str] = []

    async def fake_terminate(p: Any, _prefix: str, **_kw: Any) -> None:
        killed.append("tree")
        p.returncode = -15

    monkeypatch.setattr(progress_mod, "terminate_process_tree", fake_terminate)
    monkeypatch.setattr(progress_mod, "_GOGDL_STALL_TIMEOUT_S", 0.05)

    monitor = progress_mod._GogdlProgressMonitor.__new__(
        progress_mod._GogdlProgressMonitor,
    )
    outcome = await monitor._run_and_watch(_FakeProc(_SilentStdout()), "g", None)

    assert killed == ["tree"]
    assert not outcome.ok
    assert outcome.rc == -1
    assert "stalled" in outcome.tail


# --------------------------------------------------------------------------
# Amazon — cancel must kill the tree AND return promptly
# --------------------------------------------------------------------------

def _amazon_installer() -> Any:
    """An AmazonInstaller with just enough wiring to run one install."""
    from unifideck.stores.amazon.amazon_install import AmazonInstaller

    inst = AmazonInstaller.__new__(AmazonInstaller)
    inst._cli_path = "/nonexistent/nile"
    inst._install_timeout = 3600
    inst._current_progress = {}
    return inst


@pytest.mark.asyncio
async def test_cancelling_an_amazon_install_kills_the_nile_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nile kept running after the row already read "Cancelled"."""
    import unifideck.stores.amazon.amazon_install as install_mod

    killed: list[str] = []

    async def fake_terminate(p: Any, _prefix: str, **_kw: Any) -> None:
        killed.append("tree")
        p.returncode = -15

    proc = _FakeProc(_CancellingStdout())

    async def fake_exec(*_cmd: str, **_kw: Any) -> Any:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(install_mod, "terminate_process_tree", fake_terminate)

    with pytest.raises(asyncio.CancelledError):
        await _amazon_installer()._run_install("/tmp", "g", None)

    assert killed == ["tree"], "cancel must terminate nile's process tree"


@pytest.mark.asyncio
async def test_cancelling_an_amazon_install_returns_promptly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The half a tree-kill assertion alone would not catch.

    The original code killed nothing but *did* eventually unwind — after
    ``wait_with_timeout(proc, 3600)``. ``DownloadService.cancel`` awaits the
    install task, and the cancel RPC awaits that, so the UI hung with it.
    Bounding the wait here is what distinguishes fixed from unfixed.
    """
    import unifideck.stores.amazon.amazon_install as install_mod

    async def fake_terminate(p: Any, _prefix: str, **_kw: Any) -> None:
        p.returncode = -15

    proc = _FakeProc(_CancellingStdout())

    async def fake_exec(*_cmd: str, **_kw: Any) -> Any:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(install_mod, "terminate_process_tree", fake_terminate)

    inst = _amazon_installer()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            inst._run_install("/tmp", "g", None), timeout=2.0,
        )


@pytest.mark.asyncio
async def test_a_stalled_amazon_install_fails_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before this, a wedged nile blocked every queued game behind it."""
    import unifideck.stores.amazon.amazon_install as install_mod

    killed: list[str] = []

    async def fake_terminate(p: Any, _prefix: str, **_kw: Any) -> None:
        killed.append("tree")
        p.returncode = -15

    proc = _FakeProc(_SilentStdout())

    async def fake_exec(*_cmd: str, **_kw: Any) -> Any:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(install_mod, "terminate_process_tree", fake_terminate)
    monkeypatch.setattr(install_mod, "DEFAULT_STALL_TIMEOUT_S", 0.05)

    outcome = await asyncio.wait_for(
        _amazon_installer()._run_install("/tmp", "g", None),
        timeout=2.0,
    )
    assert killed == ["tree"]
    assert outcome.rc == -1
    assert "stalled" in outcome.tail
    assert install_mod._format_exit_error(outcome).startswith("nile_exit_-1:")


# --------------------------------------------------------------------------
# Epic — gains stall detection it never had. Cancel is already guarded in
# test_epic_phantom_install.py; this is the new half.
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_stalled_epic_install_fails_instead_of_hanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wedged legendary used to hang the queue until a plugin restart."""
    import unifideck.stores.epic.install as install_mod

    killed: list[str] = []

    async def fake_terminate(p: Any, _prefix: str, **_kw: Any) -> None:
        killed.append("tree")
        p.returncode = -15

    proc = _FakeProc(_SilentStdout())

    async def fake_exec(*_cmd: str, **_kw: Any) -> Any:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(install_mod, "terminate_process_tree", fake_terminate)
    monkeypatch.setattr(install_mod, "DEFAULT_STALL_TIMEOUT_S", 0.05)

    inst = install_mod.EpicInstaller.__new__(install_mod.EpicInstaller)
    inst._cli_path = "/nonexistent/legendary"
    inst._install_timeout = 7200

    outcome = await asyncio.wait_for(
        inst._run_install("/tmp", "g", with_dlc=False), timeout=2.0,
    )
    assert killed == ["tree"]
    assert outcome.rc == -1
    assert "stalled" in outcome.tail
    assert install_mod._format_exit_error(outcome).startswith("legendary_exit_-1:")
