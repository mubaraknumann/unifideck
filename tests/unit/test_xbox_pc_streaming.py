"""``run_streaming``: one ``xodus-cli streaming`` run, both pipes, every ending.

The fake process has separate stdout and stderr streams. Diagnostics are
read from stderr only; progress from stdout only. A cancel or a stall kills
the process tree before anything else is awaited.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _xbox_pc_fakes import PID, FakeProc, FakeStderr, FakeStdout, happy_stdout, record_line

from unifideck.stores.microsoft.xodus import streaming
from unifideck.stores.microsoft.xodus.errors import classify_failure
from unifideck.stores.microsoft.xodus.install_steps import verify
from unifideck.stores.shared import cli_install_helpers as helpers


@pytest.fixture
def kills(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    killed: list[int] = []

    async def fake_terminate(proc: Any, _prefix: str, **_kw: Any) -> None:
        killed.append(proc.pid)
        proc.returncode = -9

    monkeypatch.setattr(helpers, "terminate_process_tree", fake_terminate)
    return killed


async def _run(proc: FakeProc) -> tuple[streaming.StreamOutcome, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def cb(update: dict[str, Any]) -> None:
        sent.append(update)

    return await streaming.run_streaming(proc, PID, cb), sent


async def test_a_clean_run_completes(kills: list[int]) -> None:
    outcome, sent = await _run(FakeProc(FakeStdout(happy_stdout())))
    assert outcome.succeeded and outcome.terminal == "complete"
    # The complete record sends no phase: the row stays on "finalizing" until
    # the worker marks it complete.
    assert sent[0]["phase"] == "starting" and sent[-2]["phase"] == "finalizing"
    assert "phase" not in sent[-1]
    # The 50% record lands within a second of the first downloading one, so
    # the throttle drops it; the phase changes and the end always go out.
    assert [u.get("phase") for u in sent].count("downloading") == 1
    assert sent[-1]["percentage"] == 100.0
    assert kills == []


async def test_diagnostics_come_from_stderr_only(kills: list[int]) -> None:
    stdout = FakeStdout([
        b"NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-http 403\n",  # wrong pipe: ignored
        record_line(0, "initializing"), record_line(1, "failed"),
    ])
    stderr = FakeStderr([b"NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 content-not-", b"entitled 200\n"])
    outcome, _ = await _run(FakeProc(stdout, stderr, rc=1))
    assert [d.stage for d in outcome.diagnostics.licenses()] == ["content-not-entitled"]
    assert classify_failure(outcome).code == "xbox_license_denied"


async def test_a_record_spliced_after_a_diagnostic_fragment_is_still_read(kills: list[int]) -> None:
    spliced = b"NEXUS_CONTENT_LICENSE_DIAGNOSTIC_V1 cont" + record_line(1, "complete", 5, 5)
    outcome, sent = await _run(FakeProc(FakeStdout([record_line(0, "metadata"), spliced])))
    assert outcome.terminal == "complete"
    assert sent[-1]["downloaded_bytes"] == 5


async def test_a_failed_exit_is_xodus_exit_with_the_tail(kills: list[int]) -> None:
    stderr = FakeStderr([b"error: disk full while writing\n"])
    outcome, _ = await _run(FakeProc(FakeStdout([record_line(0, "metadata")]), stderr, rc=3))
    fail = classify_failure(outcome)
    assert fail.code == "xodus_exit_3" and "disk full" in str(fail)


async def test_cancel_kills_the_tree_before_reraising(kills: list[int]) -> None:
    proc = FakeProc(FakeStdout([record_line(0, "metadata")], hang_at_end=True))
    task = asyncio.ensure_future(_run(proc))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kills == [proc.pid]


async def test_silence_before_the_first_record_stalls_on_the_short_window(
    kills: list[int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streaming, "STALL_S", 0.05)
    monkeypatch.setattr(streaming, "STALL_QUIET_S", 5.0)
    outcome, _ = await _run(FakeProc(FakeStdout([], hang_at_end=True)))
    assert outcome.stalled and outcome.rc == -1
    assert classify_failure(outcome).code == "stalled"
    assert kills == [4242]


async def test_a_quiet_phase_gets_the_long_window(
    kills: list[int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streaming, "STALL_S", 0.05)
    monkeypatch.setattr(streaming, "STALL_QUIET_S", 5.0)
    lines = [record_line(0, "metadata"), (0.2, record_line(1, "complete", 1, 1))]
    outcome, _ = await _run(FakeProc(FakeStdout(lines)))
    assert outcome.succeeded and not outcome.stalled
    assert kills == []


async def test_downloading_uses_the_short_window_again(
    kills: list[int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streaming, "STALL_S", 0.05)
    monkeypatch.setattr(streaming, "STALL_QUIET_S", 5.0)
    lines = [record_line(0, "metadata"), record_line(1, "downloading", 0, 9)]
    outcome, _ = await _run(FakeProc(FakeStdout(lines, hang_at_end=True)))
    assert outcome.stalled


async def test_complete_then_a_hung_exit_is_incomplete(
    kills: list[int], monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    monkeypatch.setattr(streaming, "EXIT_WAIT_S", 0.05)
    outcome, _ = await _run(FakeProc(FakeStdout(happy_stdout()), hang_exit=True))
    assert outcome.terminal == "complete" and outcome.rc == -1
    with pytest.raises(Exception) as err:
        await verify(outcome, tmp_path)
    assert getattr(err.value, "code", None) == "xbox_install_incomplete"
    assert "exit_after_complete" in str(err.value)
