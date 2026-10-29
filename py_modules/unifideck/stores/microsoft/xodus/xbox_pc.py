"""stores/microsoft/xodus/xbox_pc.py — the Xbox PC facade ``MicrosoftStore`` builds.

``XboxPc`` owns everything the QAM's Xbox PC section shows:

* the opt-in toggle and the "unofficial" notice it needs first;
* the components (backend and runtime) and their install;
* the Xodus sign-in and the account check;
* the library counts the PC library (``pc_library``) reports back.

``status()`` is one snapshot shape for the RPCs and the
``XBOX_PC_STATE_CHANGED`` event (at most 2 per second). It is built from
cached state only. It never runs ``profile``, and it never carries the
account email or a top-level ``success`` key (the RPC wrapper would lift
it). ``refresh_status()`` is the call that asks Xodus again.

Turning the toggle off deletes nothing. Every blocking call (hashing,
config writes, CDP probes) goes through ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import platform
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from unifideck.core.types import Events
from unifideck.launcher.xbox_pc.layout import (
    XBOX_PC_ENABLED_KEY,
    XBOX_PC_NOTICE_ACK_KEY,
    XBOX_PC_SOURCE_KEY,
    credential_dir,
)

from . import account_state, auto_components, repair
from .auth import XboxPcSignIn
from .cli import XodusCli, XodusCliError
from .cli_models import AccountStatus
from .release_source import ReleaseSource
from .runtime import ComponentInstaller, ComponentInstallError
from .runtime_source import LocalDirSource, source_from_config
from .runtime_status import probe_runtime

if TYPE_CHECKING:
    from unifideck.auth.edge_browser import EdgeBrowser
    from unifideck.config import ConfigManager
    from unifideck.event_bus.event_bus import EventBus
    from unifideck.stores.microsoft.tokens import MicrosoftTokenManager

logger = logging.getLogger(__name__)

_LOG = "[XboxPc]"
#: Bump when the notice text changes in a way the user must read again.
NOTICE_VERSION = 1
#: ``account()`` is asked at most this often for readiness checks.
ACCOUNT_CACHE_S = 30.0
EMIT_INTERVAL_S = 0.5
_EMPTY_LIBRARY: dict[str, Any] = {
    "installable": 0, "not_supported": 0, "choice": 0, "skipped": 0,
    "stale": False, "last_read": None,
}


class XboxPcError(Exception):
    """A refused request. ``code`` is shown to the user."""

    def __init__(self, code: str) -> None:
        """``code`` is like ``notice_not_acknowledged``."""
        super().__init__(code)
        self.code = code


def unavailable_status() -> dict[str, Any]:
    """The status when no Microsoft store is registered."""
    return {
        "available": False, "enabled": False, "notice_version": NOTICE_VERSION,
        "notice_ack_version": 0, "xcloud_signed_in": False,
        "runtime": {"state": "missing", "version": None, "source_dir": "", "degraded": []},
        "account": {"state": "unknown"},
        "library": dict(_EMPTY_LIBRARY),
    }


def _drop_credentials() -> int:
    """Remove the stored Xodus credentials by hand. Used only when the CLI
    cannot sign out (no backend). Returns how many files went."""
    folder = credential_dir()
    if not folder.is_dir() or folder.is_symlink():
        return 0
    removed = 0
    for entry in folder.iterdir():
        if entry.is_file() or entry.is_symlink():
            with contextlib.suppress(OSError):
                entry.unlink()
                removed += 1
    return removed


class _Throttle:
    """Runs ``emit`` at most once per interval, always with the latest state."""

    def __init__(self, emit: Callable[[], Awaitable[None]], interval: float) -> None:
        self._emit = emit
        self._interval = interval
        self._last = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def poke(self) -> None:
        if self._timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        delay = max(0.0, self._last + self._interval - loop.time())
        self._timer = loop.call_later(delay, self._fire)

    def _fire(self) -> None:
        self._timer = None
        self._last = asyncio.get_running_loop().time()
        task = asyncio.create_task(self._emit())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None


class XboxPc:
    """The Xbox PC feature, as seen by the Microsoft store and the RPCs."""

    def __init__(
        self,
        config: ConfigManager | None,
        edge: Callable[[], EdgeBrowser | None],
        tokens: MicrosoftTokenManager,
        ms_auth_busy: Callable[[], bool],
        bus: EventBus,
        *,
        xcloud_signed_in: Callable[[], bool] = lambda: False,
        market_hint: Callable[[], str | None] = lambda: None,
        cli: XodusCli | None = None,
    ) -> None:
        """Getters, not values: Edge and the xCloud session come later."""
        self._config = config
        self._bus = bus
        self._xcloud_signed_in = xcloud_signed_in
        self._market_hint = market_hint
        #: The one ``XodusCli``. The PC library and the installer use it too.
        self.cli = cli or XodusCli()
        self._installer = ComponentInstaller(self._source)
        self._signin = XboxPcSignIn(self.cli, edge, tokens, ms_auth_busy, self._on_sign_in_change)
        self._background: set[asyncio.Task[None]] = set()
        #: A sign-in just ended and the account is being re-read. The view
        #: stays ``signing_in`` until then, so nobody sees the old account.
        self._settling_sign_in = False
        self._install_task: asyncio.Task[None] | None = None
        self._install_error: str | None = None
        self._progress: dict[str, Any] | None = None
        self._runtime: dict[str, Any] | None = None
        self._account: AccountStatus | None = None
        self._account_error: str | None = None
        self._account_at = 0.0
        self._saved: account_state.AccountState | None = None
        self._library: dict[str, Any] = dict(_EMPTY_LIBRARY)
        #: Called after an explicit Xodus sign-out (the PC library clears its
        #: titles). A listener may be sync or async.
        self.sign_out_listeners: list[Callable[[], Awaitable[None] | None]] = []
        #: Told the account view once a sign-in has ended and been re-read.
        self.sign_in_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._throttle = _Throttle(self._emit_state, EMIT_INTERVAL_S)

    # ── Config ──────────────────────────────────────────────────────

    def _get(self, key: str) -> Any:
        return self._config.get(key) if self._config is not None else None

    def _source(self) -> LocalDirSource | ReleaseSource:
        return source_from_config(self._get(XBOX_PC_SOURCE_KEY))

    def enabled(self) -> bool:
        """Whether the user turned Xbox PC on."""
        return self._get(XBOX_PC_ENABLED_KEY) is True

    def _notice_ack(self) -> int:
        value = self._get(XBOX_PC_NOTICE_ACK_KEY)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    async def _set(self, key: str, value: Any) -> None:
        if self._config is not None:
            await asyncio.to_thread(self._config.set, key, value)

    async def set_enabled(self, on: bool, notice_version: int | None = None) -> dict[str, Any]:
        """Turn the feature on or off. Raises ``XboxPcError``.

        Turning it on needs the current notice acknowledged, now
        (``notice_version``) or before, and installs missing or outdated
        components in the background: a saved Xodus sign-in skips every
        other path that would. Turning it off cancels a running sign-in and
        deletes nothing.
        """
        if on:
            if isinstance(notice_version, int) and notice_version >= NOTICE_VERSION:
                await self._set(XBOX_PC_NOTICE_ACK_KEY, notice_version)
            elif self._notice_ack() < NOTICE_VERSION:
                raise XboxPcError("notice_not_acknowledged")
        else:
            await self._signin.cancel()
        await self._set(XBOX_PC_ENABLED_KEY, bool(on))
        logger.info("%s turned %s", _LOG, "on" if on else "off")
        await self._probe()
        self.notify()
        if on:
            auto_components.schedule(self)
        return await self.status()

    def market_hint(self) -> str | None:
        """The xCloud session market, when it is known."""
        return self._market_hint()

    # ── Status ──────────────────────────────────────────────────────

    async def _probe(self) -> None:
        self._runtime = await asyncio.to_thread(probe_runtime, self._get(XBOX_PC_SOURCE_KEY), self.enabled())

    def _runtime_view(self) -> dict[str, Any]:
        view = dict(self._runtime or {"state": "missing", "version": None, "source_dir": "", "degraded": []})
        if self._installer.running:
            view["state"] = "installing"
            if self._progress is not None:
                view["progress"] = dict(self._progress)
            view.pop("error", None)
        elif self._install_error is not None:
            view["error"] = self._install_error
            if view["state"] == "missing":
                view["state"] = "failed"
        return view

    def _runtime_usable(self) -> bool:
        return (self._runtime or {}).get("state") in ("installed", "outdated")

    def _account_view(self) -> dict[str, Any]:
        if self._signin.running or self._settling_sign_in:
            running: dict[str, Any] = {"state": "signing_in"}
            if self._signin.step is not None:
                running["step"] = self._signin.step
            return running
        saved = self._saved
        view = self._account_state()
        if saved is not None and saved.gamertag and view["state"] in ("signed_in", "unverified"):
            view["gamertag"] = saved.gamertag
        if self._signin.last_error and view["state"] != "signed_in":
            view.setdefault("error", self._signin.last_error)
        return view

    def _account_state(self) -> dict[str, Any]:
        """The account's state from the last check and the saved verification."""
        saved = self._saved
        if self._account_error is not None:
            return {"state": "error", "error": self._account_error}
        if self._account is None:
            return {"state": "unknown"}
        if not self._account.stored:
            return {"state": "mismatch" if saved is not None and saved.mismatch else "signed_out"}
        if saved is not None and saved.refused:
            # Stored, but Xbox Live refuses it: not usable, so installs must
            # not read it as signed in (I-062).
            return {"state": "error", "error": "xbox_refused"}
        if saved is None or saved.unverified:
            return {"state": "unverified"}
        return {"state": "signed_in"}

    def _snapshot(self) -> dict[str, Any]:
        return {
            "available": platform.machine() == "x86_64",
            "enabled": self.enabled(),
            "notice_version": NOTICE_VERSION,
            "notice_ack_version": self._notice_ack(),
            "xcloud_signed_in": bool(self._xcloud_signed_in()),
            "runtime": self._runtime_view(),
            "account": self._account_view(),
            "library": dict(self._library),
        }

    async def status(self) -> dict[str, Any]:
        """The cached status. Probes the components once if never probed."""
        if self._runtime is None:
            await self._probe()
        return self._snapshot()

    async def checked_status(self) -> dict[str, Any]:
        """The status, with an account nobody asked about yet asked about once.

        After a plugin restart the account is ``unknown`` until the QAM row
        refreshes it. The install precondition reads this instead of
        :meth:`status`, so a signed-in user is not refused as signed out.
        """
        if self._runtime is None:
            await self._probe()
        if self._account is None and self._account_error is None and not self._signin.running:
            await self._refresh_account(force=True)
        return self._snapshot()

    async def refresh_components(self) -> dict[str, Any]:
        """Probe the components against the source again; no account check."""
        await self._probe()
        self.notify()
        return self._snapshot()

    async def refresh_status(self) -> dict[str, Any]:
        """Probe the components and ask Xodus about the account again."""
        await self._probe()
        await self._refresh_account(force=True)
        if self._account is not None and self._account.stored and not self._signin.running:
            self._saved = await asyncio.to_thread(account_state.read_state)
            if account_state.needs_recheck(self._saved):
                await self._signin.verify()
                self._saved = await asyncio.to_thread(account_state.read_state)
        self.notify()
        return self._snapshot()

    async def _refresh_account(self, *, force: bool) -> None:
        if not force and time.monotonic() - self._account_at < ACCOUNT_CACHE_S:
            return
        self._account_at = time.monotonic()
        if not self._runtime_usable():
            self._account, self._account_error = None, None
            return
        try:
            self._account = await self.cli.account()
            self._account_error = None
        except XodusCliError as e:
            if e.code == "busy":
                # A download holds the credential lock. Keep what we knew
                # and ask again next time.
                self._account_at = 0.0
                logger.info("%s account check deferred: a download holds the credentials", _LOG)
                return
            self._account_error = f"xodus_{e.code}"
            logger.info("%s account check failed: %s", _LOG, self._account_error)
        self._saved = await asyncio.to_thread(account_state.read_state)

    def signed_in_cached(self) -> bool:
        """Whether the last check found a usable stored account."""
        return self._account_view()["state"] in ("signed_in", "unverified")

    async def is_ready(self) -> bool:
        """On, components installed and verified, and Xodus signed in."""
        if not self.enabled():
            return False
        if self._runtime is None:
            await self._probe()
        if not self._runtime_usable():
            return False
        await self._refresh_account(force=False)
        return self.signed_in_cached()

    def set_library_counts(self, counts: dict[str, Any]) -> None:
        """The PC library's latest counts (``status.library``)."""
        merged = {**_EMPTY_LIBRARY, **{k: counts[k] for k in _EMPTY_LIBRARY if k in counts}}
        if merged != self._library:
            self._library = merged
            self.notify()

    # ── Events ──────────────────────────────────────────────────────

    def notify(self) -> None:
        """Send ``XBOX_PC_STATE_CHANGED`` soon (at most 2 per second)."""
        self._throttle.poke()

    async def _emit_state(self) -> None:
        try:
            await self._bus.emit(Events.XBOX_PC_STATE_CHANGED, **self._snapshot())
        except Exception:
            logger.exception("%s could not send the status event", _LOG)

    # ── Components ──────────────────────────────────────────────────

    async def install_components(self) -> dict[str, Any]:
        """Start the component install in the background.

        ``{started, reason}``. Progress and the end arrive only as
        ``XBOX_PC_STATE_CHANGED``.
        """
        if not self.enabled():
            return {"started": False, "reason": "xbox_pc_disabled"}
        if self._install_task is not None and not self._install_task.done():
            return {"started": False, "reason": "install_in_progress"}
        self._install_error = None
        self._progress = {"stage": "checking", "done_bytes": 0, "total_bytes": None}
        self._install_task = asyncio.create_task(self._run_install(), name="xbox-pc-install")
        await asyncio.sleep(0)
        self.notify()
        return {"started": True, "reason": None}

    async def _run_install(self) -> None:
        loop = asyncio.get_running_loop()

        def progress(stage: str, done: int, total: int | None) -> None:
            loop.call_soon_threadsafe(self._on_progress, stage, done, total)

        try:
            active = await self._installer.install(progress)
            logger.info("%s components %s installed", _LOG, active.runtime_version)
        except ComponentInstallError as e:
            self._install_error = None if e.code == "cancelled" else e.code
            logger.warning("%s component install failed: %s", _LOG, e.code)
        except Exception:
            logger.exception("%s component install crashed", _LOG)
            self._install_error = "install_failed"
        finally:
            self._progress = None
            await self._probe()
            self.notify()

    def _on_progress(self, stage: str, done: int, total: int | None) -> None:
        self._progress = {"stage": stage, "done_bytes": done, "total_bytes": total}
        self.notify()

    async def cancel_install(self) -> dict[str, Any]:
        """Stop a running component install. ``{cancelled}``."""
        running = self._installer.running
        self._installer.cancel()
        return {"cancelled": running}

    # ── Sign-in ─────────────────────────────────────────────────────

    def _on_sign_in_change(self) -> None:
        """A sign-in started or ended. After it ends, re-read the account.

        The end arrives from the sign-in task's done callback, so ``running``
        is already False here (I-060).

        The end is not announced until the re-read finishes. The cached
        account is the one from before the sign-in (``signed_out``), and the
        frontend settles on the first status that is not ``signing_in``, so
        announcing it would show a failure for a sign-in that worked.
        """
        if self._signin.running:
            self.notify()
            return
        self._settling_sign_in = True
        try:
            task = asyncio.get_running_loop().create_task(self._after_sign_in())
        except RuntimeError:
            self._settling_sign_in = False
            self.notify()
            return
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _after_sign_in(self) -> None:
        try:
            await self._refresh_account(force=True)
        finally:
            self._settling_sign_in = False
            self.notify()
            for listener in list(self.sign_in_listeners):
                listener(self._account_view())

    @property
    def sign_in_running(self) -> bool:
        """Whether the Xodus sign-in is running (``MicrosoftStore.start_auth`` refuses then)."""
        return self._signin.running

    async def start_sign_in(self, *, connect: bool = False) -> dict[str, Any]:
        """``{pending, launch_shortcut, reason, status}``; ``status`` may already show the end.

        ``connect``: this is the Microsoft sign-in itself (no OAuth sign-in needed first)."""
        if not self.enabled():
            return {"pending": False, "launch_shortcut": None, "reason": "xbox_pc_disabled"}
        result = await self._signin.start(connect=connect)
        self.notify()
        return {**result, "status": self._snapshot()}

    async def repair_target(self, game_id: str) -> dict[str, Any]:
        """Where a repair of ``game_id`` installs: ``{base_path, title}`` or ``{reason}``."""
        return await asyncio.to_thread(repair.repair_target, game_id)

    async def cancel_sign_in(self) -> dict[str, Any]:
        """Stop a running sign-in."""
        await self._signin.cancel()
        return self._snapshot()

    async def sign_out(self) -> dict[str, Any]:
        """Forget the Xodus account. The PC library drops its titles.

        Raises :class:`XboxPcError` ``xodus_busy`` while a download holds
        the credentials.
        """
        await self._signin.cancel()
        try:
            await self.cli.logout()
        except XodusCliError as e:
            if e.code == "busy":
                # A download is using the credentials. Deleting them under it
                # would fail the download, so the sign-out is refused.
                raise XboxPcError("xodus_busy") from None
            removed = await asyncio.to_thread(_drop_credentials)
            logger.warning("%s logout failed (%s); removed %d credential file(s)", _LOG, e.code, removed)
        await asyncio.to_thread(account_state.clear_state)
        self._account, self._account_error, self._saved = AccountStatus(stored=False), None, None
        for listener in list(self.sign_out_listeners):
            try:
                result = listener()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("%s sign-out listener failed", _LOG)
        self.notify()
        return self._snapshot()

    async def on_microsoft_logout(self) -> None:
        """Microsoft sign-out: sign the Xbox PC account out with it.

        From the user's side there is one Microsoft sign-in, so signing out
        of Microsoft signs out of both. A download holding the credentials
        keeps them (``xodus_busy``): deleting them under it would fail the
        download. That is logged, and the next sign-out or sign-in clears it.
        """
        try:
            await self.sign_out()
        except XboxPcError as e:
            logger.warning("%s Xbox PC sign-out skipped at Microsoft sign-out: %s", _LOG, e.code)
            await self._signin.cancel()

    async def shutdown(self) -> None:
        """Plugin unload."""
        await self._signin.cancel()
        await self._installer.shutdown()
        task = self._install_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for pending in list(self._background):
            pending.cancel()
        self._throttle.close()
