"""Talk to the Battle.net client's own UI over the DevTools protocol.

py_modules/unifideck/launcher/proton/handlers/battlenet_cdp.py

The client's UI is a CEF page (``resources://home/``). Started with
``--remote-debugging-port`` (``Battle.net Launcher.exe`` forwards it to the
client, measured), it exposes DevTools on 127.0.0.1 only. That is the one
way found to start a non-retail version: every ``--exec`` command only
*selects* one, and starting the game binary ourselves fails at login because
the client did not initiate the launch. See ``battlenet_play``.

The port is picked per client start and recorded in the prefix, so a later
launch into an already-running client finds it again.

Stdlib only (system Python): a minimal WebSocket client, enough for one
``Runtime.evaluate`` round-trip per call.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import urllib.request
from pathlib import Path

#: Where the debugging port of the client running in a prefix is recorded.
PORT_FILE = ".unifideck-cdp-port"
_TIMEOUT_S = 5.0


class CdpError(RuntimeError):
    """The client UI could not be reached or the evaluation failed."""


def assign_port(prefix: Path | str) -> int:
    """A free loopback port for the next client start, recorded in ``prefix``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    (Path(prefix) / PORT_FILE).write_text(str(port), encoding="utf-8")
    return port


def port_for(prefix: Path | str) -> int | None:
    """The port recorded for ``prefix``, or None."""
    try:
        return int((Path(prefix) / PORT_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pages(port: int) -> list[dict[str, object]]:
    url = f"http://127.0.0.1:{port}/json"
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
            data = json.load(resp)
    except (OSError, ValueError) as exc:
        raise CdpError(f"no DevTools endpoint on port {port}: {exc}") from exc
    return data if isinstance(data, list) else []


def alive(port: int | None) -> bool:
    """Whether a client UI answers on ``port``."""
    if port is None:
        return False
    try:
        _pages(port)
    except CdpError:
        return False
    return True


def _ui_socket_url(port: int) -> str:
    for page in _pages(port):
        if page.get("type") == "page" and str(page.get("url", "")).startswith(
            "resources://home",
        ):
            return str(page["webSocketDebuggerUrl"])
    raise CdpError("the client UI page is not loaded yet")


def evaluate(port: int, expression: str) -> object:
    """Evaluate ``expression`` in the client UI and return its JSON value."""
    sock = _connect(_ui_socket_url(port))
    try:
        _send(sock, json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        while True:
            message = json.loads(_receive(sock))
            if message.get("id") == 1:
                break
    except (OSError, ValueError) as exc:
        raise CdpError(f"DevTools exchange failed: {exc}") from exc
    finally:
        sock.close()
    result = message.get("result") or {}
    if "exceptionDetails" in result:
        raise CdpError(f"evaluation threw: {str(result['exceptionDetails'])[:300]}")
    return (result.get("result") or {}).get("value")


# -- minimal WebSocket client (RFC 6455, text frames only) -----------------


def _connect(url: str) -> socket.socket:
    hostport, path = url.split("://", 1)[1].split("/", 1)
    host, port = hostport.rsplit(":", 1)
    try:
        sock = socket.create_connection((host, int(port)), timeout=_TIMEOUT_S)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode(),
        )
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(1024)
            if not chunk:
                raise CdpError("WebSocket handshake closed")
            head += chunk
    except OSError as exc:
        raise CdpError(f"cannot open the DevTools socket: {exc}") from exc
    status = head.split(b"\r\n", 1)[0]
    if b" 101 " not in status:
        sock.close()
        raise CdpError(f"WebSocket upgrade refused: {status.decode(errors='replace')}")
    return sock


def _send(sock: socket.socket, text: str) -> None:
    data = text.encode()
    mask = os.urandom(4)
    size = len(data)
    if size < 126:
        header = bytes([0x81, 0x80 | size])
    elif size < 65536:
        header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", size)
    else:
        header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", size)
    sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def _exact(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise CdpError("WebSocket closed mid-frame")
        buf += chunk
    return buf


def _receive(sock: socket.socket) -> str:
    """One complete text message (continuation frames joined)."""
    message = b""
    while True:
        first, second = _exact(sock, 2)
        size = second & 0x7F
        if size == 126:
            size = struct.unpack(">H", _exact(sock, 2))[0]
        elif size == 127:
            size = struct.unpack(">Q", _exact(sock, 8))[0]
        payload = _exact(sock, size)
        if first & 0x0F in (0x0, 0x1):
            message += payload
            if first & 0x80:
                return message.decode(errors="replace")
