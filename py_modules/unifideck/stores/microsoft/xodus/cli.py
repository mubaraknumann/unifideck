"""stores/microsoft/xodus/cli.py — the only plugin module that runs xodus-cli.

Rules every call follows:

* The binary comes from the verified ``backend/<dir>/`` (``check_backend``),
  never from ``PATH``. An upstream ``xodus-cli`` cannot read our credentials
  (I-012).
* JSON commands are read with ``communicate()``, never ``readline``.
  ``library --json`` prints one 146 KB line, and asyncio's line reader
  stops at 64 KiB.
* A timeout or a cancel kills the whole process tree before it returns.
* stderr is redacted line by line before it is kept. It can carry the
  account email.
* Calls that read or write the stored credentials share one
  ``asyncio.Lock``. The install and the update check take it too, around
  their own runs. ``account``, ``profile`` and ``logout`` wait for it at
  most ``CREDENTIAL_WAIT_S`` and then fail with ``xodus_busy``.
* ``library`` and ``profile`` refresh the same tokens a running game uses,
  so they refuse while any launch lock is held (a game is running).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from unifideck.launcher.xbox_pc import argv as xargv
from unifideck.launcher.xbox_pc.backend import BackendPaths, check_backend
from unifideck.launcher.xbox_pc.component_locks import components_in_use
from unifideck.launcher.xbox_pc.layout import (
    ActiveComponents,
    credential_dir,
    data_root,
    ensure_private_dir,
    read_active,
)
from unifideck.stores.shared.cli_install_helpers import (
    TailRingBuffer,
    terminate_process_tree,
)

from .cli_models import (
    AccountStatus,
    ParseError,
    Profile,
    last_json_object,
    parse_account,
    parse_profile,
    redact,
)

logger = logging.getLogger(__name__)

_LOG = "[XodusCli]"
#: Line limit for the processes we read line by line (login, streaming).
SPAWN_LIMIT = 1 << 20
#: Rust exits 101 after a panic.
_PANIC_RC = 101
_PANIC_MARK = "panicked at"
#: serde's text for an empty or cut-off response (I-006).
_EOF_MARKS = ("EOF while parsing", "unexpected end of")
_CREDENTIAL_MARK = "NEXUS_CREDENTIAL_ERROR_V1 "
#: The credential code Xodus prints when nothing is stored.
_NOT_SIGNED_IN = "credential_keyring_missing_data"

ACCOUNT_TIMEOUT_S = 10.0
PROFILE_TIMEOUT_S = 30.0
LIBRARY_TIMEOUT_S = 60.0
RESOLVE_TIMEOUT_S = 60.0
LOGOUT_TIMEOUT_S = 30.0
#: How long ``account``, ``profile`` and ``logout`` wait for the credential lock. A game
#: download holds it for hours, so they answer ``busy`` instead of hanging.
CREDENTIAL_WAIT_S = 5.0

LibraryStatus = Literal["ok", "signed_out", "not_ready", "failed", "busy"]


class XodusCliError(Exception):
    """A failed Xodus command. ``str()`` is ``xodus_<code>[: tail]``."""

    def __init__(self, code: str, tail: str = "") -> None:
        """``tail`` is already redacted."""
        self.code = code
        self.tail = tail
        super().__init__(f"xodus_{code}: {tail}" if tail else f"xodus_{code}")


@dataclass(frozen=True)
class LibraryRead:
    """The outcome of ``library --json``.

    ``stdout`` is the raw JSON line on ``ok`` and empty otherwise. It holds
    the account email, so it is hidden from ``repr`` and never logged.
    """

    status: LibraryStatus
    stdout: str = field(default="", repr=False)
    tail: str = ""


@dataclass(frozen=True)
class _RunOutput:
    rc: int
    stdout: str = field(repr=False)
    tail: str
    credential: str | None
    panic: bool
    empty_response: bool


def _scan_stderr(stderr: str) -> tuple[TailRingBuffer, str | None, bool]:
    """Redacted tail, the credential code if any, and whether it panicked."""
    tail = TailRingBuffer(maxlen=20)
    credential: str | None = None
    panic = False
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(_CREDENTIAL_MARK):
            code = line[len(_CREDENTIAL_MARK):].strip().replace("-", "_")
            credential = f"credential_{code}" if code else credential
        panic = panic or _PANIC_MARK in line
        tail.append(redact(line)[:300])
    return tail, credential, panic


def classify_output(rc: int, stdout: bytes, stderr: bytes) -> _RunOutput:
    """Sort a finished run into its outcome. Pure, for tests."""
    err_text = stderr.decode(errors="replace")
    tail, credential, panic = _scan_stderr(err_text)
    return _RunOutput(
        rc=rc,
        stdout=stdout.decode(errors="replace"),
        tail=tail.tail(),
        credential=credential,
        panic=panic or rc == _PANIC_RC,
        empty_response=any(mark in err_text for mark in _EOF_MARKS),
    )


def raise_for(out: _RunOutput) -> str:
    """``out.stdout`` when the run succeeded; raise ``XodusCliError`` if not."""
    if out.credential is not None:
        raise XodusCliError(out.credential, out.tail)
    if out.panic:
        raise XodusCliError("empty_response" if out.empty_response else "panic", out.tail)
    if out.rc != 0:
        raise XodusCliError(f"exit_{out.rc}", out.tail)
    return out.stdout


def _installed_backend() -> tuple[BackendPaths | None, str | None]:
    """The verified backend, or why there is none. Blocking (hashes)."""
    active = read_active()
    if active is None:
        return None, "backend_missing"
    return check_backend(active)


class XodusCli:
    """Runs ``xodus-cli`` for the plugin. One instance per ``XboxPc``."""

    def __init__(self, *, env_base: Mapping[str, str] | None = None) -> None:
        """``env_base`` is the environment to clean (default ``os.environ``)."""
        self._env_base = env_base
        #: Held by every run that reads or writes the stored credentials.
        self.credential_lock = asyncio.Lock()

    # ── Paths and environment ───────────────────────────────────────

    def credential_dir(self) -> Path:
        """``XODUS_CREDENTIAL_DIR``, created 0700. Blocking."""
        ensure_private_dir(data_root())
        return ensure_private_dir(credential_dir())

    def _env(self) -> dict[str, str]:
        base = os.environ if self._env_base is None else self._env_base
        return xargv.build_env(base, self.credential_dir())

    async def backend(self) -> BackendPaths:
        """The verified binaries, or raise ``backend_missing``/``backend_tampered``."""
        paths, problem = await asyncio.to_thread(_installed_backend)
        if paths is None:
            raise XodusCliError(problem or "backend_missing")
        return paths

    @staticmethod
    async def _backend_of(active: ActiveComponents) -> BackendPaths:
        """The verified binaries of ``active``. Raises like :meth:`backend`."""
        paths, problem = await asyncio.to_thread(check_backend, active)
        if paths is None:
            raise XodusCliError(problem or "backend_missing")
        return paths

    # ── Running ─────────────────────────────────────────────────────

    async def _run(self, argv: list[str], *, timeout_s: float) -> _RunOutput:
        """Run one command to the end, killing its tree on timeout or cancel."""
        proc = await self._spawn(argv, stdin=asyncio.subprocess.DEVNULL)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            await terminate_process_tree(proc, _LOG)
            raise XodusCliError("timeout", f"{argv[1]} took longer than {timeout_s:.0f}s") from None
        except BaseException:
            await terminate_process_tree(proc, _LOG)
            raise
        rc = proc.returncode if proc.returncode is not None else -1
        return classify_output(rc, stdout, stderr)

    async def _spawn(self, argv: list[str], *, stdin: int) -> asyncio.subprocess.Process:
        """Start ``argv`` in its own session. A failed exec is ``spawn_failed``."""
        env = await asyncio.to_thread(self._env)
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=SPAWN_LIMIT,
                start_new_session=True,
            )
        except OSError as e:
            raise XodusCliError("spawn_failed", f"{argv[1]}: {e.strerror or type(e).__name__}") from None

    async def _json(self, argv: list[str], timeout_s: float) -> str:
        out = await self._run(argv, timeout_s=timeout_s)
        stdout = raise_for(out)
        if not stdout.strip():
            raise XodusCliError("empty_response", out.tail)
        return stdout

    @contextlib.asynccontextmanager
    async def _credential_turn(self) -> AsyncIterator[None]:
        """Hold ``credential_lock``, or raise ``busy`` after ``CREDENTIAL_WAIT_S``."""
        try:
            await asyncio.wait_for(self.credential_lock.acquire(), CREDENTIAL_WAIT_S)
        except TimeoutError:
            raise XodusCliError("busy") from None
        try:
            yield
        finally:
            self.credential_lock.release()

    async def _refuse_while_playing(self) -> None:
        if await asyncio.to_thread(components_in_use):
            raise XodusCliError("busy_game_running")

    # ── Commands ────────────────────────────────────────────────────

    async def account(self) -> AccountStatus:
        """Whether Xodus has a stored account."""
        paths = await self.backend()
        async with self._credential_turn():
            stdout = await self._json(xargv.account_argv(paths.cli), ACCOUNT_TIMEOUT_S)
        try:
            return parse_account(stdout)
        except ParseError as e:
            raise XodusCliError(e.code) from None

    async def profile(self) -> Profile:
        """The stored account's gamertag. Costly: a token exchange and a
        picture download, so it is only called to verify an account."""
        paths = await self.backend()
        await self._refuse_while_playing()
        async with self._credential_turn():
            stdout = await self._json(xargv.profile_argv(paths.cli), PROFILE_TIMEOUT_S)
        try:
            return parse_profile(stdout)
        except ParseError as e:
            raise XodusCliError(e.code) from None

    async def library(self, market: str) -> LibraryRead:
        """``library --json``. Never raises for an expected failure."""
        xargv.validate_market(market)
        try:
            paths = await self.backend()
        except XodusCliError as e:
            return LibraryRead("not_ready", tail=str(e))
        if await asyncio.to_thread(components_in_use) or self.credential_lock.locked():
            return LibraryRead("busy", tail="xodus_busy_game_running")
        try:
            async with self.credential_lock:
                stdout = await self._json(xargv.library_argv(paths.cli, market), LIBRARY_TIMEOUT_S)
        except XodusCliError as e:
            status: LibraryStatus = "signed_out" if e.code == _NOT_SIGNED_IN else "failed"
            return LibraryRead(status, tail=str(e))
        line = next((ln for ln in reversed(stdout.splitlines()) if ln.lstrip().startswith("{")), "")
        if not line:
            return LibraryRead("failed", tail="xodus_bad_json")
        return LibraryRead("ok", stdout=line.strip())

    async def resolve_package(self, pid: str, market: str) -> dict[str, Any]:
        """``resolve-package``: which package to install. Needs no credentials."""
        paths = await self.backend()
        argv = xargv.resolve_package_argv(paths.cli, pid, market)
        data = last_json_object(await self._json(argv, RESOLVE_TIMEOUT_S))
        if data is None:
            raise XodusCliError("bad_json")
        return data

    async def logout(self, device: bool = False) -> None:
        """Forget the stored account (and the device identity if ``device``)."""
        paths = await self.backend()
        argv = xargv.logout_argv(paths.cli, device=device)
        async with self._credential_turn():
            raise_for(await self._run(argv, timeout_s=LOGOUT_TIMEOUT_S))

    # ── Long-running processes (the caller drives them) ─────────────

    async def spawn_login(self) -> asyncio.subprocess.Process:
        """``--external-webview-stdio login`` with every pipe open.

        The caller holds ``credential_lock`` for the life of the process.
        """
        paths = await self.backend()
        return await self._spawn(xargv.login_argv(paths.cli), stdin=asyncio.subprocess.PIPE)

    async def spawn_streaming(
        self, pid: str, dest: str | Path, market: str, content_id: str,
        active: ActiveComponents | None = None,
    ) -> asyncio.subprocess.Process:
        """``streaming ... --progress-json``. stdout and stderr stay separate.

        The caller holds ``credential_lock`` until the process exits.
        ``active`` is the set of components whose locks the caller holds.
        The binary is taken from it, so the running process is always one
        the locks protect, even when a promote lands in between.
        """
        paths = await self._backend_of(active) if active is not None else await self.backend()
        argv = xargv.streaming_argv(paths.cli, pid, dest, market, content_id)
        return await self._spawn(argv, stdin=asyncio.subprocess.DEVNULL)
