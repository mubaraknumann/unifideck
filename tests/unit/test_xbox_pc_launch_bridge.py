"""The service bridge (ported from the launch spike's ``BridgeTests``).

The game reaches ``xodus-service`` through a loopback listener that first
checks a 64-character key. A fake service on a unix socket upper-cases
what it receives. Loopback only; nothing leaves the machine.
"""
from __future__ import annotations

import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc.bridge import Bridge


def _echo_server(path: str) -> socket.socket:
    """A unix socket that upper-cases what it receives."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    conn.sendall(data.upper())

    threading.Thread(target=serve, daemon=True).start()
    return server


@pytest.fixture
def bridge(short_tmp: Path) -> Iterator[Bridge]:
    server = _echo_server(str(short_tmp / "xodus.sock"))
    b = Bridge(str(short_tmp / "xodus.sock"))
    yield b
    b.stop()
    server.close()


@pytest.fixture
def short_tmp() -> Iterator[Path]:
    """A short folder: a unix socket path must stay under 108 bytes."""
    path = Path(tempfile.mkdtemp(prefix="xpcb"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _connect(b: Bridge) -> socket.socket:
    return socket.create_connection(("127.0.0.1", b.port), timeout=5)


def test_key_is_64_hex_characters_on_loopback(bridge: Bridge) -> None:
    assert len(bridge.key) == 64
    int(bridge.key, 16)


def test_good_key_reaches_the_service(bridge: Bridge) -> None:
    with _connect(bridge) as client:
        client.sendall(bridge.key.encode() + b"hello")  # key and payload in one segment
        assert client.recv(64) == b"HELLO"
        client.sendall(b"again")
        assert client.recv(64) == b"AGAIN"


def test_key_split_across_segments(bridge: Bridge) -> None:
    with _connect(bridge) as client:
        key = bridge.key.encode()
        client.sendall(key[:10])
        time.sleep(0.05)
        client.sendall(key[10:])
        client.sendall(b"ok")
        assert client.recv(64) == b"OK"


def test_bad_key_is_dropped(bridge: Bridge) -> None:
    with _connect(bridge) as client:
        client.sendall(b"0" * 64 + b"hello")
        assert client.recv(64) == b""  # closed, nothing forwarded


def test_half_close_still_returns_the_reply(bridge: Bridge) -> None:
    with _connect(bridge) as client:
        client.sendall(bridge.key.encode() + b"last")
        client.shutdown(socket.SHUT_WR)
        assert client.recv(64) == b"LAST"


def test_stop_closes_open_connections(bridge: Bridge) -> None:
    client = _connect(bridge)
    client.sendall(bridge.key.encode() + b"x")
    assert client.recv(8) == b"X"
    bridge.stop()
    client.settimeout(3)
    assert client.recv(8) == b""
    client.close()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", bridge.port), timeout=1)
