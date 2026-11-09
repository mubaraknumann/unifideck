"""``run_drained_cli``: the shared drain, kill and reap envelope.

The kill must come before anything else is awaited, on a stall and on any
exception, a cancel included. Cancelling the task only unwinds our
coroutine; the CLI tree would keep running and keep the store's locks.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from unifideck.stores.shared import cli_install_helpers as helpers
from unifideck.stores.shared.cli_install_helpers import (
    DrainedRun,
    TailRingBuffer,
    run_drained_cli,
)


class _Stdout:
    """Yields the given lines, then EOF, or raises ``exc`` on the first read."""

    def __init__(self, lines: list[bytes], *, exc: BaseException | None = None,
                 hang: bool = False) -> None:
        self._lines = list(lines)
        self._exc = exc
        self._hang = hang

    async def readline(self) -> bytes:
        if self._exc is not None:
            raise self._exc
        if self._hang:
            await asyncio.sleep(3600)
        return self._lines.pop(0) if self._lines else b""


class _Proc:
    def __init__(self, stdout: _Stdout, rc: int = 0) -> None:
        self.stdout = stdout
        self.returncode: int | None = None
        self.pid = 4242
        self._rc = rc

    async def wait(self) -> int:
        self.returncode = self._rc
        return self._rc


@pytest.fixture
def kills(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_terminate(proc: Any, log_prefix: str, **_kw: Any) -> None:
        calls.append(log_prefix)

    monkeypatch.setattr(helpers, "terminate_process_tree", fake_terminate)
    return calls


async def _collect(line: str, game_id: str, cb: list[str]) -> None:
    cb.append(line)


async def test_clean_run_returns_rc_and_tail(kills: list[str]) -> None:
    tail = TailRingBuffer()
    tail.append("last words")
    seen: list[str] = []
    proc = _Proc(_Stdout([b"one\n", b"two\n"]), rc=3)
    out = await run_drained_cli(
        proc, "G", seen, _collect, tail, log_prefix="[t]", wait_s=5,
    )
    assert out == DrainedRun(rc=3, tail="last words", stalled=False)
    assert seen == ["one", "two"]
    assert kills == []


async def test_stall_kills_and_returns_minus_one(kills: list[str]) -> None:
    tail = TailRingBuffer()
    tail.append("downloading")
    out = await run_drained_cli(
        _Proc(_Stdout([], hang=True)), "G", [], _collect, tail,
        log_prefix="[t]", wait_s=5, stall_s=0.05,
    )
    assert out.rc == -1 and out.stalled
    assert out.tail.startswith("stalled: no output") and out.tail.endswith("downloading")
    assert kills == ["[t]"]


async def test_cancel_kills_before_reraising(kills: list[str]) -> None:
    proc = _Proc(_Stdout([], exc=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await run_drained_cli(
            proc, "G", [], _collect, TailRingBuffer(), log_prefix="[t]", wait_s=5,
        )
    assert kills == ["[t]"]


async def test_handler_error_kills_before_reraising(kills: list[str]) -> None:
    async def boom(_line: str, _gid: str, _cb: Any) -> None:
        raise ValueError("bad line")

    with pytest.raises(ValueError):
        await run_drained_cli(
            _Proc(_Stdout([b"x\n"])), "G", None, boom, TailRingBuffer(),
            log_prefix="[t]", wait_s=5,
        )
    assert kills == ["[t]"]


async def test_cancel_during_the_post_eof_wait_kills_before_reraising(kills: list[str]) -> None:
    class _HungExit(_Proc):
        async def wait(self) -> int:
            await asyncio.sleep(3600)
            return 0

    task = asyncio.ensure_future(run_drained_cli(
        _HungExit(_Stdout([b"done\n"])), "G", [], _collect, TailRingBuffer(),
        log_prefix="[t]", wait_s=60,
    ))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kills == ["[t]"]
