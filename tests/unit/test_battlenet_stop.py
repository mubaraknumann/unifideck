"""A stop from Steam must end the Battle.net game, not only the client.

Measured 2026-09-24 on Warcraft III: ``SteamClient.Apps.TerminateApp`` killed
the launcher, umu-run and the client, and ``Warcraft III.exe`` kept running
in a pressure-vessel container that had been re-parented to
``systemd --user``. The launcher's teardown stops only the client's own
images, so nothing it owned reached the game.
"""
from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.proton.handlers import battlenet as handler
from unifideck.launcher.proton.handlers import battlenet_watch as watch
from unifideck.launcher.proton.handlers import wrapper_clients as wc

PREFIX = "/prefixes/battlenet/w3"
_CLIENT = ("163847", "C:\\Program Files (x86)\\Battle.net\\Battle.net.exe\x00--from-launcher\x00")
_AGENT = ("165930", "C:/ProgramData/Battle.net/Agent/Agent.9775/Agent.exe\x00")
_SERVICES = ("163645", "C:\\windows\\system32\\services.exe\x00")
_GAME = ("174432", "C:\\Program Files (x86)\\Warcraft III\\_retail_\\x86_64\\Warcraft III.exe\x00-launch\x00")
_OLD_GAME = ("120001", "C:\\Program Files (x86)\\Diablo III\\Diablo III64.exe\x00")


def _proc(monkeypatch: pytest.MonkeyPatch, entries: list[tuple[str, str]]) -> list[tuple[int, int]]:
    """A /proc stand-in for one prefix, plus a spy on the signals sent."""
    by_pid = dict(entries)
    monkeypatch.setattr(wc, "pids", lambda: list(by_pid))
    monkeypatch.setattr(
        wc, "proc_field",
        lambda pid, field: by_pid.get(pid, "") if field == "cmdline" else f"WINEPREFIX={PREFIX}\x00",
    )
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(wc.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def test_stop_game_signals_only_the_game_this_launch_started(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _proc(monkeypatch, [_CLIENT, _AGENT, _SERVICES, _GAME, _OLD_GAME])
    assert watch.stop_game(PREFIX, _GAME[0], before={_OLD_GAME[0]}, timeout=0.0) == 1
    assert (int(_GAME[0]), signal.SIGTERM) in sent
    signalled = {pid for pid, _ in sent}
    # The client and the Agent are the client teardown's business, and a
    # game that predates this launch is never ours.
    assert signalled == {int(_GAME[0])}


def test_stop_game_follows_a_handoff_when_the_first_pid_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _proc(monkeypatch, [_CLIENT, _GAME])
    assert watch.stop_game(PREFIX, "13990", before=set(), timeout=0.0) == 1
    assert {pid for pid, _ in sent} == {int(_GAME[0])}


def test_stop_game_is_a_noop_without_a_game(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _proc(monkeypatch, [_CLIENT, _AGENT, _SERVICES])
    assert watch.stop_game(PREFIX, None, before=set(), timeout=0.0) == 0
    assert sent == []


class _Plan:
    prefix_path = Path(PREFIX)


def _arm_teardown(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> None:
    async def _capture(_p: Any) -> None:
        order.append("capture")

    monkeypatch.setattr(handler.watch, "stop_client", lambda p: order.append("client") or 0)
    monkeypatch.setattr(handler.session, "capture_from", _capture)
    monkeypatch.setattr(
        handler.watch, "stop_game",
        lambda p, pid, before: order.append(f"game:{pid}") or 1,
    )


def test_a_stop_ends_the_game_before_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    _arm_teardown(monkeypatch, order)
    playing = asyncio.Event()

    async def _forever(*_a: Any, **_k: Any) -> None:
        playing.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(handler.watch, "wait_for_exit", _forever)

    async def _run() -> None:
        task = asyncio.create_task(handler._await_session_end(_Plan(), _GAME[0], set()))
        await playing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert order == [f"game:{_GAME[0]}", "client", "capture"]


def test_a_game_that_exits_by_itself_is_not_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    order: list[str] = []
    _arm_teardown(monkeypatch, order)

    async def _exits(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(handler.watch, "wait_for_exit", _exits)
    asyncio.run(handler._await_session_end(_Plan(), _GAME[0], set()))
    assert order == ["client", "capture"]
