"""A failed GOG install must name its cause, like Epic's and Amazon's do.

Mirrors ``test_amazon_install_error_surfacing.py`` and
``test_epic_install_error_and_dlc_retry.py``. GOG was the store that never
got this treatment: ``run_gogdl_with_progress`` returned a plain ``bool``,
so the installer could only report ``download_failed`` — a token that is not
an i18n key, which ``friendlyDownloadError`` therefore echoed verbatim into
the toast and the failed row in all 16 locales (audit §3.2).

Worse than untranslated: the frontend classifier branches on the *message
text*, so with no CLI output attached a GOG install that died from a full
disk or a dropped connection could never match the disk / network / auth
branches that Epic and Amazon hit for the identical cause.

These assert the string that reaches the frontend contract, not the internal
outcome object.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import unifideck.stores.gog.install.progress as progress_mod
from unifideck.stores.gog.install.progress import (
    _RunOutcome,
    format_gogdl_error,
)


class _ScriptedStdout:
    """stdout that replays a captured gogdl session, then EOF."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _ExitingProc:
    """A subprocess that has already exited with ``rc``."""

    def __init__(self, stdout: Any, rc: int) -> None:
        self.stdout = stdout
        self.returncode: int | None = None
        self.pid = 4242
        self._rc = rc

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc


def _monitor() -> Any:
    """A progress monitor with no installer attached — the drain needs none."""
    return progress_mod._GogdlProgressMonitor.__new__(
        progress_mod._GogdlProgressMonitor,
    )


# --------------------------------------------------------------------------
# The error string itself
# --------------------------------------------------------------------------

def test_the_format_matches_what_the_frontend_already_strips() -> None:
    """``EXIT_PREFIX_RE`` in download-errors.ts is ``^\\w+_exit_-?\\d+:\\s*``.

    It cited ``gogdl_exit_2:`` as an example for a long time while nothing in
    the tree ever produced one.
    """
    err = format_gogdl_error(_RunOutcome(rc=2, tail="[cli] ERROR: boom"))
    assert err == "gogdl_exit_2: [cli] ERROR: boom"


def test_a_bare_failure_still_has_a_machine_parsable_prefix() -> None:
    """No captured output is not a reason to lose the exit code."""
    assert format_gogdl_error(_RunOutcome(rc=5)) == "gogdl_exit_5"


def test_a_process_that_never_started_reports_like_amazons() -> None:
    """There is no exit code to report, so it must not claim one."""
    err = format_gogdl_error(
        _RunOutcome(rc=-2, tail="[Errno 13] Permission denied"),
    )
    assert err == "gogdl_spawn_failed: [Errno 13] Permission denied"


# --------------------------------------------------------------------------
# The tail actually gets captured off a real drain
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gogdl_error_lines_survive_into_the_failure_message() -> None:
    """The disk-full case, end to end through the real read loop.

    This is the shape that matters: the tail must contain the words the
    frontend classifier looks for, or GOG is back to a raw token.
    """
    proc = _ExitingProc(
        _ScriptedStdout([
            b"Progress: 10.0 100/1000, ETA: 00:01:00\n",
            b" + Download\t+ 12.30 MiB/s\n",
            b"[cli] ERROR: Not enough available disk space!\n",
        ]),
        rc=1,
    )
    outcome = await _monitor()._run_and_watch(proc, "g", None)

    assert not outcome.ok
    assert outcome.rc == 1
    assert "Not enough available disk space" in outcome.tail
    assert "disk space" in format_gogdl_error(outcome).lower()


@pytest.mark.asyncio
async def test_progress_lines_are_not_mistaken_for_error_output() -> None:
    """The tail is for the lines gogdl prints *instead of* progress."""
    proc = _ExitingProc(
        _ScriptedStdout([
            b"Progress: 50.0 500/1000, ETA: 00:00:30\n",
            b" + Download\t+ 9.00 MiB/s\n",
        ]),
        rc=1,
    )
    outcome = await _monitor()._run_and_watch(proc, "g", None)
    assert "Progress:" not in outcome.tail
    assert "MiB/s" not in outcome.tail


@pytest.mark.asyncio
async def test_a_successful_run_reports_ok() -> None:
    """The regression guard: this path is every working install."""
    proc = _ExitingProc(
        _ScriptedStdout([b"Progress: 100.0 1000/1000\n", b"Done\n"]),
        rc=0,
    )
    outcome = await _monitor()._run_and_watch(proc, "g", None)
    assert outcome.ok
    assert outcome.rc == 0


@pytest.mark.asyncio
async def test_progress_still_reaches_the_callback() -> None:
    """Capturing the tail must not cost the progress reporting."""
    seen: list[dict[str, Any]] = []

    async def cb(update: dict[str, Any]) -> None:
        seen.append(dict(update))

    proc = _ExitingProc(
        _ScriptedStdout([
            b"Progress: 42.5 425/1000, Running for: 00:00:10, ETA: 00:00:30\n",
            b" + Download\t+ 12.30 MiB/s\n",
        ]),
        rc=0,
    )
    await _monitor()._run_and_watch(proc, "g", cb)

    assert seen, "progress callback was never invoked"
    assert seen[0]["progress_percent"] == 42.5
    assert seen[0]["eta_seconds"] == 30
    assert seen[-1]["speed_bps"] == 12.30 * 1024 * 1024


@pytest.mark.asyncio
async def test_crossing_99_percent_flips_the_row_to_extracting() -> None:
    """The phase flip GOG already had — preserved through the shared drain.

    ``gog-install-100pct-hang`` is the incident behind it: without this the
    row sits frozen at 100% through extraction and reads as a hang.
    """
    phases: list[str] = []

    async def cb(update: dict[str, Any]) -> None:
        if update.get("phase"):
            phases.append(str(update["phase"]))

    proc = _ExitingProc(
        _ScriptedStdout([
            b"Progress: 50.0 500/1000\n",
            b"Progress: 99.5 995/1000\n",
        ]),
        rc=0,
    )
    await _monitor()._run_and_watch(proc, "g", cb)
    assert "extracting" in phases


@pytest.mark.asyncio
async def test_a_callback_that_raises_never_fails_the_install(
) -> None:
    """A UI error must not turn a working download into a failed one."""

    async def bad_cb(_update: dict[str, Any]) -> None:
        raise RuntimeError("frontend went away")

    proc = _ExitingProc(
        _ScriptedStdout([b"Progress: 10.0 100/1000\n"]), rc=0,
    )
    outcome = await _monitor()._run_and_watch(proc, "g", bad_cb)
    assert outcome.ok


@pytest.mark.asyncio
async def test_a_process_that_closes_stdout_and_hangs_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gogdl can close stdout and then linger; the queue must not wedge."""
    killed: list[str] = []

    async def fake_terminate(p: Any, _prefix: str, **_kw: Any) -> None:
        killed.append("tree")
        p.returncode = -15

    monkeypatch.setattr(progress_mod, "terminate_process_tree", fake_terminate)
    monkeypatch.setattr(progress_mod, "_GOGDL_FINALIZE_TIMEOUT_S", 0.05)

    class _HangingProc(_ExitingProc):
        async def wait(self) -> int:
            await asyncio.sleep(3600)
            return 0

    proc = _HangingProc(_ScriptedStdout([b"Done\n"]), rc=0)
    outcome = await asyncio.wait_for(
        _monitor()._run_and_watch(proc, "g", None), timeout=2.0,
    )
    assert killed == ["tree"]
    assert not outcome.ok
    assert "did not exit" in outcome.tail
