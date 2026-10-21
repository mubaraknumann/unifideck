"""stores/microsoft/xodus/auth.py — sign Xodus in through the Edge auth window.

The flow reuses the Microsoft auth shortcut, so the window opens the way
every other sign-in does, in Desktop and in Gaming Mode::

    1. checks: verified backend, Microsoft signed in, Edge installed,
       no other sign-in running, no stream or storefront window open
    2. take the credential lock, write ``about:blank`` to ms_auth_url.txt
    3. start ``xodus-cli --external-webview-stdio login``; answer ``ready``
    4. the frontend launches the auth shortcut: Edge opens
       ``--app=about:blank`` with CDP on 9222, in the real edge-auth profile
    5. attach to that page (120 s) and relay frames both ways
       (``auth_page.CxhPageBridge``) until the CLI exits
    6. on exit 0, check the account: Xodus's gamertag must match the one in
       Unifideck's own Xbox sign-in (XSTS ``gtg``)

The edge-auth profile carries the Microsoft session, so the user usually
only approves the sign-in (Microsoft may still ask for an Authenticator
approval on a new device, I-009).

The two Microsoft sign-ins never run together: this one refuses while the
xCloud sign-in runs, and ``MicrosoftStore.start_auth`` refuses while this
one runs (``running``). Edge allows one process per profile, so a stream or
storefront window open on 9223 or 9224 refuses with ``edge_busy``.

Failure reasons (``last_error``): ``window_not_opened``, ``window_closed``,
``signin_timeout``, ``not_stored``, ``account_mismatch``, ``xodus_*``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from unifideck.auth.edge_browser.cdp_session import CLOSED_EVENT, CdpError, CdpPageSession
from unifideck.auth.url_file import remove_url_file, write_url_file_atomically
from unifideck.stores.shared.cli_install_helpers import TailRingBuffer, terminate_process_tree

from . import auth_protocol as proto
from .account_state import AccountState, write_state
from .auth_page import CxhPageBridge
from .cli import XodusCli, XodusCliError
from .cli_models import redact

if TYPE_CHECKING:
    from unifideck.auth.edge_browser import EdgeBrowser
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager

logger = logging.getLogger(__name__)

_LOG = "[XboxPcSignIn]"
#: The file the auth shortcut opens (``launcher/flows/auth.py``).
MS_AUTH_URL_FILE = "~/.local/share/unifideck/ms_auth_url.txt"
READY_WAIT_S = 45.0
ATTACH_WAIT_S = 120.0
SIGNIN_TIMEOUT_S = 600.0
LOCK_WAIT_S = 15.0
#: After the window closes, the CLI gets this long to exit on its own.
CLOSED_GRACE_S = 15.0
_CREDENTIAL_MARK = "NEXUS_CREDENTIAL_ERROR_V1 "


class _RefusalError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _refused(reason: str) -> dict[str, Any]:
    return {"pending": False, "launch_shortcut": None, "reason": reason}


def _edge_state(edge: EdgeBrowser) -> tuple[bool, list[bool]]:
    """Whether Edge is installed, and which of its windows answer CDP:
    ``[stream or web game, storefront, auth]``. Blocking probes."""
    if not edge.is_installed:
        return False, [False, False, False]
    ports = (edge.browser_game_cdp_port(), edge.storefront_cdp_port(), edge.cdp_port)
    return True, [edge.cdp_alive(port) for port in ports]


def same_gamertag(a: str, b: str) -> bool:
    """Gamertags compare without case or surrounding space."""
    return a.strip().casefold() == b.strip().casefold()


class _Run:
    """One running login: the CLI's pipes, the page, and their merged queue."""

    def __init__(self, proc: asyncio.subprocess.Process, page: CdpPageSession) -> None:
        self.proc = proc
        self.page = page
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.bridge: CxhPageBridge | None = None
        self.buffered: list[dict[str, Any]] = []
        self.window_closed = False
        self.tail = TailRingBuffer(maxlen=20)
        self.credential: str | None = None
        self.tasks: list[asyncio.Task[None]] = []

    def start(self, port: int) -> None:
        for coro in (self._pump_stdout(), self._pump_stderr(), self._wait(), self._attach(port)):
            self.tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.page.close()

    async def send(self, frame: dict[str, Any]) -> None:
        stdin = self.proc.stdin
        if stdin is None:
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(proto.encode(frame))
            await stdin.drain()

    async def _pump_stdout(self) -> None:
        stream = self.proc.stdout
        while stream is not None:
            try:
                raw = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                logger.warning("%s skipped an over-long CLI line", _LOG)
                continue
            if not raw:
                return
            frame = proto.parse_frame(raw.decode(errors="replace"))
            if frame is not None:
                await self.queue.put(("cli", frame))

    async def _pump_stderr(self) -> None:
        stream = self.proc.stderr
        while stream is not None:
            try:
                raw = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not raw:
                return
            line = raw.decode(errors="replace").strip()
            if line.startswith(_CREDENTIAL_MARK):
                self.credential = line[len(_CREDENTIAL_MARK):].strip().replace("-", "_")
            if line:
                self.tail.append(redact(line)[:300])

    async def _wait(self) -> None:
        rc = await self.proc.wait()
        await self.queue.put(("exit", rc))

    async def _attach(self, port: int) -> None:
        ok = await self.page.attach(port, wait_s=ATTACH_WAIT_S)
        await self.queue.put(("attached", ok))

    async def _pump_page(self) -> None:
        while True:
            msg = await self.page.events.get()
            await self.queue.put(("cdp", msg))
            if msg.get("method") == CLOSED_EVENT:
                return

    # ── One step of the loop: (finished, failure reason or None) ────

    async def step(self, kind: str, data: Any) -> tuple[bool, str | None]:
        if kind == "cli":
            await self._on_cli(data)
            return False, None
        if kind == "attached":
            return await self._on_attached(bool(data))
        if kind == "cdp":
            await self._on_cdp(data)
            return False, None
        return True, self._exit_reason(int(data))

    async def _on_cli(self, frame: dict[str, Any]) -> None:
        if self.bridge is None:
            self.buffered.append(frame)
            return
        try:
            await self.bridge.on_cli_frame(frame)
        except CdpError as e:
            logger.warning("%s could not drive the window: %s", _LOG, e)

    async def _on_attached(self, ok: bool) -> tuple[bool, str | None]:
        if not ok:
            return True, "window_not_opened"
        bridge = CxhPageBridge(self.page, self.send)
        try:
            await bridge.setup()
        except CdpError as e:
            logger.warning("%s window setup failed: %s", _LOG, e)
            return True, "window_not_opened"
        self.bridge = bridge
        self.tasks.append(asyncio.create_task(self._pump_page()))
        logger.info("%s attached to the sign-in window", _LOG)
        for frame in self.buffered:
            await self._on_cli(frame)
        self.buffered.clear()
        return False, None

    async def _on_cdp(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == CLOSED_EVENT:
            self.window_closed = True
            session = self.bridge.session if self.bridge is not None else None
            logger.info("%s sign-in window closed", _LOG)
            if session is not None:
                await self.send(proto.closed_frame(session))
            return
        if self.bridge is not None:
            try:
                await self.bridge.on_event(msg)
            except CdpError as e:
                logger.warning("%s page event failed: %s", _LOG, e)

    def _exit_reason(self, rc: int) -> str | None:
        if rc == 0:
            return None
        if self.window_closed:
            return "window_closed"
        if self.credential:
            return f"xodus_credential_{self.credential}"
        logger.warning("%s xodus-cli login exited %d: %s", _LOG, rc, self.tail.tail())
        return f"xodus_exit_{rc}"

    async def next_item(self) -> tuple[str, Any] | None:
        """The next queued item; None when the closed window's grace ran out."""
        if not self.window_closed:
            return await self.queue.get()
        try:
            return await asyncio.wait_for(self.queue.get(), CLOSED_GRACE_S)
        except TimeoutError:
            return None


class XboxPcSignIn:
    """Runs one Xodus sign-in at a time."""

    def __init__(
        self,
        cli: XodusCli,
        edge: Callable[[], EdgeBrowser | None],
        tokens: MicrosoftTokenManager,
        ms_auth_busy: Callable[[], bool],
        on_change: Callable[[], None],
        *,
        page_factory: Callable[[], CdpPageSession] = CdpPageSession,
    ) -> None:
        """``edge`` is a getter: the Edge service is injected after construction.

        ``ms_auth_busy`` reports whether the xCloud sign-in is running.
        ``on_change`` is called whenever ``running`` or ``last_error`` changes.
        """
        self._cli = cli
        self._edge = edge
        self._tokens = tokens
        self._ms_auth_busy = ms_auth_busy
        self._on_change = on_change
        self._page_factory = page_factory
        self._task: asyncio.Task[None] | None = None
        self._lock_held = False
        self._proc: asyncio.subprocess.Process | None = None
        #: Why the last sign-in failed, or None.
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        """Whether a sign-in is in progress."""
        return self._task is not None and not self._task.done()

    # ── Start ───────────────────────────────────────────────────────

    async def start(self, *, connect: bool = False) -> dict[str, Any]:
        """Start a sign-in. ``{pending, launch_shortcut, reason}``.

        ``connect``: this sign-in is the Microsoft sign-in (Xbox PC on, one
        sign-in), so no earlier Microsoft (OAuth) sign-in is required.
        """
        if self.running or self._ms_auth_busy():
            return _refused("auth_in_progress")
        try:
            reason = await self._precheck(connect)
            if reason is not None:
                return _refused(reason)
            port = await self._take_lock_and_port()
            proc = await self._open_cli()
        except _RefusalError as r:
            self._release_lock()
            return _refused(r.reason)
        except BaseException:
            self._release_lock()
            raise
        self.last_error = None
        self._proc = proc
        self._task = asyncio.create_task(self._session(proc, port), name="xbox-pc-sign-in")
        self._on_change()
        logger.info("%s started; waiting for the auth window", _LOG)
        return {"pending": True, "launch_shortcut": "microsoft", "reason": None}

    async def _precheck(self, connect: bool = False) -> str | None:
        try:
            await self._cli.backend()
        except XodusCliError as e:
            return e.code
        if not connect and not await self._tokens.load():
            return "microsoft_sign_in_required"
        edge = self._edge()
        if edge is None:
            return "edge_not_installed"
        installed, alive = await asyncio.to_thread(_edge_state, edge)
        if not installed:
            return "edge_not_installed"
        if alive[0] or alive[1]:
            return "edge_busy"
        if alive[2]:
            # Neither Microsoft sign-in is running, so this window was left over.
            await edge.prepare_auth_launch()
        return None

    async def _take_lock_and_port(self) -> int:
        try:
            await asyncio.wait_for(self._cli.credential_lock.acquire(), LOCK_WAIT_S)
        except TimeoutError:
            raise _RefusalError("xodus_busy") from None
        self._lock_held = True
        edge = self._edge()
        return edge.cdp_port if edge is not None else 9222

    def _release_lock(self) -> None:
        if self._lock_held:
            self._lock_held = False
            self._cli.credential_lock.release()

    async def _open_cli(self) -> asyncio.subprocess.Process:
        if not await write_url_file_atomically(MS_AUTH_URL_FILE, "about:blank"):
            raise _RefusalError("url_write_failed")
        try:
            proc = await self._cli.spawn_login()
        except XodusCliError as e:
            await remove_url_file(MS_AUTH_URL_FILE)
            raise _RefusalError(e.code) from None
        try:
            await asyncio.wait_for(self._await_ready(proc), READY_WAIT_S)
        except (TimeoutError, _RefusalError) as e:
            await terminate_process_tree(proc, _LOG)
            await remove_url_file(MS_AUTH_URL_FILE)
            raise _RefusalError(e.reason if isinstance(e, _RefusalError) else "xodus_not_ready") from None
        if proc.stdin is not None:
            proc.stdin.write(proto.encode(proto.hello_frame()))
            await proc.stdin.drain()
        return proc

    @staticmethod
    async def _await_ready(proc: asyncio.subprocess.Process) -> None:
        stream = proc.stdout
        while stream is not None:
            raw = await stream.readline()
            if not raw:
                break
            frame = proto.parse_frame(raw.decode(errors="replace"))
            if frame is None or frame.get("type") != "ready":
                continue
            if frame.get("version") != proto.PROTOCOL_VERSION:
                raise _RefusalError("xodus_protocol_mismatch")
            return
        raise _RefusalError("xodus_not_ready")

    # ── The running sign-in ─────────────────────────────────────────

    async def _session(self, proc: asyncio.subprocess.Process, port: int) -> None:
        try:
            try:
                outcome = await asyncio.wait_for(self._drive(proc, port), SIGNIN_TIMEOUT_S)
            except TimeoutError:
                outcome = "signin_timeout"
            finally:
                await self._cleanup(proc)
            if outcome is None:
                outcome = await self.verify()
        except asyncio.CancelledError:
            self.last_error = None
            raise
        except Exception:
            logger.exception("%s sign-in crashed", _LOG)
            outcome = "signin_failed"
        self.last_error = None if outcome in (None, "unverified") else outcome
        logger.info("%s finished: %s", _LOG, outcome or "signed in")
        self._on_change()

    async def _drive(self, proc: asyncio.subprocess.Process, port: int) -> str | None:
        run = _Run(proc, self._page_factory())
        run.start(port)
        try:
            while True:
                item = await run.next_item()
                if item is None:
                    return "window_closed"
                done, reason = await run.step(*item)
                if done:
                    return reason
        finally:
            await run.stop()

    async def _cleanup(self, proc: asyncio.subprocess.Process) -> None:
        """Every exit path: kill the CLI tree, close the window, drop the URL."""
        await terminate_process_tree(proc, _LOG)
        self._release_lock()
        edge = self._edge()
        if edge is not None:
            try:
                await edge.close_auth_browser()
            except Exception as e:
                logger.warning("%s could not close the sign-in window: %s", _LOG, e)
        await remove_url_file(MS_AUTH_URL_FILE)

    async def cancel(self) -> None:
        """Stop a running sign-in silently (no failure is recorded)."""
        task, self._task = self._task, None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        proc, self._proc = self._proc, None
        if self._lock_held and proc is not None:
            # Cancelled before it first ran, so its own cleanup never did.
            await self._cleanup(proc)
        self.last_error = None
        self._on_change()

    # ── Account check ───────────────────────────────────────────────

    async def verify(self) -> str | None:
        """Check the stored Xodus account against Unifideck's Xbox sign-in.

        Returns None (verified), ``unverified`` (a gamertag could not be
        read; checked again later), ``not_stored``, ``account_mismatch``
        (Xodus is signed out again) or an ``xodus_*`` error.
        """
        try:
            account = await self._cli.account()
        except XodusCliError as e:
            return str(e).split(":", 1)[0]
        if not account.stored:
            return "not_stored"
        profile_tag = await self._profile_gamertag()
        chain_tag = await self._chain_gamertag()
        now = time.time()
        if profile_tag and chain_tag:
            if same_gamertag(profile_tag, chain_tag):
                await asyncio.to_thread(write_state, AccountState(profile_tag, now))
                return None
            logger.warning("%s Xodus signed in to a different Xbox account; signing it out", _LOG)
            with contextlib.suppress(XodusCliError):
                await self._cli.logout()
            await asyncio.to_thread(write_state, AccountState(None, now, mismatch=True))
            return "account_mismatch"
        await asyncio.to_thread(write_state, AccountState(profile_tag, now, unverified=True))
        return "unverified"

    async def _profile_gamertag(self) -> str | None:
        try:
            return (await self._cli.profile()).gamertag
        except XodusCliError as e:
            logger.info("%s Xodus gamertag unreadable: %s", _LOG, e.code)
            return None

    async def _chain_gamertag(self) -> str | None:
        try:
            # Reload first: with the one sign-in this is the new Xodus ticket.
            await self._tokens.load()
            chain = await self._tokens.build_chain()
        except Exception as e:
            logger.info("%s Unifideck gamertag unreadable: %s", _LOG, type(e).__name__)
            return None
        tag = getattr(chain, "gamertag", None) if chain is not None else None
        return tag if isinstance(tag, str) and tag else None
