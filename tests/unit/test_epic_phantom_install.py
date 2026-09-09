"""Phantom Epic installs: legendary refuses, exits 0, we called it success.

Reported as "River City Girls 2 installed instantly" — the progress bar
jumped straight to complete, the shortcut flipped to installed, and
nothing was on disk. The log told the whole story::

    [EpicInstall] executing: … install 972f47c7… --yes --skip-sdl --with-dlcs
    [EpicInstall] legendary exit_code=0                    # 1.2s later
    …
    [cli] CRITICAL: Game not installed: 972f47c7…          # legendary disagrees
    [DownloadWorker] completed install for epic:972f47c7…  # we don't
    [ShortcutService] mark_installed … — empty exe_path

Running the identical command by hand printed the missing piece::

    [cli] CRITICAL: Failed to acquire installed data lock, only one
    instance of Legendary may install/import/move applications at a time.

legendary guards install/import/move with a ``FileLock`` on
``installed.json.lock`` and answers a refusal with ``logger.fatal`` — then
**exits 0**, so the refusal is invisible to an exit-code check.

Why the lock was held: a *cancelled* Chivalry 2 install. ``cancel()``
called ``task.cancel()``, which unwinds our coroutine but never touches
the spawned process — legendary and its multiprocessing child were still
running 15 minutes later, still holding the inherited lock FD. So one
cancel silently turned every later Epic install into a phantom success.

Two independent fixes, tested here:

1. exit 0 is not proof — legendary writes ``installed.json`` before
   exiting, so its own bookkeeping is checked before reporting success;
2. cancelling kills the process *tree*, so no orphan keeps the lock.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any
from unittest.mock import AsyncMock

import pytest

from unifideck.stores.epic import sdl
from unifideck.stores.epic.install import EpicInstaller, _no_install_error, _RunOutcome
from unifideck.stores.shared.cli_install_helpers import (
    _process_tree,
    terminate_process_tree,
)

_LOCK_CRITICAL = (
    "[cli] CRITICAL: Failed to acquire installed data lock, only one "
    "instance of Legendary may install/import/move applications at a time."
)


@pytest.fixture(autouse=True)
def _isolate_legendary_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """Keep SDL resolution and installed.json off the real config dir."""
    monkeypatch.setenv("LEGENDARY_CONFIG_DIR", str(tmp_path / "legendary"))
    monkeypatch.setattr(sdl, "_CACHE_DIR", str(tmp_path / "sdl-cache"))


class _FakeStdout:
    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{ln}\n".encode() for ln in lines]

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, lines: list[str], returncode: int) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode
        self.pid = 4242

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover
        pass


def _installer() -> EpicInstaller:
    inst = EpicInstaller.__new__(EpicInstaller)
    inst._bus = AsyncMock()
    inst._cli_path = "/opt/plugin/bin/legendary"
    inst._library = AsyncMock()
    inst._library.invalidate_installed_cache = lambda: None
    inst._exe_resolver = AsyncMock()
    inst._default_install_root = "/games"
    inst._install_timeout = 7200
    inst._uninstall_timeout = 120
    return inst


def _download_events(bus: AsyncMock) -> list[str]:
    """Names of every ``DOWNLOAD_*`` event the installer emitted.

    Always empty: ``DownloadWorker`` is the sole emitter of the download
    lifecycle, and the installer reports through its ``InstallResult``
    only (audit item #4).
    """
    out = []
    for call in bus.emit.await_args_list:
        args, kwargs = call
        name = args[0] if args else kwargs.get("event")
        value = getattr(name, "value", name)
        if isinstance(value, str) and value.startswith("download_"):
            out.append(value)
    return out


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch, procs: list[_FakeProc],
) -> list[list[str]]:
    seen: list[list[str]] = []
    queue = list(procs)

    async def fake_exec(*cmd: str, **_kw: Any) -> _FakeProc:
        seen.append(list(cmd))
        return queue.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


def _record_installed(tmp_path: Any, game_id: str) -> None:
    """Write the installed.json row a real install would leave behind."""
    import json
    cfg = tmp_path / "legendary"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "installed.json").write_text(
        json.dumps({game_id: {"install_path": f"/games/{game_id}"}}),
    )


# --------------------------------------------------------------------------
# Fix 1 — exit 0 without an install is a failure
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_lock_refusal_is_a_failure_not_an_instant_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    inst = _installer()
    # legendary's actual behaviour: CRITICAL on stdout, exit 0, no install.
    _patch_subprocess(monkeypatch, [_FakeProc([_LOCK_CRITICAL], returncode=0)])

    result = await inst.install_game("rcg2", base_path=str(tmp_path / "games"))

    assert not result.success
    # The user saw "installed" with an empty game folder; now it fails loudly.
    assert result.error.startswith("legendary_install_lock_busy:")
    assert "install lock" in result.error
    assert _download_events(inst._bus) == []


@pytest.mark.asyncio
async def test_bare_exit_zero_without_install_is_also_a_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    # Any other silent exit-0 refusal must be caught by the same check,
    # not just the one message we know about.
    inst = _installer()
    _patch_subprocess(monkeypatch, [_FakeProc(["something odd"], returncode=0)])

    result = await inst.install_game("rcg2", base_path=str(tmp_path / "games"))

    assert not result.success
    assert result.error.startswith("legendary_exit_0_no_install:")


@pytest.mark.asyncio
async def test_a_real_install_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    # The guard must not reject genuine installs: legendary records the
    # game in installed.json before exiting 0.
    inst = _installer()
    from unifideck.core.types import InstallResult
    inst._finalize_install = AsyncMock(
        return_value=InstallResult(success=True, store="epic", game_id="rcg2"),
    )
    _record_installed(tmp_path, "rcg2")
    _patch_subprocess(
        monkeypatch, [_FakeProc(["Progress: 100.0%"], returncode=0)],
    )

    result = await inst.install_game("rcg2", base_path=str(tmp_path / "games"))

    assert result.success
    assert _download_events(inst._bus) == []


def test_lock_refusal_gets_its_own_error_code() -> None:
    err = _no_install_error(_RunOutcome(rc=0, tail=_LOCK_CRITICAL))
    assert err.startswith("legendary_install_lock_busy:")
    # It must say what to do about it, since the user can act on this one.
    assert "another Epic install" in err


def test_unexplained_exit_zero_keeps_a_parsable_prefix() -> None:
    err = _no_install_error(_RunOutcome(rc=0, tail=""))
    assert err.startswith("legendary_exit_0_no_install:")
    assert "no output captured" in err


# --------------------------------------------------------------------------
# Fix 2 — cancelling kills the tree, so no orphan keeps the lock
# --------------------------------------------------------------------------
def test_process_tree_finds_descendants() -> None:
    # Walks procfs rather than using killpg: these children share the
    # plugin host's process group, so a group kill would take down
    # plugin_loader itself.
    tree = _process_tree(os.getpid())
    assert tree[0] == os.getpid()


@pytest.mark.asyncio
async def test_terminate_kills_a_real_child_process() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)",
    )
    assert proc.returncode is None

    await terminate_process_tree(proc, "[test]")

    assert proc.returncode is not None


@pytest.mark.asyncio
async def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored() -> None:
    # A legendary child that ignores SIGTERM would otherwise survive and
    # keep the install lock held — the exact orphan that poisoned the queue.
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n",
    )
    try:
        await asyncio.sleep(0.5)  # let the handler install
        await terminate_process_tree(proc, "[test]", grace_s=0.5)
        assert proc.returncode is not None
        assert proc.returncode in (-signal.SIGKILL, signal.SIGKILL, 137)
    finally:
        if proc.returncode is None:  # pragma: no cover - safety net
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_terminate_is_safe_on_an_already_exited_process() -> None:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()
    await terminate_process_tree(proc, "[test]")  # must not raise


@pytest.mark.asyncio
async def test_cancelling_an_install_kills_legendary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """A cancel must not leave legendary running and holding the lock."""
    killed: list[str] = []

    class _CancellingStdout:
        async def readline(self) -> bytes:
            raise asyncio.CancelledError

    class _LiveProc:
        def __init__(self) -> None:
            self.stdout = _CancellingStdout()
            self.returncode: int | None = None
            self.pid = 4242

        async def wait(self) -> int:
            return 0

    proc = _LiveProc()

    async def fake_exec(*_cmd: str, **_kw: Any) -> Any:
        return proc

    async def fake_terminate(p: Any, _prefix: str, **_kw: Any) -> None:
        killed.append("tree")
        p.returncode = -15

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    import unifideck.stores.epic.install as install_mod
    monkeypatch.setattr(install_mod, "terminate_process_tree", fake_terminate)

    inst = _installer()
    with pytest.raises(asyncio.CancelledError):
        await inst.install_game("rcg2", base_path=str(tmp_path / "games"))

    assert killed == ["tree"], "cancel must terminate legendary's process tree"
