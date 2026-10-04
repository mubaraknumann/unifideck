"""The butlerd client: itch.io's ``butler daemon`` over JSON-RPC 2.0 / TCP.

butler is the CLI the itch.io app itself is built on. For anything that
touches a user's *owned* games (library, installs, updates) it only works as
a daemon: the one-shot ``butler fetch`` covers butler-pushed channels only, and
authenticates through OAuth tokens that itch.io refuses for downloads. So this
store's "CLI" is a long-lived subprocess, and this module is the only place
that knows it.

Everything below was measured against butler 15.31.0 on a Steam Deck
(2026-09-23), and several of the facts are not in butler's docs:

* **``--keep-alive`` is mandatory.** Without it the daemon serves exactly ONE
  TCP client and exits when that client disconnects; a second connection is
  accepted and then never answered. Each install runs on its own connection
  (so its ``Progress`` notifications are unambiguous), so we need several.
* **``--destiny-pid``** ties the daemon to the plugin: it exited one second
  after its parent was SIGKILLed. That is the guarantee against an orphaned
  daemon; ``shutdown`` is only the polite path.
* **The pipes must be drained.** The daemon writes JSON log lines to stdout
  for its whole life; an undrained pipe fills and wedges it.
* **``butler.db`` holds the API key and is created 0644**, with ``-wal`` and
  ``-shm`` siblings. All three are tightened to 0600 after every start.
* Responses are single lines and can be large (a library page), so the
  stream limit is raised well above asyncio's 64 KiB default.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from unifideck.core.binaries import clean_cli_env
from unifideck.stores.shared.cli_credentials import harden_cli_credential_file

logger = logging.getLogger(__name__)

#: Seconds to wait for the daemon's listen notification after spawning it.
_LISTEN_TIMEOUT_S = 20.0
#: One JSON-RPC message is one line; a page of owned keys is ~1 KB per game.
_STREAM_LIMIT = 64 * 1024 * 1024
_DB_SUFFIXES = ("", "-wal", "-shm")

NotificationHandler = Callable[[str, dict[str, Any]], None]
RequestHandler = Callable[[str, dict[str, Any]], dict[str, Any] | None]


class ButlerdError(Exception):
    """A JSON-RPC error answer from butlerd, or a dead transport.

    ``api_status`` carries itch.io's HTTP status when butler's error came from
    the itch.io API (butler code 12000). A revoked key answers 403 there.
    """

    def __init__(
        self, message: str, *, code: int | None = None,
        api_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.api_status = api_status

    @classmethod
    def from_payload(cls, error: dict[str, Any]) -> ButlerdError:
        """Build from the ``error`` member of a JSON-RPC response."""
        data = error.get("data") or {}
        api = data.get("apiError") if isinstance(data, dict) else None
        status = api.get("statusCode") if isinstance(api, dict) else None
        return cls(
            str(error.get("message") or "butlerd error"),
            code=error.get("code"),
            api_status=status if isinstance(status, int) else None,
        )


class ButlerdConnection:
    """One authenticated TCP connection: request/response multiplexing.

    ``on_notification`` receives every notification (``Progress``, ``Log``,
    ``TaskStarted`` …). ``on_request`` answers server→client requests; a
    ``None`` answer is sent back as "method not handled", which butler treats
    as a refusal rather than hanging.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_notification = on_notification
        self._on_request = on_request
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._pump = asyncio.create_task(self._read_loop(), name="butlerd-conn")

    @property
    def closed(self) -> bool:
        """True once the read loop has ended (EOF, error or ``close``)."""
        return self._pump.done()

    async def call(
        self, method: str, params: dict[str, Any] | None = None,
        *, timeout: float | None = 120.0,  # noqa: ASYNC109 - per-call RPC deadline, None = the caller watches progress instead
    ) -> dict[str, Any]:
        """Send a request and return its ``result``; raise ``ButlerdError``."""
        if self.closed:
            raise ButlerdError("butlerd connection closed")
        self._next_id += 1
        req_id = self._next_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send({
                "jsonrpc": "2.0", "id": req_id,
                "method": method, "params": params or {},
            })
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(req_id, None)

    async def close(self) -> None:
        """Close the socket and fail every call still waiting on it."""
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        self._pump.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._pump
        self._fail_pending("butlerd connection closed")

    async def _send(self, message: dict[str, Any]) -> None:
        self._writer.write((json.dumps(message) + "\n").encode())
        await self._writer.drain()

    async def _read_loop(self) -> None:
        try:
            while line := await self._reader.readline():
                await self._dispatch(json.loads(line))
        except (ConnectionError, ValueError, asyncio.IncompleteReadError) as e:
            logger.warning("[butlerd] connection lost: %s", e)
        finally:
            self._fail_pending("butlerd connection lost")

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.get(msg["id"])
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(ButlerdError.from_payload(msg["error"]))
                else:
                    fut.set_result(msg.get("result") or {})
            return
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}
        if "id" in msg:
            await self._answer(msg["id"], method, params)
        elif self._on_notification is not None:
            self._on_notification(method, params)

    async def _answer(self, req_id: Any, method: str, params: dict[str, Any]) -> None:
        answer = self._on_request(method, params) if self._on_request else None
        logger.info("[butlerd] server request %s -> %s",
                    method, "answered" if answer is not None else "refused")
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id}
        if answer is None:
            reply["error"] = {"code": -32601, "message": f"{method} not handled"}
        else:
            reply["result"] = answer
        await self._send(reply)

    def _fail_pending(self, reason: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ButlerdError(reason))


class ButlerDaemon:
    """Owns the ``butler daemon`` process and a shared request connection.

    Started lazily on first use and restarted if it died. ``call`` uses one
    shared connection for short requests; ``connect`` opens a dedicated one
    (installs, whose notifications must not interleave with anything else).
    """

    def __init__(self, cli_path: str | None, db_path: str, *, store: str = "itch") -> None:
        self._cli_path = cli_path
        self._db_path = str(Path(db_path).expanduser())
        self._store = store
        self._proc: asyncio.subprocess.Process | None = None
        self._listen: dict[str, Any] | None = None
        self._shared: ButlerdConnection | None = None
        self._drain: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        """Whether a butler binary was resolved at all."""
        return bool(self._cli_path)

    @property
    def has_database(self) -> bool:
        """Whether butler has ever stored anything (a profile lives in it).

        Lets a status check answer "not signed in" for a user who never
        touched itch.io without spawning a daemon to say so.
        """
        return Path(self._db_path).is_file()

    async def call(
        self, method: str, params: dict[str, Any] | None = None,
        *, timeout: float | None = 120.0,  # noqa: ASYNC109 - per-call RPC deadline
    ) -> dict[str, Any]:
        """One request on the shared connection."""
        async with self._lock:
            await self._ensure_started()
            if self._shared is None or self._shared.closed:
                self._shared = await self._open()
            conn = self._shared
        return await conn.call(method, params, timeout=timeout)

    async def connect(
        self, *,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
    ) -> ButlerdConnection:
        """A fresh authenticated connection the caller must ``close``."""
        async with self._lock:
            await self._ensure_started()
        return await self._open(on_notification=on_notification, on_request=on_request)

    async def shutdown(self) -> None:
        """``Meta.Shutdown``, close our connections, wait, then kill if needed."""
        async with self._lock:
            proc, self._proc = self._proc, None
            shared, self._shared = self._shared, None
            if proc is None or proc.returncode is not None:
                return
            if shared is not None and not shared.closed:
                with contextlib.suppress(ButlerdError, TimeoutError):
                    await shared.call("Meta.Shutdown", timeout=5)
                await shared.close()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                logger.warning("[butlerd] daemon ignored Meta.Shutdown; killing")
                proc.kill()
                await proc.wait()
            if self._drain is not None:
                self._drain.cancel()
            logger.info("[butlerd] daemon stopped")

    async def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        if not self._cli_path:
            raise ButlerdError("butler_not_found")
        self._shared = None
        self._proc = await self._spawn(self._cli_path)
        self._listen = await asyncio.wait_for(self._await_listen(), _LISTEN_TIMEOUT_S)
        self._drain = asyncio.create_task(self._drain_output(), name="butlerd-log")
        self._harden_db()
        logger.info("[butlerd] daemon pid %s listening on %s",
                    self._proc.pid, self._listen["tcp"]["address"])

    async def _spawn(self, cli_path: str) -> asyncio.subprocess.Process:
        """Start ``butler daemon``; see the module docstring for each flag."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        return await asyncio.create_subprocess_exec(
            cli_path, "daemon", "--json", "--transport", "tcp",
            "--keep-alive", "--dbpath", self._db_path,
            "--destiny-pid", str(os.getpid()),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=clean_cli_env(),
            limit=_STREAM_LIMIT,
        )

    async def _await_listen(self) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        while line := await self._proc.stdout.readline():
            with contextlib.suppress(ValueError):
                msg = json.loads(line)
                if isinstance(msg, dict) and msg.get("type") == "butlerd/listen-notification":
                    return msg
        raise ButlerdError(f"butler daemon exited early (rc={await self._proc.wait()})")

    async def _drain_output(self) -> None:
        """Forward the daemon's log lines; never let the pipe fill."""
        assert self._proc is not None and self._proc.stdout is not None
        while line := await self._proc.stdout.readline():
            text = line.decode(errors="replace").strip()
            level = logging.DEBUG
            with contextlib.suppress(ValueError):
                msg = json.loads(text)
                text = str(msg.get("message", text))
                if msg.get("level") in ("warning", "error"):
                    level = logging.WARNING
            logger.log(level, "[butlerd] %s", text)

    async def _open(self, **handlers: Any) -> ButlerdConnection:
        assert self._listen is not None
        host, port = str(self._listen["tcp"]["address"]).rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port), limit=_STREAM_LIMIT)
        conn = ButlerdConnection(reader, writer, **handlers)
        await conn.call("Meta.Authenticate", {"secret": self._listen["secret"]}, timeout=15)
        return conn

    def _harden_db(self) -> None:
        for suffix in _DB_SUFFIXES:
            harden_cli_credential_file(self._db_path + suffix, self._store)

