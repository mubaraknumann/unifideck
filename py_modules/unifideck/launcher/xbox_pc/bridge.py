"""launcher/xbox_pc/bridge.py — loopback TCP to ``xodus-service``'s unix socket.

Wine code cannot reach a host unix socket reliably, so the runtime's
``xgameruntime`` connects to ``127.0.0.1:XODUS_BRIDGE_PORT``, sends
``XODUS_BRIDGE_KEY`` (64 hex characters) and then speaks the service
protocol. A client that sends the wrong key, or no key within
:data:`BRIDGE_AUTH_TIMEOUT_S`, is dropped and nothing reaches the service.
Half-close is passed on, so a request that ends with ``SHUT_WR`` still
gets its reply.

Mirrors Nexus's ``startXodusBridge`` (``xodus-service.ts``). Ported from the
spike unchanged. It runs on daemon threads because the game talks to it
while the launcher's event loop waits on the game.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import hmac
import secrets
import selectors
import socket
import threading

BRIDGE_AUTH_TIMEOUT_S = 10.0


class Bridge:
    """A key-protected loopback listener piping each client to ``socket_path``."""

    def __init__(self, socket_path: str) -> None:
        """Bind a random loopback port and start accepting."""
        self.socket_path = socket_path
        self.key = secrets.token_hex(32)
        self._expected = self.key.encode("ascii")
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self.port: int = self._server.getsockname()[1]
        self._open: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        threading.Thread(target=self._accept_loop, name="xbox-bridge", daemon=True).start()

    def _track(self, sock: socket.socket) -> None:
        with self._lock:
            self._open.add(sock)

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            self._track(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _read_key(self, client: socket.socket) -> bytes | None:
        """Everything the client sent up to and past the key, or None."""
        client.settimeout(BRIDGE_AUTH_TIMEOUT_S)
        received = b""
        while len(received) < len(self._expected):
            chunk = client.recv(4096)
            if not chunk:
                return None
            received += chunk
        if not hmac.compare_digest(received[: len(self._expected)], self._expected):
            return None
        return received

    def _serve(self, client: socket.socket) -> None:
        upstream = None
        try:
            received = self._read_key(client)
            if received is None:
                return
            client.settimeout(None)
            upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._track(upstream)
            upstream.connect(self.socket_path)
            rest = received[len(self._expected):]
            if rest:
                upstream.sendall(rest)
            self._pump(client, upstream)
        except OSError:
            pass
        finally:
            for sock in (client, upstream):
                if sock is not None:
                    with self._lock:
                        self._open.discard(sock)
                    sock.close()

    @staticmethod
    def _pump(a: socket.socket, b: socket.socket) -> None:
        peer = {a: b, b: a}
        live = {a, b}
        with selectors.DefaultSelector() as sel:
            for sock in live:
                sel.register(sock, selectors.EVENT_READ)
            while live:
                for key, _ in sel.select():
                    src = key.fileobj
                    assert isinstance(src, socket.socket)
                    data = src.recv(65536)
                    if data:
                        peer[src].sendall(data)
                        continue
                    # Half-close: pass the EOF on and keep the other way open.
                    sel.unregister(src)
                    live.discard(src)
                    try:
                        peer[src].shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

    def stop(self) -> None:
        """Stop listening and close every open connection."""
        self._stopped.set()
        # close() alone leaves a socket another thread is accept()ing on listening.
        try:
            self._server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._server.close()
        with self._lock:
            for sock in list(self._open):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            self._open.clear()
