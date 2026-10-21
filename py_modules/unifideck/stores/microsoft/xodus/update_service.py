"""stores/microsoft/xodus/update_service.py — ask ``xodus-service`` about updates.

The update check is a request to ``xodus-service`` (``xodus-store-updates.patch``),
not a CLI command. The wire format, from ``crates/xodus-service/src``:

* Frame: ``u32 magic | u16 type | u16 size | body``, all little-endian.
  The XML magic is ``0x58445358``. The reply type is the request type + 1.
* Request, type 9 (``STORE_PACKAGE_UPDATES_REQUEST``)::

      <Request><ProductId>9PK087LNGJC5</ProductId>
               <InstalledVersion>1.0.5.0</InstalledVersion></Request>

* Reply, type 10::

      <Response><HResult>0</HResult><Checked>true</Checked>
                <LatestVersion>1.0.5.0</LatestVersion><Count>0</Count>
                <ErrorStage></ErrorStage></Response>

  ``HResult`` 0 with ``Checked`` true: up to date. ``0x80004001`` at stage
  ``version-comparison``: a newer package exists. Anything else, or an
  empty body, is "unknown", never "up to date".

The service binds ``$XDG_RUNTIME_DIR/xodus.sock`` (the socket-path patch is
not in our build), so every session gets a private ``XDG_RUNTIME_DIR``. It
takes no arguments (I-005). It exits with status 1 when it cannot read the
credentials, so an early exit fails the session at once rather than waiting
for the socket.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import stat
import struct
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from unifideck.launcher.xbox_pc import xml_lite
from unifideck.launcher.xbox_pc.argv import build_env, service_argv
from unifideck.stores.shared.cli_install_helpers import TailRingBuffer, terminate_process_tree

from .diagnostics import Diagnostics, drain_stderr, stop_drain

logger = logging.getLogger(__name__)

XML_MAGIC = 0x58445358
UPDATES_REQUEST = 9
UPDATES_RESPONSE = 10
_HEADER = struct.Struct("<IHH")
#: ``E_NOTIMPL``: what the service answers when the installed version is older.
HRESULT_NEWER = 0x80004001
NEWER_STAGE = "version-comparison"

#: ``sun_path`` holds 108 bytes, the terminating NUL included.
SOCKET_PATH_MAX = 107
SOCKET_NAME = "xodus.sock"
START_TIMEOUT_S = 60.0
POLL_S = 0.25
QUERY_TIMEOUT_S = 75.0
STOP_GRACE_S = 4.0
LOG_PREFIX = "[xbox_updates]"


class ServiceError(RuntimeError):
    """The service did not start, or a query failed at the transport level."""


@dataclass(frozen=True)
class UpdateVerdict:
    """The service's answer for one product."""

    available: bool | None
    latest: str
    stage: str


def encode_request(pid: str, installed_version: str) -> bytes:
    """One type 9 frame. Both values are validated by the caller."""
    body = (
        f"<Request><ProductId>{pid}</ProductId>"
        f"<InstalledVersion>{installed_version}</InstalledVersion></Request>"
    ).encode()
    return _HEADER.pack(XML_MAGIC, UPDATES_REQUEST, len(body)) + body


def _child_text(root: xml_lite.Element, name: str) -> str:
    """A child element's stripped text, or ``""``."""
    node = root.find(name)
    return (node.text or "").strip() if node is not None else ""


def parse_reply(body: bytes) -> UpdateVerdict:
    """The verdict in a type 10 body. Unknown on anything unexpected."""
    try:
        # The reply comes from our own service over a private 0600 socket.
        root = xml_lite.fromstring(body.decode())
        hresult = int(_child_text(root, "HResult") or "-1")
    except (ValueError, xml_lite.ParseError):
        return UpdateVerdict(None, "", "reply-unreadable")
    checked = _child_text(root, "Checked") == "true"
    latest = _child_text(root, "LatestVersion")
    stage = _child_text(root, "ErrorStage")
    if hresult == 0 and checked:
        return UpdateVerdict(False, latest, "")
    if hresult == HRESULT_NEWER and stage == NEWER_STAGE:
        return UpdateVerdict(True, latest, stage)
    return UpdateVerdict(None, latest, stage or f"hresult-{hresult:#x}")


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    """One reply frame's body. Raises ``ServiceError`` on a bad header."""
    try:
        magic, kind, size = _HEADER.unpack(await reader.readexactly(_HEADER.size))
        body = await reader.readexactly(size)
    except (asyncio.IncompleteReadError, OSError) as e:
        raise ServiceError(f"reply cut short: {type(e).__name__}") from e
    if magic != XML_MAGIC or kind != UPDATES_RESPONSE:
        raise ServiceError(f"unexpected reply magic={magic:#x} type={kind}")
    return body


async def query(socket_path: str, pid: str, installed_version: str) -> UpdateVerdict:
    """Ask the service about one product over a fresh connection."""
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path)
    except OSError as e:
        raise ServiceError(f"cannot connect: {e.strerror or type(e).__name__}") from e
    try:
        writer.write(encode_request(pid, installed_version))
        await writer.drain()
        body = await asyncio.wait_for(_read_frame(reader), timeout=QUERY_TIMEOUT_S)
    except TimeoutError as e:
        raise ServiceError("no reply in time") from e
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    return parse_reply(body) if body else UpdateVerdict(None, "", "empty-reply")


def _session_dir() -> Path:
    """A private, short folder for the service socket."""
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    parent = runtime if os.path.isabs(runtime) and os.path.isdir(runtime) else None
    folder = Path(tempfile.mkdtemp(prefix="uxs-", dir=parent))
    if len(os.fsencode(folder / SOCKET_NAME)) > SOCKET_PATH_MAX:
        shutil.rmtree(folder, ignore_errors=True)
        raise ServiceError("socket path too long")
    return folder


def service_env(
    base: Mapping[str, str], credential_dir: Path, folder: Path,
) -> dict[str, str]:
    """The service's env: the CLI env plus the private runtime folder."""
    env = build_env(base, credential_dir)
    env["XDG_RUNTIME_DIR"] = str(folder)
    # Harmless today; the socket-path patch would read it.
    env["XODUS_SOCKET"] = str(folder / SOCKET_NAME)
    return env


def _socket_ready(sock: Path) -> bool:
    """Whether the service has bound its socket (one ``stat``)."""
    try:
        return stat.S_ISSOCK(sock.stat().st_mode)
    except OSError:
        return False


async def _wait_for_socket(proc: Any, sock: Path, diags: Diagnostics) -> None:
    """Return once ``sock`` exists. Raises when the service exits first."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + START_TIMEOUT_S
    while not _socket_ready(sock):
        if proc.returncode is not None:
            codes = diags.credential_errors()
            raise ServiceError(
                f"service exited {proc.returncode}"
                + (f" ({codes[-1]})" if codes else ""),
            )
        if loop.time() > deadline:
            raise ServiceError("service did not open its socket")
        await asyncio.sleep(POLL_S)


async def _stop(proc: Any) -> None:
    """SIGINT (the service removes its socket), then the tree kill.

    A cancel during the grace wait skips to the tree kill and is then
    raised again, so the caller still sees it.
    """
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGINT)
        waiter = asyncio.ensure_future(proc.wait())
        try:
            await asyncio.wait({waiter}, timeout=STOP_GRACE_S)
        except asyncio.CancelledError:
            waiter.cancel()
            await terminate_process_tree(proc, LOG_PREFIX)
            raise
        waiter.cancel()
    await terminate_process_tree(proc, LOG_PREFIX)


async def _spawn(service: Path, env: dict[str, str], folder: Path) -> Any:
    """Start the service with no arguments (I-005). Raises ``ServiceError``."""
    try:
        return await asyncio.create_subprocess_exec(
            *service_argv(service), env=env, cwd=str(folder),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
    except OSError as e:
        raise ServiceError(f"cannot start the service: {e.strerror or type(e).__name__}") from e


def _start_drain(proc: Any, diags: Diagnostics) -> asyncio.Task[None]:
    """Drain the service's stderr for its credential diagnostics."""
    return asyncio.ensure_future(drain_stderr(proc.stderr, TailRingBuffer(), diags))


async def _teardown(proc: Any, drain: asyncio.Task[None] | None, folder: Path) -> None:
    """Stop the service and its drain, then remove the private folder."""
    try:
        if proc is not None:
            await _stop(proc)
    finally:
        try:
            if drain is not None:
                await stop_drain(drain)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


@contextlib.asynccontextmanager
async def service_session(
    service: Path, credential_dir: Path, *, base_env: Mapping[str, str] | None = None,
) -> AsyncIterator[str]:
    """Run ``xodus-service`` for the ``async with`` body; yield its socket path.

    The process and its folder are always cleaned up, a cancel included.
    """
    folder = await asyncio.to_thread(_session_dir)
    env = service_env(os.environ if base_env is None else base_env, credential_dir, folder)
    diags = Diagnostics()
    sock = folder / SOCKET_NAME
    proc: Any = None
    drain: asyncio.Task[None] | None = None
    try:
        proc = await _spawn(service, env, folder)
        drain = _start_drain(proc, diags)
        await _wait_for_socket(proc, sock, diags)
        yield str(sock)
    finally:
        await _teardown(proc, drain, folder)
