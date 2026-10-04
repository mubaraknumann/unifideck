"""Tests for ``stores.itch.butlerd``: the butler daemon client.

Two layers: ``ButlerdConnection`` against an in-process JSON-RPC server, and
``ButlerDaemon`` against a fake ``butler`` executable that reproduces the
measured daemon behaviour (listen notification on stdout, a 0644 database,
``Meta.Shutdown`` exiting only once connections close).
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from unifideck.stores.itch.butlerd import ButlerDaemon, ButlerdConnection, ButlerdError


# ── ButlerdConnection against an in-process server ─────────────────────
class _Server:
    """Answers requests per a script; can push notifications/requests first."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.received: list[dict[str, Any]] = []

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while line := await reader.readline():
            msg = json.loads(line)
            self.received.append(msg)
            if "method" in msg:
                for out in await self.handler(msg):
                    writer.write((json.dumps(out) + "\n").encode())
                    await writer.drain()
        writer.close()


async def _connect(handler: Any, **kw: Any) -> tuple[ButlerdConnection, _Server, asyncio.AbstractServer]:
    server = _Server(handler)
    srv = await asyncio.start_server(server.serve, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return ButlerdConnection(reader, writer, **kw), server, srv


def _ok(msg: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


async def test_call_returns_result_and_routes_notifications() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def handler(msg: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {"jsonrpc": "2.0", "method": "Progress", "params": {"progress": 0.5}},
            _ok(msg, {"version": "v15.31.0"}),
        ]

    conn, _, srv = await _connect(handler, on_notification=lambda m, p: seen.append((m, p)))
    try:
        assert await conn.call("Version.Get") == {"version": "v15.31.0"}
        assert seen == [("Progress", {"progress": 0.5})]
    finally:
        await conn.close()
        srv.close()


async def test_out_of_order_answers_reach_the_right_caller() -> None:
    pending: list[dict[str, Any]] = []

    async def handler(msg: dict[str, Any]) -> list[dict[str, Any]]:
        pending.append(msg)
        if len(pending) < 2:
            return []
        first, second = pending
        return [_ok(second, {"n": 2}), _ok(first, {"n": 1})]

    conn, _, srv = await _connect(handler)
    try:
        a, b = await asyncio.gather(conn.call("A"), conn.call("B"))
        assert (a, b) == ({"n": 1}, {"n": 2})
    finally:
        await conn.close()
        srv.close()


async def test_error_answer_raises_with_itch_api_status() -> None:
    async def handler(msg: dict[str, Any]) -> list[dict[str, Any]]:
        # The shape butler 15.31 returned for a bad key.
        return [{"jsonrpc": "2.0", "id": msg["id"], "error": {
            "code": 12000, "message": "itch.io API error (403): /profile: invalid key",
            "data": {"apiError": {"statusCode": 403, "messages": ["invalid key"]}},
        }}]

    conn, _, srv = await _connect(handler)
    try:
        with pytest.raises(ButlerdError) as err:
            await conn.call("Profile.LoginWithAPIKey", {"apiKey": "x"})
        assert err.value.code == 12000
        assert err.value.api_status == 403
    finally:
        await conn.close()
        srv.close()


async def test_server_request_is_answered_or_refused() -> None:
    async def handler(msg: dict[str, Any]) -> list[dict[str, Any]]:
        if msg.get("method") == "Go":
            return [
                {"jsonrpc": "2.0", "id": 900, "method": "PickUpload", "params": {"uploads": []}},
                {"jsonrpc": "2.0", "id": 901, "method": "Unknown.Thing", "params": {}},
            ]
        return []

    def on_request(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        return {"index": 0} if method == "PickUpload" else None

    conn, server, srv = await _connect(handler, on_request=on_request)
    try:
        call = asyncio.create_task(conn.call("Go", timeout=0.5))
        for _ in range(50):
            replies = [m for m in server.received if m.get("id") in (900, 901)]
            if len(replies) == 2:
                break
            await asyncio.sleep(0.02)
        by_id = {m["id"]: m for m in replies}
        assert by_id[900]["result"] == {"index": 0}
        assert by_id[901]["error"]["code"] == -32601
        call.cancel()
    finally:
        await conn.close()
        srv.close()


async def test_dropped_connection_fails_waiting_calls_instead_of_hanging() -> None:
    async def handler(msg: dict[str, Any]) -> list[dict[str, Any]]:
        raise ConnectionResetError  # the server side dies mid-request

    conn, _, srv = await _connect(handler)
    try:
        with pytest.raises(ButlerdError):
            await conn.call("Fetch.Caves", timeout=5)
        assert conn.closed
    finally:
        await conn.close()
        srv.close()


# ── ButlerDaemon against a fake butler executable ─────────────────────
_FAKE_BUTLER = textwrap.dedent('''\
    #!{python}
    import asyncio, json, os, sys
    args = sys.argv[1:]
    with open(os.environ["FAKE_BUTLER_ARGV"], "w") as fh:
        json.dump(args, fh)
    db = args[args.index("--dbpath") + 1]
    for suffix in ("", "-wal", "-shm"):
        with open(db + suffix, "w") as fh:
            fh.write("x")
        os.chmod(db + suffix, 0o644)   # butler creates its database world-readable
    conns = set()
    stopping = asyncio.Event()
    async def serve(reader, writer):
        conns.add(writer)
        try:
            while line := await reader.readline():
                msg = json.loads(line)
                m = msg["method"]
                if m == "Meta.Authenticate":
                    ok = msg["params"]["secret"] == "s3cret"
                    out = {{"result": {{"ok": ok}}}} if ok else {{"error": {{"code": -1, "message": "bad secret"}}}}
                elif m == "Profile.List":
                    out = {{"result": {{"profiles": [{{"id": 7}}]}}}}
                elif m == "Meta.Shutdown":
                    stopping.set()
                    out = {{"result": {{}}}}
                else:
                    out = {{"error": {{"code": -32601, "message": m}}}}
                writer.write((json.dumps({{"jsonrpc": "2.0", "id": msg["id"], **out}}) + "\\n").encode())
                await writer.drain()
        finally:
            conns.discard(writer)
    async def main():
        srv = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        print(json.dumps({{"type": "log", "level": "info", "message": "creating new DB"}}), flush=True)
        print(json.dumps({{"type": "butlerd/listen-notification", "secret": "s3cret",
                          "tcp": {{"address": f"127.0.0.1:{{port}}"}}}}), flush=True)
        await stopping.wait()
        while conns:            # butler waits for open connections before exiting
            await asyncio.sleep(0.05)
        srv.close()
    asyncio.run(main())
''')


@pytest.fixture()
def fake_butler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    exe = tmp_path / "butler"
    exe.write_text(_FAKE_BUTLER.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_BUTLER_ARGV", str(argv_file))
    return exe, argv_file


async def test_daemon_starts_with_keep_alive_and_destiny_pid(
    fake_butler: tuple[Path, Path], tmp_path: Path,
) -> None:
    exe, argv_file = fake_butler
    daemon = ButlerDaemon(str(exe), str(tmp_path / "db" / "butler.db"))
    try:
        assert (await daemon.call("Profile.List"))["profiles"] == [{"id": 7}]
        argv = json.loads(argv_file.read_text())
        # Without --keep-alive butlerd serves one client and exits when it
        # disconnects (measured on 15.31.0), so the flag is load-bearing.
        assert "--keep-alive" in argv
        assert argv[argv.index("--destiny-pid") + 1] == str(os.getpid())
        assert argv[:4] == ["daemon", "--json", "--transport", "tcp"]
    finally:
        await daemon.shutdown()


async def test_database_and_its_wal_files_are_hardened_to_0600(
    fake_butler: tuple[Path, Path], tmp_path: Path,
) -> None:
    exe, _ = fake_butler
    db = tmp_path / "db" / "butler.db"
    daemon = ButlerDaemon(str(exe), str(db))
    try:
        await daemon.call("Profile.List")
        for suffix in ("", "-wal", "-shm"):
            mode = Path(str(db) + suffix).stat().st_mode & 0o777
            assert mode == 0o600, (suffix, oct(mode))
    finally:
        await daemon.shutdown()


async def test_several_connections_share_one_daemon(
    fake_butler: tuple[Path, Path], tmp_path: Path,
) -> None:
    exe, _ = fake_butler
    daemon = ButlerDaemon(str(exe), str(tmp_path / "butler.db"))
    try:
        dedicated = await daemon.connect()
        assert (await dedicated.call("Profile.List"))["profiles"]
        assert (await daemon.call("Profile.List"))["profiles"]
        await dedicated.close()
    finally:
        await daemon.shutdown()


async def test_shutdown_stops_the_process_gracefully(
    fake_butler: tuple[Path, Path], tmp_path: Path,
) -> None:
    exe, _ = fake_butler
    daemon = ButlerDaemon(str(exe), str(tmp_path / "butler.db"))
    await daemon.call("Profile.List")
    proc = daemon._proc
    assert proc is not None
    await daemon.shutdown()
    assert proc.returncode == 0  # exited on Meta.Shutdown, not killed


async def test_missing_binary_raises_instead_of_spawning(tmp_path: Path) -> None:
    daemon = ButlerDaemon(None, str(tmp_path / "butler.db"))
    assert daemon.available is False
    with pytest.raises(ButlerdError, match="butler_not_found"):
        await daemon.call("Profile.List")


def test_has_database_reflects_the_file(tmp_path: Path) -> None:
    db = tmp_path / "butler.db"
    daemon = ButlerDaemon("/nonexistent/butler", str(db))
    assert daemon.has_database is False
    db.write_text("")
    assert daemon.has_database is True
