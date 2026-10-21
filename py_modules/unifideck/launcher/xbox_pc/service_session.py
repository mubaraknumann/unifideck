"""launcher/xbox_pc/service_session.py — ``xodus-service`` for one game session.

The runtime syncs Xbox cloud saves and signs the game in to Xbox through
``xodus-service``. Each launch starts its own service on a private
``XDG_RUNTIME_DIR``: the build binds ``$XDG_RUNTIME_DIR/xodus.sock`` and
has no socket-path option, so a shared runtime dir would let two games
share one socket. The game reaches it through the :class:`Bridge`.

* The service takes no arguments; even ``--help`` starts it (I-005). Its
  stdin stays an open pipe, as Nexus keeps it.
* Start-up is polled on the event loop, so a Stop during start-up cancels
  it at once. An early exit (a credential failure exits 1) fails fast.
* Stop and cancel run SIGINT, then SIGTERM, then SIGKILL, and remove the
  session folder.

Session folders are ``<sessions>/<PID>-*``. The sweep of a game removes only
its own, while it holds that game's lock.

Stdlib only, Python 3.10 safe.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import socket
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .argv import service_argv
from .bridge import Bridge
from .errors import XboxPcLaunchError
from .layout import ensure_private_dir, state_dir
from .output import OutputSink
from .stop import signal_group, wait_exit

logger = logging.getLogger(__name__)

SERVICE_READY_TIMEOUT_S = 90.0
POLL_S = 0.25
SOCKET_NAME = "xodus.sock"
_SOCKET_PATH_MAX = 107  # sun_path is 108 bytes with its NUL
_SESSIONS_SUBDIR = "unifideck-xbox-pc"


def sessions_dir() -> Path:
    """Parent of the private service folders.

    ``$XDG_RUNTIME_DIR/unifideck-xbox-pc`` (tmpfs, short paths), else
    ``state/service-sessions`` under the feature's data root.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isabs(runtime) and os.path.isdir(runtime):
        return Path(runtime) / _SESSIONS_SUBDIR
    return state_dir() / "service-sessions"


def socket_ready(path: str) -> bool:
    """Whether something accepts connections on the unix socket ``path``."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


class ServiceSession:
    """One ``xodus-service`` and its bridge, for one launch."""

    def __init__(
        self, service: Path, env: Mapping[str, str], sink: OutputSink, *, pid: str,
    ) -> None:
        """``env`` is the Xodus env (``argv.build_env``) the service runs with."""
        self._service = service
        self._env = dict(env)
        self._sink = sink
        self._pid = pid
        self.runtime_dir: Path | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.bridge: Bridge | None = None
        self._pumps: list[asyncio.Future[None]] = []

    @property
    def socket_path(self) -> str:
        """The service's socket in the private folder."""
        assert self.runtime_dir is not None
        return str(self.runtime_dir / SOCKET_NAME)

    def env(self) -> dict[str, str]:
        """The game env that points the runtime at this service."""
        assert self.bridge is not None
        return {
            "XODUS_SOCKET": self.socket_path,
            "XODUS_BRIDGE_PORT": str(self.bridge.port),
            "XODUS_BRIDGE_KEY": self.bridge.key,
            "XODUS_SAVE_SYNC": "auto",
        }

    def _make_runtime_dir(self) -> Path:
        """A fresh 0700 folder for the socket. Raises when its path is too long."""
        parent = sessions_dir()
        ensure_private_dir(parent)
        folder = Path(tempfile.mkdtemp(prefix=f"{self._pid}-", dir=parent))
        os.chmod(folder, 0o700)
        if len(os.fsencode(str(folder / SOCKET_NAME))) > _SOCKET_PATH_MAX:
            shutil.rmtree(folder, ignore_errors=True)
            raise XboxPcLaunchError("cloud_save_unavailable", f"socket path too long under {parent}")
        return folder

    async def _spawn(self) -> asyncio.subprocess.Process:
        """Start the service process with its output pumped into the sink."""
        self.runtime_dir = await asyncio.to_thread(self._make_runtime_dir)
        env = dict(self._env)
        env["XDG_RUNTIME_DIR"] = str(self.runtime_dir)
        try:
            proc = await asyncio.create_subprocess_exec(
                *service_argv(self._service), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
        except OSError as err:
            raise XboxPcLaunchError("cloud_save_unavailable", f"xodus-service did not start: {err}") from err
        self._pumps.append(asyncio.ensure_future(self._sink.pump(proc.stdout, source="service")))
        return proc

    async def _wait_ready(self, timeout_s: float) -> None:
        """Poll for the socket. Raises when the service exits or times out."""
        proc = self.proc
        assert proc is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            if await asyncio.to_thread(socket_ready, self.socket_path):
                return
            if proc.returncode is not None:
                raise XboxPcLaunchError(
                    "cloud_save_unavailable", f"xodus-service exited {proc.returncode}",
                )
            await asyncio.sleep(POLL_S)
        raise XboxPcLaunchError(
            "cloud_save_unavailable", f"xodus-service not ready after {timeout_s:.0f}s",
        )

    async def start(self, timeout_s: float = SERVICE_READY_TIMEOUT_S) -> None:
        """Start the service and the bridge. On any failure or cancel, clean up."""
        try:
            self.proc = await self._spawn()
            await self._wait_ready(timeout_s)
            self.bridge = Bridge(self.socket_path)
        except BaseException:
            await self.stop()
            raise
        logger.info("[xbox_pc.service] account service and bridge ready")

    async def stop(self) -> None:
        """Close the bridge, stop the service, remove the session folder."""
        if self.bridge is not None:
            self.bridge.stop()
            self.bridge = None
        proc = self.proc
        if proc is not None and proc.returncode is None:
            await self._stop_process(proc)
        for pump in self._pumps:
            if not pump.done():
                await asyncio.wait({pump}, timeout=2.0)
            if not pump.done():
                pump.cancel()
        self._pumps.clear()
        if self.runtime_dir is not None:
            await asyncio.to_thread(shutil.rmtree, self.runtime_dir, ignore_errors=True)
            self.runtime_dir = None

    @staticmethod
    async def _stop_process(proc: asyncio.subprocess.Process) -> None:
        """SIGINT, then SIGTERM after 4 s, then SIGKILL after 2 s."""
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        for sig, grace in ((signal.SIGINT, 4.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
            if not signal_group(proc, sig):
                return
            if await wait_exit(proc, grace):
                return
        logger.warning("[xbox_pc.service] xodus-service survived SIGKILL (pid %d)", proc.pid)
