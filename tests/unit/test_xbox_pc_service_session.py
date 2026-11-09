"""``xodus-service`` per launch: ready, early exit, cancel during start-up.

The fake service is a Python script that, like the real one, binds
``$XDG_RUNTIME_DIR/xodus.sock`` and takes no arguments. The session folder
lives under a short ``XDG_RUNTIME_DIR`` so the socket path fits.
"""
from __future__ import annotations

import asyncio
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from unifideck.launcher.xbox_pc.errors import XboxPcLaunchError
from unifideck.launcher.xbox_pc.output import OutputSink
from unifideck.launcher.xbox_pc.service_session import ServiceSession, sessions_dir

PID = "9PK087LNGJC5"

_SERVES = """
import os, signal, socket, sys
sys.stdout.write("service up\\n"); sys.stdout.flush()
path = os.path.join(os.environ["XDG_RUNTIME_DIR"], "xodus.sock")
s = socket.socket(socket.AF_UNIX); s.bind(path); s.listen(4)
signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
while True:
    c, _ = s.accept()
    data = c.recv(64)  # the readiness probe sends nothing and closes
    if data:
        c.sendall(data.upper())
    c.close()
"""
_EXITS = "import sys; print('credential failure'); sys.exit(1)\n"
_HANGS = "import time\nwhile True: time.sleep(0.1)\n"


@pytest.fixture
def runtime_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="xpcs"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(path))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _service(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "xodus-service"
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    return script


def test_sessions_dir_prefers_the_runtime_dir(runtime_dir: Path) -> None:
    assert sessions_dir() == runtime_dir / "unifideck-xbox-pc"


@pytest.mark.asyncio
async def test_ready_service_gives_a_bridge_env_and_cleans_up(tmp_path: Path, runtime_dir: Path) -> None:
    sink = OutputSink(None)
    session = ServiceSession(_service(tmp_path, _SERVES), {"PATH": "/usr/bin:/bin"}, sink, pid=PID)
    await session.start(timeout_s=20)
    folder = session.runtime_dir
    assert folder is not None and folder.name.startswith(PID + "-")
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    env = session.env()
    assert env["XODUS_SOCKET"] == str(folder / "xodus.sock")
    assert len(env["XODUS_BRIDGE_KEY"]) == 64 and env["XODUS_SAVE_SYNC"] == "auto"
    with socket.create_connection(("127.0.0.1", int(env["XODUS_BRIDGE_PORT"])), timeout=5) as c:
        c.sendall(env["XODUS_BRIDGE_KEY"].encode() + b"ping")
        assert c.recv(16) == b"PING"
    proc = session.proc
    await session.stop()
    assert proc is not None and proc.returncode == 0, "SIGINT stopped it cleanly"
    assert not folder.exists()
    assert "service up" in sink.tail()


@pytest.mark.asyncio
async def test_an_early_exit_fails_fast(tmp_path: Path, runtime_dir: Path) -> None:
    session = ServiceSession(_service(tmp_path, _EXITS), {"PATH": "/usr/bin:/bin"}, OutputSink(None), pid=PID)
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(XboxPcLaunchError) as err:
        await session.start(timeout_s=60)
    assert err.value.reason == "cloud_save_unavailable"
    assert loop.time() - started < 10
    assert list((runtime_dir / "unifideck-xbox-pc").iterdir()) == []


@pytest.mark.asyncio
async def test_cancel_during_start_kills_the_service_and_removes_the_dir(
    tmp_path: Path, runtime_dir: Path,
) -> None:
    session = ServiceSession(_service(tmp_path, _HANGS), {"PATH": "/usr/bin:/bin"}, OutputSink(None), pid=PID)
    task = asyncio.ensure_future(session.start(timeout_s=60))
    await asyncio.sleep(0.5)
    proc = session.proc
    assert proc is not None
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=15)
    assert task in done and task.cancelled()
    assert proc.returncode is not None
    assert list((runtime_dir / "unifideck-xbox-pc").iterdir()) == []


@pytest.mark.asyncio
async def test_a_missing_binary_is_cloud_save_unavailable(tmp_path: Path, runtime_dir: Path) -> None:
    session = ServiceSession(tmp_path / "absent", {}, OutputSink(None), pid=PID)
    with pytest.raises(XboxPcLaunchError) as err:
        await session.start(timeout_s=5)
    assert err.value.reason == "cloud_save_unavailable"
