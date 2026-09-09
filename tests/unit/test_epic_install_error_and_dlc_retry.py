"""UD-026: Epic installs surface legendary's real error + retry without DLC.

Two regressions from the same report ("Fallout 3: GOTY fails to download,
other Epic games work"):

1. **Opaque failures.** The installer streamed legendary's stdout but
   forwarded only ``Progress:``/speed lines; legendary's actual error
   (``[cli] ERROR: …``) went to a DEBUG log and was dropped, so a failed
   install surfaced only ``legendary_exit_{rc}`` → a generic "Failed".
   Now the last non-progress lines are captured in a ``TailRingBuffer``
   and folded into the error (``legendary_exit_{rc}: <tail>``).

2. **A single broken DLC blocks the whole game.** ``--with-dlcs`` makes
   legendary install the base game then each DLC; one bad DLC manifest
   aborts the command after the base game is already on disk. A GOTY
   edition (5 DLC) is exactly this case, so a failed DLC attempt now
   retries once with an explicit ``--skip-dlcs``.

These are pure-logic tests: a fake subprocess (scripted stdout +
returncode) replaces ``asyncio.create_subprocess_exec`` — no network, no
real legendary.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from unifideck.services.download.models import classify_download_error
from unifideck.stores.epic import sdl
from unifideck.stores.epic.install import EpicInstaller, _format_exit_error
from unifideck.stores.shared.cli_install_helpers import TailRingBuffer


@pytest.fixture(autouse=True)
def _isolate_legendary_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    """Keep the install path off the developer's real legendary state.

    ``install_game`` now resolves Selective Downloads tags, which reads
    legendary's cached ``version.json``. Redirecting it keeps these tests
    hermetic (and off the network) regardless of the host's Epic library.
    """
    monkeypatch.setenv("LEGENDARY_CONFIG_DIR", str(tmp_path / "legendary"))
    monkeypatch.setattr(sdl, "_CACHE_DIR", str(tmp_path / "sdl-cache"))


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class _FakeStdout:
    """A stream whose ``readline`` yields scripted bytes then EOF."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [f"{ln}\n".encode() for ln in lines]

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess."""

    def __init__(self, lines: list[str], returncode: int) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover - timeout path unused here
        pass


def _installer() -> EpicInstaller:
    """Build an installer with mocked collaborators (no real I/O)."""
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


def _record_installed(tmp_path: Any, game_id: str) -> None:
    """Write the ``installed.json`` row a real install leaves behind.

    ``install_game`` verifies legendary's own bookkeeping before
    reporting success, because legendary answers a refusal (e.g. its
    install lock already held) with exit 0 — see
    ``test_epic_phantom_install.py``. A mocked rc=0 therefore has to
    leave the same trace a genuine install would.
    """
    cfg = tmp_path / "legendary"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "installed.json").write_text(
        json.dumps({game_id: {"install_path": f"/games/{game_id}"}}),
    )


def _download_events(bus: AsyncMock) -> list[str]:
    """Names of every ``DOWNLOAD_*`` event the installer emitted.

    Must always be empty. ``DownloadWorker`` is the sole emitter of the
    download lifecycle for all six stores; the installer only returns an
    ``InstallResult`` and the worker turns that into the one terminal
    event, carrying the queue item the UI needs. This installer used to
    emit its own store-shaped copy too, which surfaced as a second,
    title-less failure toast (audit item #4).
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
    """Feed ``procs`` to successive ``create_subprocess_exec`` calls.

    Returns a list that accumulates the argv of each spawned command so a
    test can assert the flags (``--with-dlcs`` / ``--skip-dlcs``).
    """
    seen: list[list[str]] = []
    queue = list(procs)

    async def fake_exec(*cmd: str, **_kw: Any) -> _FakeProc:
        seen.append(list(cmd))
        return queue.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


# --------------------------------------------------------------------------
# TailRingBuffer
# --------------------------------------------------------------------------
def test_tail_ring_buffer_keeps_only_last_n() -> None:
    buf = TailRingBuffer(maxlen=3)
    for i in range(10):
        buf.append(f"line {i}")
    buf.append("")  # empty lines are ignored
    assert buf.tail(count=3) == "line 7 | line 8 | line 9"


def test_tail_ring_buffer_empty_is_blank() -> None:
    assert TailRingBuffer().tail() == ""


# --------------------------------------------------------------------------
# _build_install_cmd flag shape
# --------------------------------------------------------------------------
def test_build_install_cmd_with_dlc() -> None:
    cmd = _installer()._build_install_cmd("/games", "abc123", with_dlc=True)
    assert "--with-dlcs" in cmd
    assert "--skip-dlcs" not in cmd


def test_build_install_cmd_skips_dlc_explicitly() -> None:
    cmd = _installer()._build_install_cmd("/games", "abc123", with_dlc=False)
    # An explicit --skip-dlcs is required — with --yes, merely omitting
    # --with-dlcs would still auto-install DLC.
    assert "--skip-dlcs" in cmd
    assert "--with-dlcs" not in cmd


# --------------------------------------------------------------------------
# _format_exit_error / classifier
# --------------------------------------------------------------------------
def test_format_exit_error_appends_tail() -> None:
    from unifideck.stores.epic.install import _RunOutcome
    err = _format_exit_error(_RunOutcome(rc=1, tail="[cli] ERROR: boom"))
    assert err == "legendary_exit_1: [cli] ERROR: boom"


def test_format_exit_error_bare_when_no_tail() -> None:
    from unifideck.stores.epic.install import _RunOutcome
    assert _format_exit_error(_RunOutcome(rc=2)) == "legendary_exit_2"


def test_classifier_recognizes_legendary_phrases() -> None:
    disk = "legendary_exit_1: [cli] ERROR: No space left on device"
    assert classify_download_error(Exception(disk)) == "disk_full"
    net = "legendary_exit_1: Failed to establish a new connection"
    assert classify_download_error(Exception(net)) == "network_error"
    missing = "legendary_exit_1: Could not find app in list of available games"
    assert classify_download_error(Exception(missing)) == "not_found"


# --------------------------------------------------------------------------
# Full install flow: retry + error surfacing
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_success_runs_single_attempt_no_failure_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    inst = _installer()
    from unifideck.core.types import InstallResult
    inst._finalize_install = AsyncMock(
        return_value=InstallResult(success=True, store="epic", game_id="g"),
    )
    _record_installed(tmp_path, "g")
    seen = _patch_subprocess(
        monkeypatch, [_FakeProc(["Progress: 100.0%"], returncode=0)],
    )

    result = await inst.install_game("g", base_path=str(tmp_path))

    assert result.success
    assert len(seen) == 1  # no spurious retry on success
    assert "--with-dlcs" in seen[0]
    assert _download_events(inst._bus) == []


@pytest.mark.asyncio
async def test_dlc_failure_retries_without_dlc_and_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    inst = _installer()
    from unifideck.core.types import InstallResult
    inst._finalize_install = AsyncMock(
        return_value=InstallResult(success=True, store="epic", game_id="g"),
    )
    _record_installed(tmp_path, "g")
    seen = _patch_subprocess(
        monkeypatch,
        [
            _FakeProc(["[cli] ERROR: Failed to download DLC xyz"], returncode=1),
            _FakeProc(["Progress: 100.0%"], returncode=0),
        ],
    )

    result = await inst.install_game("g", base_path=str(tmp_path))

    assert result.success
    assert len(seen) == 2
    assert "--with-dlcs" in seen[0]
    assert "--skip-dlcs" in seen[1]  # retry drops DLC
    # A recovered install must NOT report a terminal failure — and the
    # installer reports nothing at all, so the successful InstallResult is
    # the only thing the worker sees.
    assert _download_events(inst._bus) == []


@pytest.mark.asyncio
async def test_both_attempts_fail_report_one_failure_with_real_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    inst = _installer()
    err_line = "[cli] ERROR: No app asset found for platform Windows"
    seen = _patch_subprocess(
        monkeypatch,
        [
            _FakeProc([err_line], returncode=1),
            _FakeProc([err_line], returncode=1),
        ],
    )

    result = await inst.install_game("g", base_path=str(tmp_path))

    assert not result.success
    assert len(seen) == 2  # DLC attempt + one no-DLC retry
    # The InstallResult is the only report, and it carries legendary's real
    # text — the worker folds that into the single DOWNLOAD_FAILED.
    assert _download_events(inst._bus) == []
    assert result.error.startswith("legendary_exit_1:")
    assert "No app asset found" in result.error
