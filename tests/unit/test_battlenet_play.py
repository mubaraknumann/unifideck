"""Battle.net: start a non-retail version by pressing the client's own Play.

tests/unit/test_battlenet_play.py

Measured on-device: ``--exec`` only selects a non-retail version, and a game
binary we start ourselves never logs in. What works is ``launch_uid`` to
select the exact version, then the client's Play button over DevTools. These
pin that flow, the one-shot choice the launch popup hands the launcher, and
the minimal DevTools client it uses.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import struct
import threading
from pathlib import Path
from typing import Any

import pytest

from unifideck.launcher.proton.handlers import battlenet_cdp as cdp
from unifideck.launcher.proton.handlers import battlenet_play as play
from unifideck.stores.battlenet import launch_choice

# --------------------------------------------------------------------------
# the popup's one-shot choice
# --------------------------------------------------------------------------


def test_a_choice_is_used_once() -> None:
    launch_choice.record("wow", "wow_classic_era")
    assert launch_choice.consume("wow") == "wow_classic_era"
    assert launch_choice.consume("wow") is None


def test_a_stale_choice_never_hijacks_a_later_launch() -> None:
    launch_choice.record("wow", "wow_classic_era", now=1000.0)
    assert launch_choice.consume("wow", now=1000.0 + launch_choice.FRESH_S + 1) is None


def test_choices_are_kept_per_game() -> None:
    launch_choice.record("wow", launch_choice.CLIENT)
    launch_choice.record("w3", "w3")
    assert launch_choice.consume("w3") == "w3"
    assert launch_choice.consume("wow") == launch_choice.CLIENT


def test_an_unreadable_choice_file_reads_as_no_choice() -> None:
    path = launch_choice.choice_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert launch_choice.consume("wow") is None


# --------------------------------------------------------------------------
# DevTools client, against a real local WebSocket server
# --------------------------------------------------------------------------


def _serve_once(server: socket.socket, reply: dict[str, Any]) -> None:
    conn, _ = server.accept()
    with conn:
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(1024)
        key = next(
            line.split(b":", 1)[1].strip()
            for line in head.split(b"\r\n") if line.lower().startswith(b"sec-websocket-key")
        )
        accept = base64.b64encode(
            hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11", usedforsecurity=False).digest(),
        )
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        _first, second = conn.recv(2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack(">H", conn.recv(2))[0]
        mask = conn.recv(4)
        data = b""
        while len(data) < size:
            data += conn.recv(size - len(data))
        request = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
        assert request["method"] == "Runtime.evaluate"
        # An unrelated event first, then the answer split over two frames.
        for message in (json.dumps({"method": "Runtime.consoleAPICalled"}),
                        json.dumps({"id": request["id"], **reply})):
            payload = message.encode()
            half = len(payload) // 2
            conn.sendall(bytes([0x01, half]) + payload[:half] if half < 126 else b"")
            rest = payload[half:]
            conn.sendall(bytes([0x80, 126]) + struct.pack(">H", len(rest)) + rest
                         if len(rest) >= 126 else bytes([0x80, len(rest)]) + rest)


@pytest.fixture
def ws_server(monkeypatch: pytest.MonkeyPatch):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    monkeypatch.setattr(cdp, "_ui_socket_url", lambda p: f"ws://127.0.0.1:{port}/devtools/page/x")
    yield server
    server.close()


def test_evaluate_returns_the_value(ws_server: socket.socket) -> None:
    thread = threading.Thread(
        target=_serve_once, args=(ws_server, {"result": {"result": {"value": True}}}),
    )
    thread.start()
    assert cdp.evaluate(1, "1") is True
    thread.join(5)


def test_evaluate_raises_when_the_page_throws(ws_server: socket.socket) -> None:
    thread = threading.Thread(
        target=_serve_once, args=(ws_server, {"result": {"exceptionDetails": {"text": "x"}}}),
    )
    thread.start()
    with pytest.raises(cdp.CdpError):
        cdp.evaluate(1, "boom()")
    thread.join(5)


def test_a_port_is_recorded_per_prefix(tmp_path: Path) -> None:
    port = cdp.assign_port(tmp_path)
    assert 0 < port < 65536
    assert cdp.port_for(tmp_path) == port
    assert cdp.alive(None) is False


# --------------------------------------------------------------------------
# press_play
# --------------------------------------------------------------------------


class _Plan:
    def __init__(self, prefix: Path) -> None:
        self.prefix_path = prefix


class _FailedError(Exception):
    pass


def _fail(plan: Any, key: str, message: str, **context: Any) -> _FailedError:
    return _FailedError(message)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """A fake client: its logs, its UI, and the game it starts."""
    state: dict[str, Any] = {
        "logs": [], "clicked": False, "exec": [], "alive": True, "game": "4242",
    }
    monkeypatch.setattr(play, "_POLL", 0.0)
    for limit in ("UI_READY_TIMEOUT", "SELECT_TIMEOUT", "CLICK_TIMEOUT", "LAUNCH_TIMEOUT"):
        monkeypatch.setattr(play, limit, 0.2)
    monkeypatch.setattr(play.cdp, "port_for", lambda prefix: 9333)
    monkeypatch.setattr(play.cdp, "alive", lambda port: state["alive"])
    monkeypatch.setattr(play, "count_in_logs",
                        lambda prefix, needle: sum(needle in line for line in state["logs"]))

    def fake_eval(port: int, expression: str) -> object:
        if expression == play.CLICK_PLAY_JS:
            state["clicked"] = True
            if state.get("launches", True):
                state["logs"].append("LaunchBinary: uid=wow_classic_era selectedRegion=US")
            return True
        return expression == play.PLAY_BUTTON_JS or "World of Warcraft Classic"

    async def fake_exec(plan: Any, exe: Path, command: str) -> None:
        state["exec"].append(command)
        if state.get("selects", True):
            state["logs"].append(f"Selecting game by uid. uid={command.rsplit(maxsplit=1)[-1]} x=1")
        await asyncio.sleep(3600)

    async def fake_wait_game(prefix: Any, before: set, limit: float) -> str | None:
        return state["game"]

    monkeypatch.setattr(play.cdp, "evaluate", fake_eval)
    monkeypatch.setattr(play.bnet_exec, "issue_exec", fake_exec)
    monkeypatch.setattr(play.watch, "game_pids", lambda prefix: set())
    monkeypatch.setattr(play.watch, "wait_for_game", fake_wait_game)
    monkeypatch.setattr(play.watch, "stop_client", lambda prefix: 0)
    monkeypatch.setattr(play.asyncio, "sleep", _no_sleep)
    state["plan"] = _Plan(tmp_path)
    return state


_real_sleep = asyncio.sleep


async def _no_sleep(delay: float, *a: Any) -> None:
    await _real_sleep(0)


async def _no_restart() -> None:
    raise AssertionError("a client with DevTools must not be restarted")


def _press(state: dict[str, Any], restart=_no_restart) -> tuple[str, set[str]]:
    return asyncio.run(play.press_play(
        state["plan"], Path("Battle.net.exe"), "wow_classic_era", fail=_fail, restart=restart,
    ))


def test_selects_the_exact_version_then_presses_play(client: dict[str, Any]) -> None:
    pid, _ = _press(client)
    assert pid == "4242"
    assert client["exec"] == ["launch_uid wow_classic_era"]
    assert client["clicked"] is True


def test_a_client_without_devtools_is_restarted_once(client: dict[str, Any]) -> None:
    client["alive"] = False
    restarted: list[bool] = []

    async def restart() -> None:
        restarted.append(True)
        client["alive"] = True

    pid, _ = _press(client, restart)
    assert (pid, restarted) == ("4242", [True])


def test_a_selection_the_client_never_logs_fails_loudly(client: dict[str, Any]) -> None:
    client["selects"] = False
    with pytest.raises(_FailedError, match="did not select"):
        _press(client)
    assert client["clicked"] is False


def test_play_that_launches_nothing_fails_loudly(client: dict[str, Any]) -> None:
    client["launches"] = False
    with pytest.raises(_FailedError, match="did not launch"):
        _press(client)


def test_an_earlier_launch_in_the_log_is_no_proof(client: dict[str, Any]) -> None:
    """One client session logs many launches; only a new one counts."""
    client["logs"].append("LaunchBinary: uid=wow_classic_era selectedRegion=US")
    client["launches"] = False
    with pytest.raises(_FailedError, match="did not launch"):
        _press(client)


def test_a_launch_with_no_game_process_fails_loudly(client: dict[str, Any]) -> None:
    client["game"] = None
    with pytest.raises(_FailedError, match="no game process"):
        _press(client)


def test_count_in_logs_counts_every_occurrence(tmp_path: Path) -> None:
    logs = tmp_path / "drive_c" / play._LOG_DIR
    logs.mkdir(parents=True)
    (logs / "battle.net-1.log").write_text("LaunchBinary: uid=a x\nLaunchBinary: uid=a y\n")
    (logs / "battle.net-2.log").write_text("LaunchBinary: uid=a z\n")
    assert play.count_in_logs(tmp_path, "LaunchBinary: uid=a ") == 3
    assert play.count_in_logs(tmp_path / "missing", "x") == 0
